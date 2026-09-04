# -*- coding: utf-8 -*-
"""PLAN_FOLLOW_V2_v1 的跑数脚本 —— 玻纯跟随卡能不能变成可下单的东西。

**先读 PLAN_FOLLOW_V2_v1.md。** 闸门与结局处置在那边事前钉死。

与上一轮(run_follow_cost.py)的三处差别,全部来自预注册第三节:
  1. **允许同向态建仓**(拆掉 DEC-142 的状态门);
  2. **预热期写死**:强度分母历史不足 250 个交易日时不建仓;
  3. 参数格子 H2 阈值 {1,2,3,5}、H3 资金上限 {35%,20%,12%,8%},代表格 N=2 / use=20%。

成本 2 元/手/边,T+1 开盘成交,PIT 强度,持仓 net_off —— 与上一轮相同。

用法:CSV_DIR=research/data python research/run_follow_v2.py
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
CAP = 500000.0
MARGIN = {"FG": 0.09, "SA": 0.08}
MULT = {"FG": 20.0, "SA": 20.0}
FEE = 2.0
WARM = 250                      # 预热期,预注册写死
CARD = ["永安期货", "东证期货"]

PX, ST, MK, OP = {}, {}, {}, {}
for _c, _s in (("FG", "fg"), ("SA", "sa")):
    H.use(_c)
    PX[_c] = H.clean_price(pd.read_csv(DATA / f"{_s}_price.csv.gz"))
    ST[_c] = H.clean_seat(pd.read_csv(DATA / f"{_s}_seat.csv.gz"))
    MK[_c] = H.main_series(PX[_c])
    OP[_c], _ = H.contract_prices(PX[_c])
IDX = MK["FG"].index.intersection(MK["SA"].index)
IDX = IDX[IDX >= pd.Timestamp("2020-06-01")]
MAIN = {k: MK[k]["main"].reindex(IDX) for k in ("FG", "SA")}


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


def plan(net, d, gmax, use):
    fg, sa = net["FG"].get(d, np.nan), net["SA"].get(d, np.nan)
    if not (np.isfinite(fg) and np.isfinite(sa)) or fg == 0 or sa == 0:
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
    budget = CAP * use * strength
    sized = H._fit_within_budget(
        [(c, k, abs(u) * (budget / per)) for c, k, u in unit], budget,
        lambda c, n: n * MULT[kind[c]] * pxs[c] * MARGIN[kind[c]])
    if not sized:
        return {}
    return {(kind[c], c): (1 if u > 0 else -1) * sized[c] for c, _k, u in unit}


def run(m, use=0.20, thresh=0, fee=FEE, lo=None, hi=None):
    net = nets(m)
    g = net["FG"].abs().fillna(0) + net["SA"].abs().fillna(0)
    gmax = g.rolling("730D").max()
    pos, rows = {}, []
    for i in range(len(IDX) - 2):
        d, d1, d2 = IDX[i], IDX[i + 1], IDX[i + 2]
        if i < WARM or (lo is not None and d < lo) or (hi is not None and d >= hi):
            want = {}
        else:
            want = plan(net, d, gmax.iloc[i], use)
            if thresh:
                keep = {}
                for key in set(want) | set(pos):
                    a, b = pos.get(key, 0), want.get(key, 0)
                    keep[key] = a if (a and b and abs(b - a) < thresh) else b
                want = {k: v for k, v in keep.items() if v}
        pnl = sum(l * MULT[k] * (opx(k, c, d2) - opx(k, c, d1))
                  for (k, c), l in want.items()
                  if np.isfinite(opx(k, c, d1)) and np.isfinite(opx(k, c, d2)))
        cost = sum(abs(want.get(key, 0) - pos.get(key, 0)) * fee
                   for key in set(want) | set(pos))
        pos = want
        rows.append((d2, (pnl - cost) / CAP * 100))
    return pd.Series(dict(rows)).sort_index()


def stat(s):
    d = s.fillna(0) / 100
    eq = (1 + d).cumprod()
    n = max(int((d != 0).sum()), 1)
    return {"累计%": round((eq.iloc[-1] - 1) * 100, 1),
            "夏普": round(float(d.mean() / d.std() * np.sqrt(242)), 2)
            if d.std() > 0 else np.nan,
            "回撤%": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
            "有仓天": n}


print(f"样本 {IDX[0].date()} ~ {IDX[-1].date()};预热 {WARM} 日;"
      f"成本 {FEE:.0f} 元/手/边;代表格 use=20% / N=2\n")

print("=== 主数字(代表格)===")
S = {m: run(m) for m in CARD}
print(pd.DataFrame({m: stat(S[m]) for m in CARD}).T.to_string())

print("\n【G3 逐年】需 ≥4/6 正")
yr = pd.DataFrame({m: ((1 + S[m] / 100).groupby(S[m].index.year).prod() - 1) * 100
                   for m in CARD}).round(1)
print(yr.to_string())
for m in CARD:
    w = int((yr[m] > 0).sum())
    print(f"  {m}:{w}/{len(yr)}  {'过' if w >= 4 else '不过'}")

print("\n【G4 后半不塌】")
for m in CARD:
    s = S[m][S[m] != 0]
    mid = s.index[len(s) // 2]
    b = (1 + S[m][S[m].index >= mid] / 100).prod() - 1
    print(f"  {m}  后半 {b*100:+.1f}%  {'过' if b >= 0 else '不过'}(分界 {mid.date()})")

print("\n【G5 回撤 ≤35%】  【G6 去掉最赚 5 天仍 ≥0】  【G7 成本翻倍仍正】")
for m in CARD:
    st = stat(S[m])
    s = S[m]
    top = s[s != 0].nlargest(5)
    g6 = ((1 + s.drop(top.index) / 100).prod() - 1) * 100
    g7 = stat(run(m, fee=4.0))["累计%"]
    print(f"  {m}  回撤 {st['回撤%']}% {'过' if abs(st['回撤%']) <= 35 else '不过'}"
          f"   去5天 {g6:+.1f}% {'过' if g6 >= 0 else '不过'}"
          f"   成本翻倍 {g7:+.1f}% {'过' if g7 > 0 else '不过'}")

print("\n【G1 席位池安慰剂 · 拆门后重排】需前 25%")
pool = []
for m in sorted(set(ST["FG"].member_key) & set(ST["SA"].member_key)):
    s = run(m)
    if int((s != 0).sum()) < 200:
        continue
    pool.append({"席位": m, **stat(s)})
df = pd.DataFrame(pool).sort_values("累计%", ascending=False).reset_index(drop=True)
df.index += 1
print(f"  池子 = {len(df)} 家;前 10:")
print(df.head(10).to_string())
for m in CARD:
    if m in set(df["席位"]):
        r = int(df.index[df["席位"] == m][0])
        pct = (r - 1) / len(df) * 100
        print(f"  {m} 排 {r}/{len(df)} = 前 {pct:.0f}%  {'过' if pct < 25 else '不过'}")

print("\n【G2 走前挑人】每年年初只用此前数据挑,需跑赢池子中位数")
names = list(df["席位"])
curves = {m: run(m) for m in names}
years = sorted({d.year for d in IDX})[1:]
seg = []
for y in years:
    cut = pd.Timestamp(f"{y}-01-01")
    hist = {m: ((1 + curves[m][curves[m].index < cut] / 100).prod() - 1)
            for m in names}
    hist = {k: v for k, v in hist.items() if np.isfinite(v)}
    if not hist:
        continue
    pick = max(hist, key=hist.get)
    c = curves[pick]
    seg.append((y, pick, c[(c.index >= cut) & (c.index < pd.Timestamp(f"{y+1}-01-01"))]))
for y, pick, s in seg:
    print(f"  {y} 挑 {pick}  当年 {((1+s/100).prod()-1)*100:+.1f}%")
wf = pd.concat([s for _y, _p, s in seg])
med = df["累计%"].median()
print(f"  走前拼接 {stat(wf)['累计%']:+.1f}%   池子中位数 {med:+.1f}%   "
      f"{'过' if stat(wf)['累计%'] > med else '不过'}")

print("\n【H2 换手阈值】需 4 格里 ≥3 格优于 N=0")
rows = []
for m in CARD:
    base = stat(run(m, thresh=0))["累计%"]
    line = {"席位": m, "N=0": base}
    for n in (1, 2, 3, 5):
        line[f"N={n}"] = stat(run(m, thresh=n))["累计%"]
    line["优于N=0"] = sum(line[f"N={n}"] > base for n in (1, 2, 3, 5))
    rows.append(line)
print(pd.DataFrame(rows).to_string(index=False))

print("\n【H3 降杠杆】代表格 use=20% 需同时 回撤≤35% 且 累计>0")
rows = []
for m in CARD:
    line = {"席位": m}
    for u in (0.35, 0.20, 0.12, 0.08):
        st = stat(run(m, use=u))
        line[f"use={u:.0%}"] = f"{st['累计%']:+.1f}% / 回撤{st['回撤%']:.0f}%"
    rows.append(line)
print(pd.DataFrame(rows).to_string(index=False))

print("\n判定按 PLAN_FOLLOW_V2_v1 第五节执行,本脚本不下结论。")
