"""方正中期、中信建投算不算散户席位。

运营者 2026-08-19 追问。它们在**全样本**垫底榜上很显眼(方正中期 5 个品种、
中信建投 3 个),却没进 v2 名单——因为名单是用 2021 年前的数据选的。

**这里有个方法论陷阱要避开**:如果我拿全样本看出它们是散户、再加进名单去测
"样本外",那是前视偏差,测出来的好看数字不算数。所以分三步:
  ① 看持仓方向(散户天然净多)——这条相对稳定,不太受时点影响
  ② 看**早期数据**里它们是什么样——若早期就已是净多且亏钱,加进去才不算作弊
  ③ 加进去测,并如实标注前视风险的程度
"""
from __future__ import annotations
import numpy as np, pandas as pd
import lhlib as L
from run_flow_skill import build, seat_alpha, power

CODES = ["AU", "AG", "LH", "FG", "SA"]
CUT = pd.Timestamp("2021-01-01")
data = {c: build(c) for c in CODES}
V2 = ["东方财富", "中衍期货", "平安期货", "徽商期货"]
ASK = ["方正中期", "中信建投"]

print("① 持仓方向(散户天然净多)")
print(f"  {'席位':8s}" + "".join(f"{c:>10s}" for c in CODES))
for m in V2 + ASK:
    row = ""
    for c in CODES:
        df, _ = data[c]
        sub = df[df["member_key"] == m]
        row += f"{sub['net'].mean():>10,.0f}" if len(sub) > 500 else f"{'—':>10s}"
    tag = "  ← 待测" if m in ASK else ""
    print(f"  {m:8s}{row}{tag}")

print("\n② 早期(2021 前)vs 后期(2021 后):它们是一直亏,还是后来才亏")
print(f"  {'席位':8s}{'品种':6s}{'早期alpha':>11s}{'早期分位':>10s}{'后期alpha':>11s}{'后期分位':>10s}")
for m in ASK:
    for c in CODES:
        df, _ = data[c]
        e = seat_alpha(df[df["trade_date"] < CUT], c, min_days=150)
        l = seat_alpha(df[df["trade_date"] >= CUT], c, min_days=150)
        if m not in e.index and m not in l.index: continue
        ea = e.loc[m, "alpha"] if m in e.index else np.nan
        ep = e["alpha"].rank(pct=True)[m] if m in e.index else np.nan
        la = l.loc[m, "alpha"] if m in l.index else np.nan
        lp = l["alpha"].rank(pct=True)[m] if m in l.index else np.nan
        f = lambda v, p: (f"{v:>11.1f}{p:>10.2f}" if np.isfinite(v) else f"{'—':>11s}{'—':>10s}")
        print(f"  {m:8s}{c:6s}{f(ea,ep)}{f(la,lp)}")

def sig_for(code, members, lo=CUT):
    df, main = data[code]
    te = df[df["trade_date"] >= lo]
    have = [m for m in members if m in set(te["member_key"])]
    if len(have) < 2: return None, None, []
    s = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    dm = (s - s.rolling(120, min_periods=60).mean()) / s.rolling(120, min_periods=60).std()
    return dm, main[main.index >= lo], have

print("\n③ 加进名单测(去均值 t)。⚠ 它们是从全样本垫底榜上看出来的,")
print("   所以下面这些数字**带前视偏差**,不能与 v2 那份等同看待。")
print(f"  {'品种':6s}{'v2(4家)':>10s}{'+方正中期':>11s}{'+中信建投':>11s}{'+两家':>9s}")
for c in CODES:
    out = []
    for members in (V2, V2 + ["方正中期"], V2 + ["中信建投"], V2 + ASK):
        s, m, have = sig_for(c, members)
        r = power(s, m) if s is not None else None
        out.append(r[1] if r else np.nan)
    print(f"  {c:6s}" + "".join(f"{v:>+10.2f} " if np.isfinite(v) else f"{'—':>10s} " for v in out))

print("\n④ 无前视的版本:只用早期数据能不能选出它们")
print("   (把门槛从「≥3 个品种后 20%」放宽到「≥2 个品种后 25%」,看谁会进来)")
early = {}
for c in CODES:
    df, _ = data[c]
    a = seat_alpha(df[df["trade_date"] < CUT], c, min_days=150)
    if not a.empty: early[c] = a["alpha"].rank(pct=True)
votes = {}
for c, r in early.items():
    for m in r.index:
        votes.setdefault(m, []).append((c, r[m]))
rows = []
for m, vs in votes.items():
    n_bad = sum(1 for _, p in vs if p <= 0.25)
    if n_bad >= 2:
        rows.append((m, n_bad, len(vs), ", ".join(f"{c}{p:.2f}" for c, p in vs if p <= 0.25)))
t = pd.DataFrame(rows, columns=["席位", "后25%品种数", "有样本品种数", "明细"]).sort_values(
    "后25%品种数", ascending=False)
print(t.head(12).to_string(index=False))
print(f"\n  方正中期是否入选:{'是' if '方正中期' in set(t['席位']) else '否'}")
print(f"  中信建投是否入选:{'是' if '中信建投' in set(t['席位']) else '否'}")

# ---------------------------------------------------------------- ⑤
# ③ 显示加人反而变差。要分清是「这两家特殊」还是「名单本来就不该太大」——
# 前者说明它们不合群,后者说明信号的信噪比随人数下降。测法:从 v2 四家里
# 逐个去掉(减到 3 家)、以及加入别的候选(增到 5 家),看规模本身的影响。
print("\n⑤ 名单规模本身的影响:少一家 / 多一家")
def t_of(members):
    out = {}
    for c in CODES:
        s, m, have = sig_for(c, members)
        if s is None: continue
        r = power(s, m)
        if r: out[c] = r[1]
    return out
base = t_of(V2)
print(f"  {'名单':28s}" + "".join(f"{c:>8s}" for c in CODES))
print(f"  {'v2 四家(基准)':28s}" + "".join(f"{base.get(c,float('nan')):>+8.2f}" for c in CODES))
print("  —— 减到 3 家 ——")
for drop in V2:
    o = t_of([m for m in V2 if m != drop])
    print(f"  {'去掉 ' + drop:28s}" + "".join(f"{o.get(c,float('nan')):>+8.2f}" for c in CODES))
print("  —— 加到 5 家(候选来自早期无前视名单) ——")
for add in ["中信建投", "方正中期", "长江期货", "国泰君安"]:
    o = t_of(V2 + [add])
    tag = "(套保/非散户,仅作对照)" if add == "国泰君安" else ""
    print(f"  {'加入 ' + add:28s}" + "".join(f"{o.get(c,float('nan')):>+8.2f}" for c in CODES) + tag)

print("\n⑥ 这两家单独当信号,自己灵不灵")
for m in ASK + ["东方财富"]:
    row = ""
    for c in CODES:
        df, main = data[c]
        te = df[df["trade_date"] >= CUT]
        if m not in set(te["member_key"]): row += f"{'—':>8s}"; continue
        s = -te[te["member_key"] == m].groupby("trade_date")["net"].sum().sort_index().diff(5)
        dm = (s - s.rolling(120, min_periods=60).mean()) / s.rolling(120, min_periods=60).std()
        r = power(dm, main[main.index >= CUT])
        row += f"{r[1]:>+8.2f}" if r else f"{'—':>8s}"
    note = "  ← 待测" if m in ASK else "  ← 名单核心,作对照"
    print(f"  {m:8s}" + "".join(f"{c:>0s}" for c in []) + row + note)
print(f"  (列顺序:{'  '.join(CODES)})")
