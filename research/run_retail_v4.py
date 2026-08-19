"""散户名单 v4:三家种子 + 第四家选谁。

运营者 2026-08-19 拍板:
  - 种子固定三家:东方财富、平安期货、徽商期货
  - 去掉中衍期货——除 AU/AG 外持仓太小(FG 407 手、SA 58 手、LH 无),作用不大
  - 第四家在方正中期 / 中信建投里二选一

评判不能只看 t 高:还要看逐年符号稳不稳、极端档的区分度够不够,
以及去掉信号自身均值后活不活得下来(排除伪装的方向 beta)。
"""
from __future__ import annotations
import numpy as np, pandas as pd
import lhlib as L
from run_flow_skill import build, seat_alpha, power

CODES = ["AU", "AG", "LH", "FG", "SA"]
CUT = pd.Timestamp("2021-01-01")
data = {c: build(c) for c in CODES}
SEED = ["东方财富", "平安期货", "徽商期货"]
OPTS = {"三家种子": SEED,
        "种子+方正中期": SEED + ["方正中期"],
        "种子+中信建投": SEED + ["中信建投"],
        "(旧 v2:含中衍)": ["东方财富", "中衍期货", "平安期货", "徽商期货"]}


def signal(code, members, demean=True):
    df, main = data[code]
    te = df[df["trade_date"] >= CUT]
    have = [m for m in members if m in set(te["member_key"])]
    if len(have) < 2: return None, None, have
    s = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    if demean:
        s = (s - s.rolling(120, min_periods=60).mean()) / s.rolling(120, min_periods=60).std()
    return s, main[main.index >= CUT], have


print("① 各组合的样本外 t(去均值)")
print(f"  {'组合':16s}" + "".join(f"{c:>8s}" for c in CODES) + "   化工农产小计")
res = {}
for name, members in OPTS.items():
    row, agri = "", []
    for c in CODES:
        s, m, have = signal(c, members)
        r = power(s, m) if s is not None else None
        t = r[1] if r else np.nan
        res[(name, c)] = t
        row += f"{t:>+8.2f}" if np.isfinite(t) else f"{'—':>8s}"
        if c in ("LH", "FG", "SA") and np.isfinite(t): agri.append(t)
    print(f"  {name:16s}{row}   {np.mean(agri):+.2f}")

print("\n② 逐年符号(只看化工农产 LH/FG/SA,贵金属那条线本来就弱)")
for name, members in OPTS.items():
    marks, pos, tot = [], 0, 0
    for c in ("LH", "FG", "SA"):
        s, m, have = signal(c, members)
        if s is None: continue
        j = pd.concat([s.rename("sig"), m], axis=1, sort=True); j["y"] = j.index.year
        cm = []
        for y, g in j.groupby("y"):
            r = power(g["sig"], g)
            if r:
                cm.append("+" if r[0] > 0 else "-"); tot += 1; pos += 1 if r[0] > 0 else 0
        marks.append(f"{c}:{''.join(cm)}")
    print(f"  {name:16s} {'  '.join(marks)}   → {pos}/{tot} 正")

print("\n③ 极端档区分度(最正档 − 最负档,单位:百分点)")
print(f"  {'组合':16s}" + "".join(f"{c:>9s}" for c in ("LH", "FG", "SA")))
for name, members in OPTS.items():
    row = ""
    for c in ("LH", "FG", "SA"):
        s, m, have = signal(c, members)
        if s is None: row += f"{'—':>9s}"; continue
        j = pd.concat([s.rename("sig"), m], axis=1, sort=True).dropna()
        if len(j) < 200: row += f"{'—':>9s}"; continue
        j["b"] = pd.qcut(j["sig"], 5, labels=list("12345"))
        g = j.groupby("b", observed=True)["fwd"].mean() * 100
        row += f"{g.iloc[-1]-g.iloc[0]:>+9.2f}"
    print(f"  {name:16s}{row}")

print("\n④ 留一稳健:在最优组合里逐个去掉一家")
best = max([(n, np.mean([res[(n, c)] for c in ("LH", "FG", "SA")
                         if np.isfinite(res[(n, c)])])) for n in OPTS if "旧" not in n],
           key=lambda x: x[1])
print(f"  (按化工农产均值,最优是「{best[0]}」{best[1]:+.2f})")
for drop in OPTS[best[0]]:
    sub = [m for m in OPTS[best[0]] if m != drop]
    row = ""
    for c in CODES:
        s, m, have = signal(c, sub)
        r = power(s, m) if s is not None else None
        row += f"{r[1]:>+8.2f}" if r else f"{'—':>8s}"
    print(f"  去掉 {drop:8s}{row}")

print("\n⑤ 两个候选各自在哪些品种上有量(手,平均净持仓)")
print(f"  {'席位':8s}" + "".join(f"{c:>9s}" for c in CODES))
for m in ["东方财富", "平安期货", "徽商期货", "中衍期货", "方正中期", "中信建投"]:
    row = ""
    for c in CODES:
        df, _ = data[c]
        sub = df[df["member_key"] == m]
        row += f"{sub['net'].mean():>9,.0f}" if len(sub) > 500 else f"{'—':>9s}"
    print(f"  {m:8s}{row}")
