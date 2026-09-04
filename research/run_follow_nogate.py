# -*- coding: utf-8 -*-
"""把「对冲态」那道门拆掉,同向日也跟,T+1 之后还剩多少?

与 run_follow_cost.py 同一套口径(真实费率 2 元/手/边、PIT 强度、次日开盘成交),
唯一差别是允许同向日建仓。另报剔预热期(前 250 日)的版本。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
CAP, USE = 500000.0, 0.35
MARGIN = {"FG": 0.09, "SA": 0.08}
MULT = {"FG": 20.0, "SA": 20.0}
FEE = 2.0
MEM = ["永安期货", "东证期货"]


def load(code, stem):
    H.use(code)
    price = H.clean_price(pd.read_csv(D / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{stem}_seat.csv.gz"))
    return price, seat, H.main_series(price)


PX, ST, MK = {}, {}, {}
for c, s in (("FG", "fg"), ("SA", "sa")):
    PX[c], ST[c], MK[c] = load(c, s)
IDX = MK["FG"].index.intersection(MK["SA"].index)
IDX = IDX[IDX >= pd.Timestamp("2020-06-01")]
MAIN = {k: MK[k]["main"].reindex(IDX) for k in ("FG", "SA")}
OP = {}
for k in ("FG", "SA"):
    OP[k], _ = H.contract_prices(PX[k])


def opx(k, c, d):
    if not isinstance(c, str) or c not in OP[k].columns:
        return np.nan
    try:
        v = OP[k].at[d, c]
    except KeyError:
        return np.nan
    return float(v) if np.isfinite(v) else np.nan


def nets(m):
    return {k: (ST[k][ST[k].member_key == m].groupby("trade_date")["net_off"]
                .sum().reindex(IDX)) for k in ("FG", "SA")}


def plan(net, d, gmax, mode):
    fg, sa = net["FG"].get(d, np.nan), net["SA"].get(d, np.nan)
    if not (np.isfinite(fg) and np.isfinite(sa)) or fg == 0 or sa == 0:
        return {}
    same = fg * sa > 0
    if mode == "hedge" and same:
        return {}
    if mode == "same" and not same:
        return {}
    cf, cs = MAIN["FG"].get(d), MAIN["SA"].get(d)
    pf, ps = opx("FG", cf, d), opx("SA", cs, d)
    if not (np.isfinite(pf) and np.isfinite(ps)):
        return {}
    strength = min(1.0, (abs(fg) + abs(sa)) / gmax) if gmax and gmax > 0 else 1.0
    base = min(abs(fg), abs(sa))
    unit = [(cf, "FG", fg / base), (cs, "SA", sa / base)]
    pxs, kind = {cf: pf, cs: ps}, {cf: "FG", cs: "SA"}
    per = sum(abs(u) * MULT[k] * pxs[c] * MARGIN[k] for c, k, u in unit)
    if per <= 0:
        return {}
    budget = CAP * USE * strength
    sized = H._fit_within_budget(
        [(c, k, abs(u) * (budget / per)) for c, k, u in unit], budget,
        lambda c, n: n * MULT[kind[c]] * pxs[c] * MARGIN[kind[c]])
    if not sized:
        return {}
    return {(kind[c], c): (1 if u > 0 else -1) * sized[c] for c, _k, u in unit}


def run(m, mode="all"):
    net = nets(m)
    g = net["FG"].abs().fillna(0) + net["SA"].abs().fillna(0)
    gmax = g.rolling("730D").max()
    pos, rows = {}, []
    for i in range(len(IDX) - 2):
        d, d1, d2 = IDX[i], IDX[i + 1], IDX[i + 2]
        want = plan(net, d, gmax.iloc[i], mode)
        pnl = sum(l * MULT[k] * (opx(k, c, d2) - opx(k, c, d1))
                  for (k, c), l in want.items()
                  if np.isfinite(opx(k, c, d1)) and np.isfinite(opx(k, c, d2)))
        cost = sum(abs(want.get(key, 0) - pos.get(key, 0)) * FEE
                   for key in set(want) | set(pos))
        pos = want
        rows.append((d2, (pnl - cost) / CAP * 100))
    return pd.Series(dict(rows)).sort_index()


def stat(s):
    d = s.fillna(0) / 100
    eq = (1 + d).cumprod()
    return (round((eq.iloc[-1] - 1) * 100, 1),
            round((eq.iloc[-1] ** (242 / len(d)) - 1) * 100, 1),
            round(float(d.mean() / d.std() * np.sqrt(242)), 2) if d.std() > 0 else np.nan,
            round(float((eq / eq.cummax() - 1).min()) * 100, 1),
            int((d != 0).sum()))


print(f"样本 {IDX[0].date()} ~ {IDX[-1].date()}  成本 2 元/手/边  T+1 开盘成交\n")
hdr = f"{'方案':<30}{'累计%':>9}{'年化%':>8}{'夏普':>7}{'回撤%':>9}{'有仓天':>7}"
for tag, cut in (("全样本", 0), ("剔预热(前 250 日)", 250)):
    print(f"=== {tag} ===")
    print(hdr)
    for m in MEM:
        for mode, name in (("hedge", "只对冲态(现状)"),
                           ("same", "只同向"),
                           ("all", "两种都跟(拆掉门)")):
            s = run(m, mode).iloc[cut:]
            a, b, c, dd, n = stat(s)
            print(f"{m[:2]+' · '+name:<30}{a:>8.1f}%{b:>7.1f}%{c:>7}{dd:>8.1f}%{n:>7}")
    print()
