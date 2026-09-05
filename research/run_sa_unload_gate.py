# -*- coding: utf-8 -*-
"""PLAN_SA_UNLOAD_GATE_v1 的跑数脚本 —— 卸仓门在「区间上沿」放宽。

**先读 PLAN_SA_UNLOAD_GATE_v1.md。**

口径(预注册第三节):不重写 `cost_entry_frame`,把 `unload` 序列在
`pos_20 ≥ p 且 unload ≤ ucap` 的日子上置 0,再原样调生产的 `cost_entry_frame`。
该函数对 `unload` 只有 `u > umax` 一处比较,置 0 与新规则等价,且不产生第二份实现。

用法:CSV_DIR=research/data python research/run_sa_unload_gate.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import hog_money as H  # noqa: E402

DATA = Path(os.environ.get("CSV_DIR", "research/data"))
POS_N = 20                       # 位置窗口,预注册固定,不许扫
UCAP = 0.60                      # 放宽后的卸仓上限,预注册固定,不许扫
PS, P_REP = [0.6, 0.7, 0.8], 0.7  # 就这 3 格;代表格事前指定 0.7
DRAWS = 500                      # G5 安慰剂次数,按预注册
RNG = np.random.default_rng(20260906)

C = {}
for code, stem in (("SA", "sa"), ("FG", "fg")):
    H.use(code)
    price = H.clean_price(pd.read_csv(DATA / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(DATA / f"{stem}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        groups, log = H.apply_group_overrides(groups, log, cuts,
                                              H.RULES["group_overrides"], seat, price)
    if H.RULES.get("freeze_since"):
        groups, log, cuts = H.freeze_groups(groups, log, cuts, H.RULES["freeze_since"])
    raw = H.signal_series(seat, groups)
    unl = H.unload_series(raw, seat, groups)["pct"].reindex(mkt.index)
    cc = H.inst_cost_series(raw, mkt, groups)
    prod = H.attach_cost_signal(raw, seat, mkt, groups)      # 生产口径,自检用
    rdf, _h = H.retail_series(seat, mkt.index)

    m = price.set_index(["contract", "trade_date"])
    hi, lo = [], []
    for d, row in mkt.iterrows():
        c_ = row["main"]
        if isinstance(c_, str) and (c_, d) in m.index:
            hi.append(float(m.loc[(c_, d), "high_price"]))
            lo.append(float(m.loc[(c_, d), "low_price"]))
        else:
            hi.append(np.nan)
            lo.append(np.nan)
    hi, lo = pd.Series(hi, index=mkt.index), pd.Series(lo, index=mkt.index)
    pos = ((mkt["settle"] - lo.rolling(POS_N).min())
           / (hi.rolling(POS_N).max() - lo.rolling(POS_N).min())).clip(0, 1)

    C[code] = {"raw": raw, "prod": prod, "unl": unl, "cc": cc, "mkt": mkt,
               "op": op, "st": st, "rdf": rdf, "pos": pos, "umax": H.RULES["cost_unload_max"]}
    print(f"{code} 预处理完成({len(mkt)} 天,umax={H.RULES['cost_unload_max']:.0%})")


def build(code, p=None, shift=0):
    """p=None → 基线(完全走生产口径)。"""
    c = C[code]
    H.use(code)
    unl = c["unl"]
    if p is not None:
        pos = c["pos"]
        if shift:
            pos = pd.Series(np.roll(pos.to_numpy(), shift), index=pos.index)
        relax = ((pos >= p) & (unl <= UCAP)).fillna(False)
        unl = unl.where(~relax, 0.0)
    ext = H.cost_entry_frame(c["cc"], c["raw"]["net"], c["mkt"]["settle"],
                             unl, c["raw"]["chg"].reindex(c["mkt"].index))
    return c["raw"].assign(cost_z=ext["cost_z"].reindex(c["raw"].index),
                           cost_reason=ext["cost_reason"].reindex(c["raw"].index))


def run(code, p=None, shift=0):
    c = C[code]
    H.use(code)
    sig = build(code, p, shift)
    trades, _pos, daily = H.replay(sig, c["mkt"], c["rdf"], c["op"], c["st"])
    perf = H._perf(daily)
    return {"累计%": perf["cum_pct"], "夏普": perf["sharpe"] or 0.0,
            "回撤%": perf["max_dd_pct"], "笔数": len(trades), "daily": daily}


# ---- 自检:不放宽时必须与生产逐字节一致(预注册第三节要求) ----
for code in ("SA", "FG"):
    a = build(code)["cost_z"]
    b = C[code]["prod"]["cost_z"]
    assert a.equals(b), f"{code} 基线与生产口径不一致,后面的数全部作废"
    # p 取不可能触发的值,也必须回到生产
    a2 = build(code, p=9.9)["cost_z"]
    assert a2.equals(b), f"{code} p=9.9 竟改变了信号,放宽逻辑写错了"
print("自检通过:基线 = 生产口径,逐字节一致\n")


def show(tag, r):
    return (f"  {tag:<20}{r['累计%']:>+10.1f}%{r['夏普']:>8.2f}"
            f"{r['回撤%']:>9.1f}%{r['笔数']:>7}")


RES = {}
for code, name in (("SA", "纯碱"), ("FG", "玻璃")):
    print(f"\n{'='*78}\n=== {name} {code}(ucap={UCAP:.0%} 固定,pos 窗口 {POS_N} 日)===")
    base = run(code)
    print(f"  {'方案':<18}{'累计':>11}{'夏普':>8}{'回撤':>10}{'笔数':>7}")
    print(show("基线(现行规则)", base))
    store = {}
    for p in PS:
        store[p] = run(code, p)
        print(show(f"上沿放宽 p={p}", store[p]) + ("   ← 代表格" if p == P_REP else ""))
    RES[code] = {"base": base, "store": store}
    r = store[P_REP]

    print(f"\n  【G1 相邻档同向】3 格中 ≥2 格 夏普与累计都 ≥ 基线")
    good = [p for p in PS if store[p]["夏普"] >= base["夏普"]
            and store[p]["累计%"] >= base["累计%"]]
    print(f"    达标格 {good} → {'过' if len(good) >= 2 else '不过'}")

    print(f"\n  【G2 逐年】代表格 ≥ 基线的年份需 ≥ 4/6 比例")
    d, db = r["daily"].fillna(0), base["daily"].fillna(0)
    yb = ((1 + db).groupby(db.index.year).prod() - 1) * 100
    yr = ((1 + d).groupby(d.index.year).prod() - 1) * 100
    win = 0
    for y in yb.index:
        ok = yr.get(y, -1e9) >= yb[y]
        win += ok
        print(f"    {y}  基线 {yb[y]:+7.1f}%   放宽后 {yr.get(y, np.nan):+7.1f}%   {'✓' if ok else '✗'}")
    print(f"    {win}/{len(yb)} = {win/len(yb)*100:.0f}%  "
          f"{'过' if win/len(yb) >= 4/6 else '不过'}")

    print(f"\n  【G3 笔数不暴涨】代表格笔数需 ≤ 基线的 200%")
    print(f"    {r['笔数']}/{base['笔数']} = {r['笔数']/base['笔数']*100:.0f}%  "
          f"{'过' if r['笔数'] <= base['笔数']*2 else '不过'}")

    print(f"\n  【G4 后半不塌】")
    mid = d.index[len(d)//2]
    hn = ((1 + d[d.index >= mid]).prod() - 1) * 100
    hb = ((1 + db[db.index >= mid]).prod() - 1) * 100
    print(f"    放宽后半 {hn:+.1f}%   基线后半 {hb:+.1f}%   "
          f"{'过' if (hn >= 0 and hn >= hb) else '不过'}(分界 {mid.date()})")

    print(f"\n  【G7 回撤(运营者点名)】代表格回撤需 ≤ 基线(数值上不更负)")
    # max_dd_pct 是负数,「回撤降低」= 数值更大(更接近 0)
    print(f"    放宽 {r['回撤%']:.1f}%  基线 {base['回撤%']:.1f}%   "
          f"{'过' if r['回撤%'] >= base['回撤%'] else '不过'}")

    print(f"\n  【G5 安慰剂】把 pos_20 整体随机平移 {DRAWS} 次,代表格夏普提升需 p<0.05")
    obs = r["夏普"] - base["夏普"]
    n = len(C[code]["mkt"])
    draws = np.array([run(code, P_REP, shift=int(RNG.integers(20, n - 20)))["夏普"]
                      - base["夏普"] for _ in range(DRAWS)])
    p5 = float((draws >= obs).mean())
    print(f"    实测提升 {obs:+.3f};随机平移中 ≥ 它的比例 p={p5:.3f}  "
          f"→ {'过' if p5 < 0.05 else '不过'}")

print(f"\n=== G6 两品种同向(代表格 p={P_REP} 的夏普提升符号)===")
dd = {c: RES[c]["store"][P_REP]["夏普"] - RES[c]["base"]["夏普"] for c in ("SA", "FG")}
print(f"  纯碱 {dd['SA']:+.3f}   玻璃 {dd['FG']:+.3f}   "
      f"→ {'同向,过' if np.sign(dd['SA']) == np.sign(dd['FG']) else '符号相反,不过'}")

print("\n判定按 PLAN_SA_UNLOAD_GATE_v1 第五节执行,本脚本不下结论。")
