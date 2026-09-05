# -*- coding: utf-8 -*-
"""PLAN_SA_ENTRY_RESONATE_v1 的跑数脚本 —— 成本进场加一道散户反向确认。

**先读 PLAN_SA_ENTRY_RESONATE_v1.md。**

做法:不改引擎,用 `replay` 的研究口子。原进场信号 `cost_z` 在不满足
「与 rz 同号且 |rz| ≥ k」的日子上**置 nan**(nan 不进场,`entry_side` 判不出来),
其余一切原样走 `replay` —— 与生产同一条回放,不另写一套。

用法:CSV_DIR=research/data python research/run_sa_entry_resonate.py
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
OLD_SEED = ["东方财富", "平安期货", "徽商期货"]
KS = [0.0, 0.5, 1.0]
REP = 0.5                       # 代表格,预注册写死
RNG = np.random.default_rng(20260905)

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
    sig = H.signal_series(seat, groups)
    sig = H.attach_cost_signal(sig, seat, mkt, groups)
    C[code] = {"seat": seat, "mkt": mkt, "op": op, "st": st, "sig": sig,
               "seed": list(H.RULES["retail_seed"])}
    print(f"{code} 预处理完成({len(mkt)} 天,seed={C[code]['seed']})")


def run(code, k=None, seed=None, shift=0):
    """k=None → 基线(不加确认)。shift → 把 rz 整体平移若干个交易日(安慰剂)。"""
    c = C[code]
    H.use(code)
    H.RULES["retail_seed"] = list(seed or c["seed"])
    rdf, _h = H.retail_series(c["seat"], c["mkt"].index)
    sig = c["sig"].copy()
    if k is not None:
        rz = rdf["rz"]
        if shift:
            rz = pd.Series(np.roll(rz.to_numpy(), shift), index=rz.index)
        cz = sig["cost_z"]
        ok = (np.sign(cz) == np.sign(rz)) & (rz.abs() >= k) & cz.notna() & rz.notna()
        sig["cost_z"] = cz.where(ok, np.nan)
    trades, _pos, daily = H.replay(sig, c["mkt"], rdf, c["op"], c["st"])
    p = H._perf(daily)
    return {"累计%": p["cum_pct"], "夏普": p["sharpe"] or 0.0,
            "回撤%": p["max_dd_pct"], "笔数": len(trades), "daily": daily}


def line(tag, r):
    return (f"{tag:<22}{r['累计%']:>+10.1f}%{r['夏普']:>8.2f}"
            f"{r['回撤%']:>9.1f}%{r['笔数']:>7}")


for code, name in (("SA", "纯碱"), ("FG", "玻璃")):
    print(f"\n{'='*72}\n=== {name} {code}(seed = {'、'.join(C[code]['seed'])})===")
    base = run(code)
    print(f"{'方案':<20}{'累计':>11}{'夏普':>8}{'回撤':>10}{'笔数':>7}")
    print(line("基线(不加确认)", base))
    res = {}
    for k in KS:
        res[k] = run(code, k=k)
        print(line(f"加确认 k={k}", res[k]))

    print("\n【G1 相邻档同向】三格中 ≥2 格 夏普与累计都 ≥ 基线")
    good = [k for k in KS if res[k]["夏普"] >= base["夏普"]
            and res[k]["累计%"] >= base["累计%"]]
    print(f"  达标格:{good}  → {'过' if len(good) >= 2 else '不过'}")

    print("\n【G3 笔数不塌】需 ≥ 基线的 50%")
    n0 = base["笔数"]
    for k in KS:
        n = res[k]["笔数"]
        mark = "✓" if n >= n0 * 0.5 else "✗"
        print(f"  k={k}: {n} / {n0} = {n/n0*100:.0f}%  {mark}"
              + ("   ← 代表格" if k == REP else ""))
    g3 = res[REP]["笔数"] >= n0 * 0.5

    print("\n【G2 逐年】代表格 k=0.5 vs 基线")
    yb = ((1 + base["daily"].fillna(0)).groupby(base["daily"].index.year).prod() - 1) * 100
    yr = ((1 + res[REP]["daily"].fillna(0)).groupby(res[REP]["daily"].index.year).prod() - 1) * 100
    win = tot = 0
    for y in sorted(set(yb.index) | set(yr.index)):
        a, b = yb.get(y, np.nan), yr.get(y, np.nan)
        if not (np.isfinite(a) and np.isfinite(b)):
            continue
        tot += 1
        ok = b >= a
        win += ok
        print(f"  {y}  基线 {a:+7.1f}%   加确认 {b:+7.1f}%   {'✓' if ok else '✗'}")
    print(f"  {win}/{tot}  {'过' if tot and win / tot >= 4 / 6 else '不过'}")

    print("\n【G4 后半不塌】")
    d = res[REP]["daily"].fillna(0)
    db = base["daily"].fillna(0)
    mid = d.index[len(d) // 2]
    h_new = ((1 + d[d.index >= mid]).prod() - 1) * 100
    h_base = ((1 + db[db.index >= mid]).prod() - 1) * 100
    print(f"  加确认后半 {h_new:+.1f}%   基线后半 {h_base:+.1f}%   "
          f"{'过' if (h_new >= 0 and h_new >= h_base) else '不过'}(分界 {mid.date()})")

    print("\n【G5 安慰剂】把 rz 整体随机平移 500 次,代表格夏普提升需 p<0.05")
    obs = res[REP]["夏普"] - base["夏普"]
    n = len(C[code]["mkt"])
    draws = []
    for _ in range(500):
        sh = int(RNG.integers(20, n - 20))
        draws.append(run(code, k=REP, shift=sh)["夏普"] - base["夏普"])
    draws = np.array(draws)
    p5 = float((draws >= obs).mean())
    print(f"  实测提升 {obs:+.3f};随机平移里 ≥ 它的比例 p = {p5:.3f}  "
          f"→ {'过' if p5 < 0.05 else '不过'}")

    print("\n【G6 对名单不敏感】用旧三家(东方财富/平安/徽商)重跑,符号不许翻")
    ob = run(code, seed=OLD_SEED)
    orr = run(code, k=REP, seed=OLD_SEED)
    print(line("  旧名单 基线", ob))
    print(line("  旧名单 加确认", orr))
    same = np.sign(orr["夏普"] - ob["夏普"]) == np.sign(obs)
    print(f"  提升 {orr['夏普']-ob['夏普']:+.3f} vs 新名单 {obs:+.3f}  "
          f"→ {'符号同向,过' if same else '符号翻了,不过'}")

print("\n判定按 PLAN_SA_ENTRY_RESONATE_v1 第五节执行,本脚本不下结论。")
