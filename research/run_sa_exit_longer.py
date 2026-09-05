# -*- coding: utf-8 -*-
"""PLAN_SA_EXIT_LONGER_v1 的跑数脚本 —— 去掉散户那一路出场 + 放宽 max_hold。

**先读 PLAN_SA_EXIT_LONGER_v1.md。**

口径:不改 `replay`。「去掉散户出场」= 把 `retail["rz"]` 整条置 NaN
(反向与消退都靠 `np.isfinite(z)`,NaN 会让两条同时跳过,止损/持满/交割不受影响)。
`max_hold` 只改 `RULES["max_hold"]`。

用法:CSV_DIR=research/data python research/run_sa_exit_longer.py
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
HOLDS = [40, 60, 80]
H_REP = 60                      # 代表格,预注册写死
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
    C[code] = {"mkt": mkt, "op": op, "st": st, "sig": sig, "rdf": rdf,
               "price": price, "hold0": H.RULES["max_hold"]}
    print(f"{code} 预处理完成({len(mkt)} 天,散户 = {'/'.join(have)},"
          f"现行 max_hold = {H.RULES['max_hold']})")


def run(code, drop_retail, hold, lo=None, hi=None):
    c = C[code]
    H.use(code)
    H.RULES["max_hold"] = hold
    rdf = c["rdf"].copy()
    if drop_retail:
        rdf["rz"] = pd.Series(np.nan, index=rdf.index)
    trades, _pos, daily = H.replay(c["sig"], c["mkt"], rdf, c["op"], c["st"])
    H.RULES["max_hold"] = c["hold0"]
    d = daily
    if lo is not None:
        d = d[(d.index >= lo) & (d.index <= hi)]
        trades = [t for t in trades
                  if lo <= pd.Timestamp(t["entry_date"]) <= hi]
    p = H._perf(d)
    return {"累计%": p["cum_pct"], "夏普": p["sharpe"] or 0.0, "回撤%": p["max_dd_pct"],
            "笔数": len(trades), "daily": d, "trades": trades}


def hold_med(r):
    return float(np.median([t["hold_days"] for t in r["trades"]])) if r["trades"] else np.nan


def exit_score(code, r):
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
        hi_, lo_ = float(w["high_price"].max()), float(w["low_price"].min())
        if hi_ <= lo_:
            continue
        p = min(1.0, max(0.0, (t["exit_px"] - lo_) / (hi_ - lo_)))
        s.append((1 - p) if t["side"] == "short" else p)
    return float(np.median(s)) if s else np.nan


def show(tag, code, r):
    return (f"  {tag:<24}{r['累计%']:>+9.1f}%{r['夏普']:>7.2f}{r['回撤%']:>8.1f}%"
            f"{r['笔数']:>6}{hold_med(r):>9.0f}{exit_score(code, r):>10.2f}")


RES = {}
for code, name in (("SA", "纯碱"), ("FG", "玻璃")):
    print(f"\n{'='*90}\n=== {name} {code} ===")
    base = run(code, False, 40)
    arms = {1: run(code, True, 40), 2: run(code, True, 60), 3: run(code, True, 80),
            0: run(code, False, 60)}

    # 自检(预注册第三节)
    rs = {t["exit_reason"] for t in arms[1]["trades"]}
    assert "反向" not in rs and "消退" not in rs, f"{code} 臂1 仍有散户出场:{rs}"
    print(f"  自检通过:臂1 的出场原因只剩 {sorted(rs)}")

    print(f"  {'臂':<22}{'累计':>10}{'夏普':>7}{'回撤':>9}{'笔数':>6}{'中位持仓日':>10}{'出场点得分':>11}")
    print(show("基线(现行,持满 40)", code, base))
    print(show("臂1 去散户,持满 40", code, arms[1]))
    print(show("臂2 去散户,持满 60", code, arms[2]) + "   ← 代表格")
    print(show("臂3 去散户,持满 80", code, arms[3]))
    print(show("臂0 留散户,持满 60", code, arms[0]) + "   ← 效应分离对照")
    RES[code] = {"base": base, "arms": arms}
    rep = arms[2]

    print(f"\n  —— 两个差分必须分开看(预注册第二节)——")
    print(f"    「去散户」净效应  臂2 − 臂0:夏普 {rep['夏普']-arms[0]['夏普']:+.3f}  "
          f"累计 {rep['累计%']-arms[0]['累计%']:+.1f}pp")
    print(f"    「放宽持满」净效应 臂2 − 臂1:夏普 {rep['夏普']-arms[1]['夏普']:+.3f}  "
          f"累计 {rep['累计%']-arms[1]['累计%']:+.1f}pp")

    print(f"\n  【G1 相邻档同向】臂1/2/3 中 ≥2 条夏普与累计都 ≥ 基线")
    good = [k for k in (1, 2, 3) if arms[k]["夏普"] >= base["夏普"]
            and arms[k]["累计%"] >= base["累计%"]]
    print(f"    达标 {good} → {'过' if len(good) >= 2 else '不过'}")

    print(f"\n  【G2 逐年】代表格 ≥ 基线的年份需 ≥ 4/6 比例")
    d, db = rep["daily"].fillna(0), base["daily"].fillna(0)
    yb = ((1 + db).groupby(db.index.year).prod() - 1) * 100
    yr = ((1 + d).groupby(d.index.year).prod() - 1) * 100
    win = 0
    for y in yb.index:
        ok = yr.get(y, -1e9) >= yb[y]
        win += ok
        print(f"    {y}  基线 {yb[y]:+7.1f}%   臂2 {yr.get(y, np.nan):+7.1f}%   {'✓' if ok else '✗'}")
    print(f"    {win}/{len(yb)} = {win/len(yb)*100:.0f}%  {'过' if win/len(yb) >= 4/6 else '不过'}")

    print(f"\n  【G4 后半不塌】")
    mid = d.index[len(d) // 2]
    hn = ((1 + d[d.index >= mid]).prod() - 1) * 100
    hbf = ((1 + db[db.index >= mid]).prod() - 1) * 100
    print(f"    臂2 后半 {hn:+.1f}%  基线后半 {hbf:+.1f}%  "
          f"{'过' if (hn >= 0 and hn >= hbf) else '不过'}(分界 {mid.date()})")

    print(f"\n  【G5 走前检验(核心)】前半挑 max_hold,后半验")
    idx = C[code]["mkt"].index
    cut = idx[len(idx) // 2]
    front = {h: run(code, True, h, idx[0], cut) for h in HOLDS}
    pick = max(HOLDS, key=lambda h: front[h]["夏普"])
    print("    前半各格夏普:" + "  ".join(f"{h}日 {front[h]['夏普']:+.2f}" for h in HOLDS)
          + f"  → 挑中 {pick} 日")
    back_new = run(code, True, pick, cut, idx[-1])
    back_base = run(code, False, 40, cut, idx[-1])
    print(f"    后半:挑中的 {pick} 日 {back_new['累计%']:+.1f}%/夏普 {back_new['夏普']:.2f}"
          f"   基线 {back_base['累计%']:+.1f}%/夏普 {back_base['夏普']:.2f}  "
          f"{'过' if back_new['夏普'] >= back_base['夏普'] else '不过'}")

    print(f"\n  【G6 效应分离】臂2 夏普需 ≥ 臂0(只放宽持满)")
    print(f"    {rep['夏普']:.2f} vs {arms[0]['夏普']:.2f} → "
          f"{'过' if rep['夏普'] >= arms[0]['夏普'] else '不过'}")

    print(f"\n  —— 另报:出场原因分布 ——")
    for tag, r in (("基线", base), ("臂2", rep)):
        cnt = pd.Series([t["exit_reason"] for t in r["trades"]]).value_counts()
        print(f"    {tag:<6}" + "  ".join(f"{k} {v}" for k, v in cnt.items()))

    if code in EPS:
        print(f"\n  —— 另报:运营者标的两轮里各臂实际出场日 ——")
        for nm, want in EPS[code]:
            w = pd.Timestamp(want)
            for tag, r in (("基线", base), ("臂2", rep), ("臂3", arms[3])):
                near = [t for t in r["trades"]
                        if abs((pd.Timestamp(t["exit_date"]) - w).days) <= 90]
                s = "、".join(f"{t['exit_date']}@{t['exit_px']:.0f}({t['exit_reason']})"
                             for t in near) or "该窗口无出场"
                print(f"    该在 {want} —— {tag:<5}{s}")

print(f"\n=== G3 两品种同向(臂2 − 基线 的夏普变化)===")
dd = {c: RES[c]["arms"][2]["夏普"] - RES[c]["base"]["夏普"] for c in ("SA", "FG")}
print(f"  纯碱 {dd['SA']:+.3f}   玻璃 {dd['FG']:+.3f}   "
      f"→ {'同向,过' if np.sign(dd['SA']) == np.sign(dd['FG']) else '符号相反,不过'}")

print("\n判定按 PLAN_SA_EXIT_LONGER_v1 第五节执行,本脚本不下结论。")
