# -*- coding: utf-8 -*-
"""PLAN_SA_EXIT_RETAIL_v1 的跑数脚本 —— 用散户动态改出场。

**先读 PLAN_SA_EXIT_RETAIL_v1.md。**

口径(预注册第三节):不改 `replay`,把 `retail["rz"]` 在被屏蔽的日子上置 **NaN**
(置 0 会让「消退」立刻触发,与意图相反),其余原样跑生产的回放。

用法:CSV_DIR=research/data python research/run_sa_exit_retail.py
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
QWIN = 250                          # 分位窗口,预注册固定,不许扫
RS, R_REP = [0.6, 0.7, 0.8], 0.7    # B 的三格,代表格 0.7
KS, K_REP = [1, 2, 3], 2            # A 的三格,代表格 2
DRAWS = 500
RNG = np.random.default_rng(20260906)
# 运营者事后指认的两轮完整下跌:(名字, 该出场日, 那天结算)
EPS = {"SA": [("2026 年 5~8 月", "2026-08-03"), ("2025 年 2~5 月", "2025-05-28")]}

C = {}
for code, stem in (("SA", "sa"), ("FG", "fg")):
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
    rdf, have = H.retail_series(seat, mkt.index)
    sig = H.attach_cost_signal(H.signal_series(seat, g), seat, mkt, g)
    q = rdf["net"].rolling(QWIN, min_periods=60).rank(pct=True)
    C[code] = {"mkt": mkt, "op": op, "st": st, "sig": sig, "rdf": rdf, "q": q,
               "price": price, "enter": H.RULES["enter"]}
    print(f"{code} 预处理完成({len(mkt)} 天,散户 = {'/'.join(have)})")


def run(code, kind=None, par=None, shift=0):
    """kind=None → 基线。B:散户分位极端时屏蔽散户出场。A:反向要连续 k 日。"""
    c = C[code]
    H.use(code)
    rdf = c["rdf"].copy()
    rz = rdf["rz"]
    if kind == "B":
        q = c["q"]
        if shift:
            q = pd.Series(np.roll(q.to_numpy(), shift), index=q.index)
        # rz > 0 会平掉空仓 → 散户分位仍高(还在接刀)时屏蔽
        # rz < 0 会平掉多仓 → 分位仍低时屏蔽
        block = (((rz > 0) & (q >= par)) | ((rz < 0) & (q <= 1 - par))).fillna(False)
        rdf["rz"] = rz.where(~block, np.nan)
    elif kind == "A" and par > 1:
        z = rz
        if shift:
            z = pd.Series(np.roll(z.to_numpy(), shift), index=z.index)
        e = c["enter"]
        strong = (z.abs() >= e)
        sgn = np.sign(z)
        ok = strong.copy()
        for j in range(1, par):
            ok &= strong.shift(j).fillna(False) & (sgn.shift(j) == sgn)
        # 未连续成立的日子:屏蔽散户那一路(NaN),让仓位继续拿着
        rdf["rz"] = z.where(ok | (z.abs() < e), np.nan)
    trades, _pos, daily = H.replay(c["sig"], c["mkt"], rdf, c["op"], c["st"])
    p = H._perf(daily)
    return {"累计%": p["cum_pct"], "夏普": p["sharpe"] or 0.0, "回撤%": p["max_dd_pct"],
            "笔数": len(trades), "daily": daily, "trades": trades}


def hold_median(r):
    return float(np.median([t["hold_days"] for t in r["trades"]])) if r["trades"] else np.nan


def exit_score(code, r):
    """出场点得分中位:做空该平在低位、做多该平在高位。"""
    px = C[code]["price"].set_index(["contract", "trade_date"]).sort_index()
    s = []
    for t in r["trades"]:
        try:
            sub = px.loc[t["contract"]]
        except KeyError:
            continue
        w = sub[sub.index <= pd.Timestamp(t["exit_date"])].tail(20)
        if len(w) < 20 or not np.isfinite(t["exit_px"]):
            continue
        hi, lo = float(w["high_price"].max()), float(w["low_price"].min())
        if hi <= lo:
            continue
        p = min(1.0, max(0.0, (t["exit_px"] - lo) / (hi - lo)))
        s.append((1 - p) if t["side"] == "short" else p)
    return float(np.median(s)) if s else np.nan


def show(tag, code, r):
    return (f"  {tag:<18}{r['累计%']:>+9.1f}%{r['夏普']:>7.2f}{r['回撤%']:>8.1f}%"
            f"{r['笔数']:>6}{hold_median(r):>9.0f}{exit_score(code, r):>10.2f}")


RES = {}
for code, name in (("SA", "纯碱"), ("FG", "玻璃")):
    print(f"\n{'='*86}\n=== {name} {code}(分位窗口 {QWIN} 日固定)===")
    base = run(code)
    print(f"  {'方案':<16}{'累计':>10}{'夏普':>7}{'回撤':>9}{'笔数':>6}{'中位持仓日':>10}{'出场点得分':>11}")
    print(show("基线(现行)", code, base))
    store = {"B": {}, "A": {}}
    for r_ in RS:
        store["B"][r_] = run(code, "B", r_)
        print(show(f"B 散户水平门 {r_}", code, store["B"][r_])
              + ("   ← 代表格" if r_ == R_REP else ""))
    for k in KS:
        store["A"][k] = run(code, "A", k)
        print(show(f"A 连续 {k} 日", code, store["A"][k])
              + ("   ← 代表格" if k == K_REP else ""))
    RES[code] = {"base": base, "store": store}

    for lab, kind, grid, rep, alpha in (("B 散户水平门", "B", RS, R_REP, 0.05),
                                        ("A 出场变慢", "A", KS, K_REP, 0.025)):
        r = store[kind][rep]
        print(f"\n  —— {lab}(代表格 {rep})——")
        good = [x for x in grid if store[kind][x]["夏普"] >= base["夏普"]
                and store[kind][x]["累计%"] >= base["累计%"]]
        print(f"    G1 相邻档同向:达标 {good} → {'过' if len(good) >= 2 else '不过'}")
        d, db = r["daily"].fillna(0), base["daily"].fillna(0)
        yb = ((1 + db).groupby(db.index.year).prod() - 1) * 100
        yr = ((1 + d).groupby(d.index.year).prod() - 1) * 100
        win = sum(1 for y in yb.index if yr.get(y, -1e9) >= yb[y])
        print(f"    G2 逐年:{win}/{len(yb)} = {win/len(yb)*100:.0f}%  "
              f"{'过' if win/len(yb) >= 4/6 else '不过'}")
        hm, hb = hold_median(r), hold_median(base)
        print(f"    G3 中位持仓日:{hm:.0f} vs 基线 {hb:.0f}  "
              f"{'过' if hm > hb else '不过'}")
        mid = d.index[len(d)//2]
        hn = ((1 + d[d.index >= mid]).prod() - 1) * 100
        hbf = ((1 + db[db.index >= mid]).prod() - 1) * 100
        print(f"    G4 后半:{hn:+.1f}% vs 基线 {hbf:+.1f}%  "
              f"{'过' if (hn >= 0 and hn >= hbf) else '不过'}")
        obs = r["夏普"] - base["夏普"]
        n = len(C[code]["mkt"])
        draws = np.array([run(code, kind, rep, shift=int(RNG.integers(20, n - 20)))["夏普"]
                          - base["夏普"] for _ in range(DRAWS)])
        p5 = float((draws >= obs).mean())
        print(f"    G5 安慰剂:提升 {obs:+.3f}, p={p5:.3f}(需 <{alpha})  "
              f"{'过' if p5 < alpha else '不过'}")

    # 另报不设闸:运营者标的两轮里,代表格实际在哪天出场
    if code in EPS:
        print(f"\n  —— 另报(不设闸):运营者标的两轮里代表格实际出场日 ——")
        for nm, want in EPS[code]:
            w = pd.Timestamp(want)
            for tag, rr in (("基线", base), (f"B r={R_REP}", store["B"][R_REP]),
                            (f"A k={K_REP}", store["A"][K_REP])):
                near = [t for t in rr["trades"]
                        if abs((pd.Timestamp(t["exit_date"]) - w).days) <= 75
                        and pd.Timestamp(t["exit_date"]) >= w - pd.Timedelta(days=120)]
                s = "、".join(f"{t['exit_date']}@{t['exit_px']:.0f}({t['exit_reason']})"
                             for t in near[-3:]) or "该窗口无出场"
                print(f"    {nm} 该在 {want} —— {tag:<10}{s}")

print(f"\n=== G6 两品种同向(代表格夏普提升符号)===")
for lab, kind, rep in (("B 散户水平门", "B", R_REP), ("A 出场变慢", "A", K_REP)):
    d = {c: RES[c]["store"][kind][rep]["夏普"] - RES[c]["base"]["夏普"] for c in ("SA", "FG")}
    print(f"  {lab}: 纯碱 {d['SA']:+.3f}  玻璃 {d['FG']:+.3f}  "
          f"→ {'同向,过' if np.sign(d['SA']) == np.sign(d['FG']) else '符号相反,不过'}")

print("\n判定按 PLAN_SA_EXIT_RETAIL_v1 第五节执行,本脚本不下结论。")
