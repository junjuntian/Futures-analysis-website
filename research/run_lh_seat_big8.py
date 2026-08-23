"""生猪「大席位里挑赚钱多」前 8 家 + 信号力(DEC-122)。跑法:cd research  PYTHONPATH=. python run_lh_seat_big8.py"""
import numpy as np, pandas as pd
import run_lh_phase1 as P
df, main = P.build()
cut = pd.Timestamp("2026-05-01")   # 现行重选截点,只用截点前数据
tr = P.seat_alpha(df, hi=cut)
d0 = df[df.trade_date < cut]
tr = tr.join(d0.groupby("member_key")["net"].apply(lambda s: s.abs().mean()).rename("平均净持仓(手)"))
tr = tr.join(d0.groupby("member_key")["net"].apply(lambda s: s.abs().max()).rename("最大净持仓"))
# 截点后至今的仓位与方向
d1 = df[df.trade_date >= cut]
now = d1[d1.trade_date == d1.trade_date.max()].groupby("member_key")["net"].sum().rename("当前净持仓")
tr = tr.join(now)
big = tr[tr["平均净持仓(手)"] >= tr["平均净持仓(手)"].median()]
print(f"截点 {cut.date()},候选 {len(tr)} 家,仓位≥中位({tr['平均净持仓(手)'].median():.0f} 手)的大席位 {len(big)} 家")
print("\n=== 大席位按总盈亏排序 前 8 ===")
cols = ["pnl", "alpha", "平均净持仓(手)", "最大净持仓", "days", "当前净持仓"]
top8 = big.sort_values("pnl", ascending=False).head(8)
print(top8[cols].rename(columns={"pnl":"总盈亏(亿)","alpha":"择时收益(亿)","days":"在榜天数"}).round(2).to_string())
print("\n(对照)现行择时收益前 5:", P.seat_alpha(df, hi=cut).sort_values("alpha", ascending=False).head(5).index.tolist())

# 前 8 家做组的信号力:走前半年重选,与 K=5 和 alpha 对比
cuts = pd.date_range("2024-05-01", "2026-05-01", freq="6MS")
def pick(tr, rule, k):
    if rule == "alpha": return tr.sort_values("alpha", ascending=False).head(k).index.tolist()
    b = tr[tr["avg_abs_net"] >= tr["avg_abs_net"].median()]
    return b.sort_values("pnl", ascending=False).head(k).index.tolist()
def wf(rule, k):
    sigs = []
    for i, c in enumerate(cuts):
        end = cuts[i+1] if i+1 < len(cuts) else pd.Timestamp("2099-01-01")
        t = P.seat_alpha(df, hi=c).join(df[df.trade_date < c].groupby("member_key")["net"].apply(lambda s: s.abs().mean()).rename("avg_abs_net"))
        if len(t) < k: continue
        g = pick(t, rule, k)
        te = df[(df.trade_date >= c) & (df.trade_date < end)]
        sigs.append(P.group_signal_variety(te, g))
    return pd.concat(sigs).sort_index()
print("\n=== 走前(半年重选)信号力 ===")
print(f"  {'准则':16s}{'K':>3s}{'合计t':>7s}{'2025':>8s}{'2026':>8s}")
for rule in ("alpha", "大席位挑赚钱多"):
    for k in (5, 8):
        s = wf(rule, k); tot = P.power_variety(s, main)
        ys = [P.power_variety(s[s.index.year == y], main[main.index.year == y])["t"] for y in (2025, 2026)]
        print(f"  {rule:16s}{k:>3d}{tot['t']:>+7.2f}{ys[0]:>+8.2f}{ys[1]:>+8.2f}")
