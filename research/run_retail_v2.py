"""散户名单 v2:剔掉套保席位,再测与聪明钱信号的叠加。

运营者 2026-08-19 两条:
  ① 去掉格林大华——**它是套保席位**,从生猪净空就能看出来。套保为交割锁价、
     不在乎盈亏,不算真正的散户。(数据也支持:去掉它生猪从 +2.84 升到 +4.53。)
  ② 测方向 C:散户反向信号能不能与各品种自己的聪明钱信号叠加。

①引出一个该一并做的事:名单里**其他几家会不会也是套保**。判据就是运营者给的——
**散户天然站多头**,一致净空的是套保。所以先按持仓方向把名单洗一遍。
"""
from __future__ import annotations
import numpy as np, pandas as pd
import lhlib as L
from run_flow_skill import build, seat_alpha, power

CODES = ["AU", "AG", "LH", "FG", "SA"]
CUT = pd.Timestamp("2021-01-01")
data = {c: build(c) for c in CODES}
CAND = ["东方财富", "中衍期货", "平安期货", "徽商期货", "格林大华", "长江期货"]

print("① 按持仓方向洗名单:散户天然净多,一致净空的是套保")
print(f"  {'席位':8s}" + "".join(f"{c:>10s}" for c in CODES) + "   判定")
keep = []
for m in CAND:
    nets = {}
    for c in CODES:
        df, _ = data[c]
        sub = df[df["member_key"] == m]
        nets[c] = sub["net"].mean() if len(sub) > 500 else np.nan
    vals = [v for v in nets.values() if np.isfinite(v)]
    n_long = sum(1 for v in vals if v > 0)
    verdict = "散户(留)" if n_long >= len(vals) - 1 and n_long >= 2 else "方向不一致(剔)"
    if "散户" in verdict: keep.append(m)
    print(f"  {m:8s}" + "".join(f"{nets[c]:>10,.0f}" if np.isfinite(nets[c]) else f"{'—':>10s}"
                                for c in CODES) + f"   {verdict}")
# 运营者明确点名剔除格林大华,即便自动判据放它过也要剔
if "格林大华" in keep:
    keep.remove("格林大华")
    print("  (格林大华由运营者点名剔除:套保席位,为交割锁价不在乎盈亏)")
print(f"\n  v2 名单:{'、'.join(keep)}")

def retail_sig(code, members, lo=CUT):
    df, main = data[code]
    te = df[df["trade_date"] >= lo]
    have = [m for m in members if m in set(te["member_key"])]
    if len(have) < 2: return None, None, 0
    s = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    return s, main[main.index >= lo], len(have)

print("\n② v2 名单的样本外表现(对照 v1)")
print(f"  {'品种':6s}{'v1 t':>8s}{'v2 t':>8s}{'v2 去均值':>10s}   逐年")
for c in CODES:
    s1, m1, _ = retail_sig(c, CAND)
    s2, m2, n2 = retail_sig(c, keep)
    if s1 is None or s2 is None: continue
    r1, r2 = power(s1, m1), power(s2, m2)
    dm = (s2 - s2.rolling(120, min_periods=60).mean()) / s2.rolling(120, min_periods=60).std()
    r3 = power(dm, m2)
    j = pd.concat([s2.rename("sig"), m2], axis=1, sort=True); j["y"] = j.index.year
    marks = []
    for y, g in j.groupby("y"):
        rr = power(g["sig"], g)
        if rr: marks.append(f"{y}{'+' if rr[0] > 0 else '-'}")
    if r1 and r2 and r3:
        print(f"  {c:6s}{r1[1]:>+8.2f}{r2[1]:>+8.2f}{r3[1]:>+10.2f}   {' '.join(marks)}")

print("\n③ 方向 C:散户反向 × 聪明钱,两者叠加会不会更强")
print("   聪明钱=各品种自己 alpha 前 5(只用 CUT 之前的数据选,不看未来)")
for c in CODES:
    df, main = data[c]
    tr = seat_alpha(df[df["trade_date"] < CUT], c, min_days=150)
    if tr.empty or len(tr) < 5: 
        print(f"  {c}: 早期样本不足,跳过"); continue
    smart5 = tr.sort_values("alpha", ascending=False).head(5).index.tolist()
    te = df[df["trade_date"] >= CUT]
    smart = te[te["member_key"].isin(smart5)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    rsig, m, nh = retail_sig(c, keep)
    if rsig is None: continue
    j = pd.concat([smart.rename("smart"), rsig.rename("retail"), m], axis=1, sort=True).dropna()
    if len(j) < 200: continue
    both = np.sign(j["smart"]) == np.sign(j["retail"])
    # 共振时:两者都指同一个方向,用它们的和当信号
    zz = ((j["smart"] - j["smart"].mean()) / j["smart"].std()
          + (j["retail"] - j["retail"].mean()) / j["retail"].std())
    rs = power(j["smart"], j); rr = power(j["retail"], j); rc = power(zz, j)
    print(f"  {c}:")
    print(f"    只用聪明钱 t={rs[1]:+.2f}   只用散户反向 t={rr[1]:+.2f}   两者相加 t={rc[1]:+.2f}")
    for label, sub in [("  共振(同向)", j[both]), ("  背离(反向)", j[~both])]:
        r = power(sub["retail"], sub)
        if r: print(f"  {label}时只看散户反向 t={r[1]:+.2f}  N={r[2]}")

# ---------------------------------------------------------------- ④
# 生猪席位数据 2023-08 才开始,用 CUT=2021 选不出早期聪明钱名单而被跳过。
# 它是唯一已上线的品种,叠加对它最有实际意义,所以单独用自己的时间轴测:
# 2023-08~2023-12 选人,2024 起做样本外。样本短,结论要打折看。
print("\n④ 生猪单独测叠加(自己的时间轴:2024-01 起样本外)")
LCUT = pd.Timestamp("2024-01-01")
df, main = data["LH"]
tr = seat_alpha(df[df["trade_date"] < LCUT], "LH", min_days=80)
if tr.empty or len(tr) < 5:
    print("  早期样本仍不足")
else:
    smart5 = tr.sort_values("alpha", ascending=False).head(5).index.tolist()
    print(f"  聪明钱(2024 前选):{'、'.join(smart5)}")
    te = df[df["trade_date"] >= LCUT]
    smart = te[te["member_key"].isin(smart5)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    have = [m for m in keep if m in set(te["member_key"])]
    rsig = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    print(f"  散户反向(v2 名单里在生猪上的 {len(have)} 家):{'、'.join(have)}")
    j = pd.concat([smart.rename("smart"), rsig.rename("retail"),
                   main[main.index >= LCUT]], axis=1, sort=True).dropna()
    both = np.sign(j["smart"]) == np.sign(j["retail"])
    rs, rr = power(j["smart"], j), power(j["retail"], j)
    print(f"    只用聪明钱 t={rs[1]:+.2f}   只用散户反向 t={rr[1]:+.2f}   N={len(j)}")
    for label, sub in [("共振(同向)", j[both]), ("背离(反向)", j[~both])]:
        r = power(sub["retail"], sub)
        if r: print(f"    {label}时只看散户反向 t={r[1]:+.2f}  N={r[2]}")

print("\n⑤ 汇总:三种用法在四个品种上的样本外 t")
print(f"  {'品种':6s}{'聪明钱':>9s}{'散户反向':>10s}{'共振时':>9s}   建议")
for c in ("AU", "FG", "SA"):
    df, main = data[c]
    tr = seat_alpha(df[df["trade_date"] < CUT], c, min_days=150)
    if tr.empty or len(tr) < 5: continue
    smart5 = tr.sort_values("alpha", ascending=False).head(5).index.tolist()
    te = df[df["trade_date"] >= CUT]
    smart = te[te["member_key"].isin(smart5)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    rsig, m, _ = retail_sig(c, keep)
    j = pd.concat([smart.rename("smart"), rsig.rename("retail"), m], axis=1, sort=True).dropna()
    both = np.sign(j["smart"]) == np.sign(j["retail"])
    rs, rr = power(j["smart"], j), power(j["retail"], j)
    rc = power(j[both]["retail"], j[both])
    best = max([("聪明钱", rs[1]), ("散户反向", rr[1]), ("共振", rc[1])], key=lambda x: x[1])
    print(f"  {c:6s}{rs[1]:>+9.2f}{rr[1]:>+10.2f}{rc[1]:>+9.2f}   用「{best[0]}」")
