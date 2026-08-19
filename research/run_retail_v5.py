"""方向 C(与聪明钱叠加),用运营者拍板的三家种子重测。

三家种子:东方财富、平安期货、徽商期货(2026-08-19 运营者定,去掉中衍期货)。

**这轮问得比上一轮实在**:上次只看 t 值,但 t 变高可能只是共振时段样本更干净,
不等于信号真的更强。所以三个指标一起看:
  ① t 值(统计显著性)
  ② **极端档收益差(百分点)**——这才是实际能吃到的
  ③ 共振时段占比——过滤掉一半机会换来一点提升,未必划算
"""
from __future__ import annotations
import numpy as np, pandas as pd
import lhlib as L
from run_flow_skill import build, seat_alpha, power

CODES = ["AU", "AG", "LH", "FG", "SA"]
CUT = pd.Timestamp("2021-01-01")
LH_CUT = pd.Timestamp("2024-01-01")      # 生猪席位 2023-08 才有,自己的时间轴
SEED = ["东方财富", "平安期货", "徽商期货"]
data = {c: build(c) for c in CODES}


def spread_pct(j, col):
    """极端档收益差:最正档 − 最负档,百分点。实际能吃到的就是这个。"""
    if len(j) < 150: return np.nan
    b = pd.qcut(j[col], 5, labels=list("12345"), duplicates="drop")
    g = j.groupby(b, observed=True)["fwd"].mean() * 100
    return g.iloc[-1] - g.iloc[0]


print(f"种子名单:{'、'.join(SEED)}\n")
print(f"  {'品种':5s}{'聪明钱t':>9s}{'散户t':>8s}{'共振t':>8s}{'背离t':>8s}"
      f"{'散户档差':>10s}{'共振档差':>10s}{'共振占比':>10s}")
for c in CODES:
    cut = LH_CUT if c == "LH" else CUT
    df, main = data[c]
    tr = seat_alpha(df[df["trade_date"] < cut], c, min_days=80 if c == "LH" else 150)
    if tr.empty or len(tr) < 5: continue
    smart5 = tr.sort_values("alpha", ascending=False).head(5).index.tolist()
    te = df[df["trade_date"] >= cut]
    have = [m for m in SEED if m in set(te["member_key"])]
    if len(have) < 2: continue
    smart = te[te["member_key"].isin(smart5)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    ret = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    ret = (ret - ret.rolling(120, min_periods=60).mean()) / ret.rolling(120, min_periods=60).std()
    j = pd.concat([smart.rename("smart"), ret.rename("retail"),
                   main[main.index >= cut]], axis=1, sort=True).dropna()
    if len(j) < 200: continue
    both = np.sign(j["smart"]) == np.sign(j["retail"])
    rs, rr = power(j["smart"], j), power(j["retail"], j)
    rc = power(j[both]["retail"], j[both]); rd = power(j[~both]["retail"], j[~both])
    print(f"  {c:5s}{rs[1]:>+9.2f}{rr[1]:>+8.2f}{rc[1]:>+8.2f}{rd[1]:>+8.2f}"
          f"{spread_pct(j,'retail'):>+10.2f}{spread_pct(j[both],'retail'):>+10.2f}"
          f"{100*both.mean():>9.0f}%")

print("\n② 关键问题:共振把机会砍掉一半,换来的提升值不值")
print("   算法:极端档差 × 可用时段占比 = 「单位时间能吃到的」")
print(f"  {'品种':5s}{'全时段':>10s}{'共振时段':>10s}{'占比':>8s}{'折算后':>10s}   判定")
for c in ("LH", "FG", "SA"):
    cut = LH_CUT if c == "LH" else CUT
    df, main = data[c]
    tr = seat_alpha(df[df["trade_date"] < cut], c, min_days=80 if c == "LH" else 150)
    if tr.empty or len(tr) < 5: continue
    smart5 = tr.sort_values("alpha", ascending=False).head(5).index.tolist()
    te = df[df["trade_date"] >= cut]
    have = [m for m in SEED if m in set(te["member_key"])]
    smart = te[te["member_key"].isin(smart5)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    ret = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    ret = (ret - ret.rolling(120, min_periods=60).mean()) / ret.rolling(120, min_periods=60).std()
    j = pd.concat([smart.rename("smart"), ret.rename("retail"),
                   main[main.index >= cut]], axis=1, sort=True).dropna()
    both = np.sign(j["smart"]) == np.sign(j["retail"])
    a, b = spread_pct(j, "retail"), spread_pct(j[both], "retail")
    share = both.mean()
    verdict = "值" if b * share > a * 0.9 else "不值(机会损失大于提升)"
    print(f"  {c:5s}{a:>+10.2f}{b:>+10.2f}{100*share:>7.0f}%{b*share:>+10.2f}   {verdict}")

print("\n③ 换个用法:不做过滤,只在**共振且信号极端**时才动(事件式)")
print(f"  {'品种':5s}{'触发天数':>9s}{'占比':>7s}{'之后20日平均':>13s}{'全样本平均':>12s}{'超额':>9s}")
for c in ("LH", "FG", "SA"):
    cut = LH_CUT if c == "LH" else CUT
    df, main = data[c]
    tr = seat_alpha(df[df["trade_date"] < cut], c, min_days=80 if c == "LH" else 150)
    if tr.empty or len(tr) < 5: continue
    smart5 = tr.sort_values("alpha", ascending=False).head(5).index.tolist()
    te = df[df["trade_date"] >= cut]
    have = [m for m in SEED if m in set(te["member_key"])]
    smart = te[te["member_key"].isin(smart5)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    ret = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    ret = (ret - ret.rolling(120, min_periods=60).mean()) / ret.rolling(120, min_periods=60).std()
    j = pd.concat([smart.rename("smart"), ret.rename("retail"),
                   main[main.index >= cut]], axis=1, sort=True).dropna()
    both = np.sign(j["smart"]) == np.sign(j["retail"])
    # 极端 = |z| >= 1 且共振;方向按 retail 的符号取
    hot = j[both & (j["retail"].abs() >= 1.0)]
    if len(hot) < 30: print(f"  {c:5s}  触发太少({len(hot)})"); continue
    r = (np.sign(hot["retail"]) * hot["fwd"]).mean() * 100
    base = (np.sign(hot["retail"]).mean() * j["fwd"].mean()) * 100
    print(f"  {c:5s}{len(hot):>9d}{100*len(hot)/len(j):>6.0f}%{r:>+13.2f}%{base:>+11.2f}%{r-base:>+9.2f}")
