# -*- coding: utf-8 -*-
"""PLAN_FOLLOW_COST_v1 的跑数脚本 —— 玻纯跟随卡在真实手续费下能不能用。

**先读 PLAN_FOLLOW_COST_v1.md,再读本文件。** 闸门与结局处置在那边事前钉死,
这里只负责如实算。

成本口径:**开仓 2 元/手、平仓 2 元/手**(运营者 2026-09-04 给的真实费率)。
加仓是开仓、减仓是平仓,单边都是 2 元,所以 cost = Σ|手数变动| × 2,不分方向。
上一版按「名义 × 0.05%」算,那是真实值的 4.8~5.3 倍。

用法:
    CSV_DIR=research/data python research/run_follow_cost.py
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
CAP, USE = 500000.0, 0.35
MARGIN = {"FG": 0.09, "SA": 0.08}
MULT = {"FG": 20.0, "SA": 20.0}
FEE = 2.0                      # 元/手/边(开或平都是 2)
CARD = ["永安期货", "东证期货"]


def load(code, stem):
    H.use(code)
    price = H.clean_price(pd.read_csv(DATA / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(DATA / f"{stem}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, _st = H.contract_prices(price)
    return mkt, op, seat


FG_MKT, FG_OP, FG_SEAT = load("FG", "fg")
SA_MKT, SA_OP, SA_SEAT = load("SA", "sa")
IDX = FG_MKT.index.intersection(SA_MKT.index)
IDX = IDX[IDX >= pd.Timestamp("2020-06-01")]
MAIN = {"FG": FG_MKT["main"].reindex(IDX), "SA": SA_MKT["main"].reindex(IDX)}
OPEN = {"FG": FG_OP, "SA": SA_OP}
SEAT = {"FG": FG_SEAT, "SA": SA_SEAT}


def net_of(member):
    return {k: (SEAT[k][SEAT[k].member_key == member]
                .groupby("trade_date")["net_off"].sum().reindex(IDX))
            for k in ("FG", "SA")}


def opx(k, c, d):
    if not isinstance(c, str) or c not in OPEN[k].columns:
        return np.nan
    try:
        v = OPEN[k].at[d, c]
    except KeyError:
        return np.nan
    return float(v) if np.isfinite(v) else np.nan


def plan_on(net, d, gmax):
    fg, sa = net["FG"].get(d, np.nan), net["SA"].get(d, np.nan)
    if not (np.isfinite(fg) and np.isfinite(sa)) or fg * sa >= 0:
        return {}
    cf, cs = MAIN["FG"].get(d), MAIN["SA"].get(d)
    pf, ps = opx("FG", cf, d), opx("SA", cs, d)
    if not (np.isfinite(pf) and np.isfinite(ps)):
        return {}
    strength = min(1.0, (abs(fg) + abs(sa)) / gmax) if gmax and gmax > 0 else 1.0
    base = min(abs(fg), abs(sa))
    unit = [(cf, "FG", fg / base), (cs, "SA", sa / base)]
    pxs = {cf: pf, cs: ps}
    kind = {cf: "FG", cs: "SA"}
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


def run(member, fee=FEE, thresh=0):
    """返回逐日收益率(%)。thresh = 手数变动小于它就不动(H2)。"""
    net = net_of(member)
    gross = net["FG"].abs().fillna(0) + net["SA"].abs().fillna(0)
    gmax = gross.rolling("730D").max()          # PIT,只用截至当日
    pos, rows = {}, []
    for i in range(len(IDX) - 2):
        d, d1, d2 = IDX[i], IDX[i + 1], IDX[i + 2]
        want = plan_on(net, d, gmax.iloc[i])
        if thresh:
            # 变动不足阈值就沿用昨天那一腿(**空仓/开仓不受阈值保护**:
            # 从无到有、从有到无是状态变化,不是微调)
            keep = {}
            for key in set(want) | set(pos):
                a, b = pos.get(key, 0), want.get(key, 0)
                keep[key] = a if (a and b and abs(b - a) < thresh) else b
            want = {k: v for k, v in keep.items() if v}
        pnl = 0.0
        for (k, c), lots in want.items():
            a, b = opx(k, c, d1), opx(k, c, d2)
            if np.isfinite(a) and np.isfinite(b):
                pnl += lots * MULT[k] * (b - a)
        cost = sum(abs(want.get(key, 0) - pos.get(key, 0)) * fee
                   for key in set(want) | set(pos))
        pos = want
        rows.append((d2, (pnl - cost) / CAP * 100))
    return pd.Series(dict(rows)).sort_index()


def stats(s):
    d = s.fillna(0) / 100
    eq = (1 + d).cumprod()
    return {"累计%": round((eq.iloc[-1] - 1) * 100, 1),
            "年化%": round((eq.iloc[-1] ** (242 / len(d)) - 1) * 100, 1),
            "夏普": round(float(d.mean() / d.std() * np.sqrt(242)), 2)
            if d.std() > 0 else np.nan,
            "回撤%": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
            "有仓天": int((d != 0).sum())}


def yearly(s):
    return ((1 + s / 100).groupby(s.index.year).prod() - 1) * 100


print(f"样本 {IDX[0].date()} ~ {IDX[-1].date()},{len(IDX)} 个共同交易日")
print(f"成本 开/平各 {FEE:.0f} 元/手\n")

print("=== 主数字(H1)===")
S = {m: run(m) for m in CARD}
print(pd.DataFrame({m: stats(S[m]) for m in CARD}).T.to_string())

print("\n=== 对照:上一版的错误成本(名义×0.05%)长什么样 ===")
print("  (仅供说明口径差异,不参与判定)")

print("\n【G2 逐年】正收益年份需 ≥ 4/7")
yr = pd.DataFrame({m: yearly(S[m]) for m in CARD}).round(1)
print(yr.to_string())
for m in CARD:
    w = int((yr[m] > 0).sum())
    print(f"  {m}:{w}/7  {'过' if w >= 4 else '不过'}")

print("\n【G3 后半不塌】后半累计需 ≥ 0")
for m in CARD:
    s = S[m]
    mid = s.index[len(s) // 2]
    a = (1 + s[s.index < mid] / 100).prod() - 1
    b = (1 + s[s.index >= mid] / 100).prod() - 1
    print(f"  {m}  前半 {a*100:+.1f}%  后半 {b*100:+.1f}%  "
          f"{'过' if b >= 0 else '不过'}(分界 {mid.date()})")

print("\n【G4 成本翻倍】开/平各 4 元仍需为正")
for m in CARD:
    v = stats(run(m, fee=4.0))["累计%"]
    print(f"  {m}  {v:+.1f}%  {'过' if v > 0 else '不过'}")

print("\n【G5 样本不稀疏】有仓天数需 ≥ 400   【G6 回撤】需 ≤ 35%")
for m in CARD:
    st = stats(S[m])
    print(f"  {m}  有仓 {st['有仓天']} 天 {'过' if st['有仓天'] >= 400 else '不过'}"
          f"   回撤 {st['回撤%']}% {'过' if abs(st['回撤%']) <= 35 else '不过'}")

print("\n【G1 席位池安慰剂 · 核心】永安/东证需落在分布前 25%")
pool = []
mem = set(FG_SEAT.member_key) & set(SA_SEAT.member_key)
for m in sorted(mem):
    s = run(m)
    n = int((s != 0).sum())
    if n < 200:
        continue
    st = stats(s)
    pool.append({"席位": m, **st})
df = pd.DataFrame(pool).sort_values("累计%", ascending=False).reset_index(drop=True)
df.index += 1
print(f"  池子(有仓 ≥200 天)= {len(df)} 家")
print(df.head(12).to_string())
for m in CARD:
    if m in set(df["席位"]):
        r = int(df.index[df["席位"] == m][0])
        pct = (r - 1) / len(df) * 100
        print(f"  {m} 排 {r}/{len(df)} = 前 {pct:.0f}%  "
              f"{'过' if pct < 25 else '不过'}")
    else:
        print(f"  {m} 未进池(有仓不足 200 天)")

print("\n【H2 换手阈值】4 格,代表格 N=2;需 ≥3 格优于 N=0")
rows = []
for m in CARD:
    base = stats(S[m])["累计%"]
    line = {"席位": m, "N=0": base}
    for n in (1, 2, 3, 5):
        line[f"N={n}"] = stats(run(m, thresh=n))["累计%"]
    line["优于N=0的格数"] = sum(line[f"N={n}"] > base for n in (1, 2, 3, 5))
    rows.append(line)
print(pd.DataFrame(rows).to_string(index=False))

print("\n判定按 PLAN_FOLLOW_COST_v1 第五节执行,本脚本不下结论。")
