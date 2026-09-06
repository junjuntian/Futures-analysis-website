# -*- coding: utf-8 -*-
"""PLAN_FGSA_SIZING_v1 的跑数脚本 —— 分数仓位,按席位水位建仓。

**先读 PLAN_FGSA_SIZING_v1.md。**

口径:不改引擎。拿生产的 `replay` 跑出 `(trades, pos, daily)`,
再把日收益按权重缩放并扣掉**因分数仓位多出来的调仓成本**:

    exposure_t = pos_t × w_t          (w 用前一日收盘可见的值)
    daily_w    = daily_t × w_{t-1}
    turnover_t = |exposure_t − exposure_{t-1}|
    cost_t     = turnover_t × 费率 / (结算价 × 点值)

核心对照是「等效缩仓」:同样的平均仓位,但**权重是常数**。
分数仓位若赢不过它,就说明按水位调仓没有择时价值。

用法:CSV_DIR=research/data python research/run_fgsa_sizing.py
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
FEE = 2.0                    # 元/手/边,运营者给的真实值
FEE_X2 = 4.0                 # G6 敏感性
RECENT = pd.Timestamp("2024-09-01")
FOLLOW = "永安期货"

C = {}
for code, stem in (("FG", "fg"), ("SA", "sa")):
    H.use(code)
    price = H.clean_price(pd.read_csv(DATA / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(DATA / f"{stem}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    g, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        g, log = H.apply_group_overrides(g, log, cuts, H.RULES["group_overrides"], seat, price)
    if H.RULES.get("freeze_since"):
        g, log, cuts = H.freeze_groups(g, log, cuts, H.RULES["freeze_since"])
    rdf, _ = H.retail_series(seat, mkt.index)
    sig = H.signal_series(seat, g)
    if H.RULES["signal_source"] == "cost":
        sig = H.attach_cost_signal(sig, seat, mkt, g)
    trades, pos, daily = H.replay(sig, mkt, rdf, op, st)

    def lvl(series):
        mx = series.abs().rolling(H.SEAT_LEVEL_WIN,
                                  min_periods=H.SEAT_LEVEL_MIN).max()
        return (series.abs() / mx).clip(upper=1.0)

    w_grp = lvl(sig["net"])                       # H1:整组水位,不挑人
    r = seat[seat["member_key"] == FOLLOW]
    w_one = None
    if not r.empty:
        off, _f = H._pit_pair(r)
        w_one = lvl(off.reindex(mkt.index))       # H2:永安

    C[code] = dict(mkt=mkt, pos=pos, daily=daily, trades=trades,
                   w_grp=w_grp, w_one=w_one, mult=H.RULES["multiplier"])
    print(f"{code} 预处理完成({len(mkt)} 天,点值 {H.RULES['multiplier']:g},"
          f"基线 {len(trades)} 笔)")


def apply_size(code, w, fee=FEE, since=None):
    """w=None → 基线(满仓);w 为常数或序列 → 分数仓位。返回带成本的日收益。"""
    c = C[code]
    idx = c["daily"].index
    pos = c["pos"].reindex(idx).fillna(0.0)
    if w is None:
        ww = pd.Series(1.0, index=idx)
    elif np.isscalar(w):
        ww = pd.Series(float(w), index=idx)
    else:
        ww = w.reindex(idx)
    ww = ww.fillna(0.0).clip(0.0, 1.0)
    wlag = ww.shift(1).fillna(0.0)
    d = c["daily"].fillna(0.0) * wlag
    if w is not None and fee:
        expo = pos * ww
        turn = expo.diff().abs().fillna(expo.abs())
        px = c["mkt"]["settle"].reindex(idx).ffill()
        d = d - turn * fee / (px * c["mult"])
    if since is not None:
        d = d[d.index >= since]
        wlag = wlag[wlag.index >= since]
        pos = pos[pos.index >= since]
    held = pos.abs() > 0
    return d, (float(wlag[held].mean()) if held.any() else 1.0)


def perf(d, avgw, label):
    p = H._perf(d)
    return {"标签": label, "累计%": p["cum_pct"], "夏普": p["sharpe"] or 0.0,
            "回撤%": p["max_dd_pct"], "平均仓位": avgw, "daily": d}


def row(r):
    return (f"  {r['标签']:<26}{r['累计%']:>+10.1f}%{r['夏普']:>7.2f}"
            f"{r['回撤%']:>9.1f}%{r['平均仓位']:>10.0%}")


RES = {}
for code, name in (("FG", "玻璃"), ("SA", "纯碱")):
    for wlab, since in (("全样本(主口径)", None), ("近两年(参考)", RECENT)):
        print(f"\n{'='*86}\n=== {name} {code} · {wlab} ===")
        print(f"  {'方案':<24}{'累计':>11}{'夏普':>7}{'回撤':>10}{'平均仓位':>10}")
        out = {}
        d0, a0 = apply_size(code, None, since=since)
        out["基线"] = perf(d0, a0, "基线(满仓)")
        print(row(out["基线"]))
        for key, w, lab in (("H1", C[code]["w_grp"], "H1 整组水位(代表格)"),
                            ("H2", C[code]["w_one"], f"H2 {FOLLOW}水位")):
            if w is None:
                continue
            d, a = apply_size(code, w, since=since)
            out[key] = perf(d, a, lab)
            print(row(out[key]))
            # 等效缩仓对照:同样的平均仓位,权重是常数
            dc, ac = apply_size(code, a, since=since)
            out[key + "_flat"] = perf(dc, ac, f"   └ 等效缩仓 {a:.0%}(对照)")
            print(row(out[key + "_flat"]))
        if since is None:
            RES[code] = out

    # —— 闸门(全样本、代表格 H1)——
    o = RES[code]
    base, h1, flat = o["基线"], o["H1"], o["H1_flat"]
    print(f"\n  【G1 核心:必须胜过等效缩仓】H1 夏普需 ≥ 同平均仓位的固定缩仓")
    print(f"    {h1['夏普']:.2f} vs 等效缩仓 {flat['夏普']:.2f} → "
          f"{'过' if h1['夏普'] >= flat['夏普'] else '不过'}")
    print(f"  【G2 回撤(运营者点名)】需 ≤ 基线")
    print(f"    {h1['回撤%']:.1f}% vs 基线 {base['回撤%']:.1f}% → "
          f"{'过' if h1['回撤%'] >= base['回撤%'] else '不过'}")
    print(f"  【G3 夏普不塌】需 ≥ 基线")
    print(f"    {h1['夏普']:.2f} vs {base['夏普']:.2f} → "
          f"{'过' if h1['夏普'] >= base['夏普'] else '不过'}")
    print(f"  【G5 逐年】H1 ≥ 基线的年份需 ≥ 4/6 比例")
    a, b = h1["daily"].fillna(0), base["daily"].fillna(0)
    ya = ((1 + a).groupby(a.index.year).prod() - 1) * 100
    yb = ((1 + b).groupby(b.index.year).prod() - 1) * 100
    win = sum(1 for y in yb.index if ya.get(y, -1e9) >= yb[y])
    print(f"    {win}/{len(yb)} = {win/len(yb)*100:.0f}%  "
          f"{'过' if win/len(yb) >= 4/6 else '不过'}")
    print(f"  【G6 成本 2 倍(4 元/手/边)】G1~G3 结论不许变")
    d2, a2 = apply_size(code, C[code]["w_grp"], fee=FEE_X2)
    p2 = perf(d2, a2, "")
    df2, af2 = apply_size(code, a2, fee=FEE_X2)
    pf2 = perf(df2, af2, "")
    print(f"    H1 夏普 {p2['夏普']:.2f}(vs {h1['夏普']:.2f})、回撤 {p2['回撤%']:.1f}%;"
          f" 等效缩仓 {pf2['夏普']:.2f} → "
          f"{'结论不变' if (p2['夏普'] >= pf2['夏普']) == (h1['夏普'] >= flat['夏普']) else '**结论翻了**'}")
    # 调仓成本本身有多大
    dn, _ = apply_size(code, C[code]["w_grp"], fee=0.0)
    print(f"    调仓成本本身:不扣费 {perf(dn,0,'')['累计%']:+.1f}% vs 扣费 {h1['累计%']:+.1f}%"
          f"(差 {perf(dn,0,'')['累计%']-h1['累计%']:.2f}pp)")

print(f"\n{'='*86}\n=== G4 两品种同向(H1 − 基线 的夏普变化,全样本)===")
dd = {c: RES[c]["H1"]["夏普"] - RES[c]["基线"]["夏普"] for c in ("FG", "SA")}
print(f"  玻璃 {dd['FG']:+.3f}   纯碱 {dd['SA']:+.3f}   "
      f"→ {'同向,过' if np.sign(dd['FG']) == np.sign(dd['SA']) else '符号相反,不过'}")

print("\n判定按 PLAN_FGSA_SIZING_v1 第五节执行,本脚本不下结论。")
