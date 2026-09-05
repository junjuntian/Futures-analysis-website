# -*- coding: utf-8 -*-
"""PLAN_SA_ENTRY_FILTER_v1 的跑数脚本 —— 成本进场加位置 / 持仓量 / 散户水平过滤。

**先读 PLAN_SA_ENTRY_FILTER_v1.md。**

做法同上一份:不改引擎,把不满足过滤的日子上的 `cost_z` 置 nan,其余原样走 `replay`。

用法:CSV_DIR=research/data python research/run_sa_entry_filter.py
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
POS_N = 10                      # 位置窗口,预注册固定
QS, Q_REP = [0.4, 0.5, 0.6], 0.5
RS, R_REP = [0.5, 0.6, 0.7], 0.6
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
    sig = H.attach_cost_signal(H.signal_series(seat, groups), seat, mkt, groups)
    idx = mkt.index
    m = price.set_index(["contract", "trade_date"])
    hi, lo = [], []
    for d, row in mkt.iterrows():
        c = row["main"]
        if isinstance(c, str) and (c, d) in m.index:
            hi.append(float(m.loc[(c, d), "high_price"]))
            lo.append(float(m.loc[(c, d), "low_price"]))
        else:
            hi.append(np.nan)
            lo.append(np.nan)
    hi, lo = pd.Series(hi, index=idx), pd.Series(lo, index=idx)
    H_, L_ = hi.rolling(POS_N).max(), lo.rolling(POS_N).min()
    pos = ((mkt["settle"] - L_) / (H_ - L_)).clip(0, 1)
    oi = price.groupby("trade_date")["open_interest"].sum().reindex(idx)
    rdf, _h = H.retail_series(seat, idx)
    lvl = rdf["net"].rolling(250, min_periods=60).rank(pct=True)
    C[code] = {"seat": seat, "mkt": mkt, "op": op, "st": st, "sig": sig,
               "rdf": rdf, "pos": pos, "doi": oi.diff(), "lvl": lvl}
    print(f"{code} 预处理完成({len(idx)} 天)")


def run(code, kind=None, par=None, shift=0):
    c = C[code]
    H.use(code)
    sig = c["sig"].copy()
    cz = sig["cost_z"]
    if kind:
        if kind == "A":
            v = c["pos"]
        elif kind == "B":
            v = c["doi"]
        else:
            v = c["lvl"]
        if shift:
            v = pd.Series(np.roll(v.to_numpy(), shift), index=v.index)
        if kind == "A":
            ok = ((cz < 0) & (v >= par)) | ((cz > 0) & (v <= 1 - par))
        elif kind == "B":
            ok = (v > 0)
        else:
            ok = ((cz < 0) & (v >= par)) | ((cz > 0) & (v <= 1 - par))
        sig["cost_z"] = cz.where(ok & cz.notna() & v.notna(), np.nan)
    trades, _p, daily = H.replay(sig, c["mkt"], c["rdf"], c["op"], c["st"])
    p = H._perf(daily)
    return {"累计%": p["cum_pct"], "夏普": p["sharpe"] or 0.0,
            "回撤%": p["max_dd_pct"], "笔数": len(trades), "daily": daily}


def show(tag, r):
    print(f"  {tag:<22}{r['累计%']:>+10.1f}%{r['夏普']:>8.2f}{r['回撤%']:>9.1f}%{r['笔数']:>7}")


RES = {}
for code, name in (("SA", "纯碱"), ("FG", "玻璃")):
    print(f"\n{'='*74}\n=== {name} {code} ===")
    base = run(code)
    print(f"  {'方案':<20}{'累计':>11}{'夏普':>8}{'回撤':>10}{'笔数':>7}")
    show("基线", base)
    grids = {"A 进场位置": ("A", QS, Q_REP), "C 散户水平": ("C", RS, R_REP)}
    store = {}
    for label, (kind, grid, rep) in grids.items():
        store[kind] = {}
        for q in grid:
            store[kind][q] = run(code, kind, q)
            show(f"{label} {q}", store[kind][q])
    store["B"] = {None: run(code, "B", None)}
    show("B 持仓量增仓", store["B"][None])
    RES[code] = {"base": base, "store": store}

    for label, kind, grid, rep, alpha in (("A 进场位置", "A", QS, Q_REP, 0.05),
                                          ("C 散户水平", "C", RS, R_REP, 0.025)):
        print(f"\n  —— {label} ——")
        good = [q for q in grid
                if store[kind][q]["夏普"] >= base["夏普"]
                and store[kind][q]["累计%"] >= base["累计%"]]
        print(f"  G1 相邻档同向:达标 {good} → {'过' if len(good) >= 2 else '不过'}")
        r = store[kind][rep]
        print(f"  G3 笔数:{r['笔数']}/{base['笔数']} = {r['笔数']/base['笔数']*100:.0f}%"
              f"  {'过' if r['笔数'] >= base['笔数']*0.6 else '不过'}")
        d, db = r["daily"].fillna(0), base["daily"].fillna(0)
        yb = ((1+db).groupby(db.index.year).prod()-1)*100
        yr = ((1+d).groupby(d.index.year).prod()-1)*100
        w = sum(1 for y in yb.index if y in yr.index and yr[y] >= yb[y])
        print(f"  G2 逐年:{w}/{len(yb)} = {w/len(yb)*100:.0f}%"
              f"  {'过' if w/len(yb) >= 4/6 else '不过'}")
        mid = d.index[len(d)//2]
        hn = ((1+d[d.index >= mid]).prod()-1)*100
        hb = ((1+db[db.index >= mid]).prod()-1)*100
        print(f"  G4 后半:{hn:+.1f}% vs 基线 {hb:+.1f}%"
              f"  {'过' if (hn >= 0 and hn >= hb) else '不过'}")
        obs = r["夏普"] - base["夏普"]
        n = len(C[code]["mkt"])
        draws = np.array([run(code, kind, rep, shift=int(RNG.integers(20, n-20)))["夏普"]
                          - base["夏普"] for _ in range(300)])
        p5 = float((draws >= obs).mean())
        print(f"  G5 安慰剂:提升 {obs:+.3f}, p={p5:.3f} (需 <{alpha})"
              f"  {'过' if p5 < alpha else '不过'}")

    rb = store["B"][None]
    print(f"\n  —— B 持仓量增仓 ——")
    print(f"  夏普 {rb['夏普']:.2f} vs 基线 {base['夏普']:.2f};"
          f"笔数 {rb['笔数']}/{base['笔数']} = {rb['笔数']/base['笔数']*100:.0f}%")

print("\n=== G6 两品种同向(代表格夏普提升的符号)===")
for label, kind, rep in (("A 进场位置", "A", Q_REP), ("C 散户水平", "C", R_REP),
                         ("B 持仓量", "B", None)):
    d = {}
    for code in ("SA", "FG"):
        b = RES[code]["base"]["夏普"]
        d[code] = RES[code]["store"][kind][rep]["夏普"] - b
    same = np.sign(d["SA"]) == np.sign(d["FG"])
    print(f"  {label}: 纯碱 {d['SA']:+.3f}  玻璃 {d['FG']:+.3f}  "
          f"→ {'同向,过' if same else '符号相反,不过'}")

print("\n判定按 PLAN_SA_ENTRY_FILTER_v1 第五节执行,本脚本不下结论。")
