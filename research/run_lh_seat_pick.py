"""生猪选人准则对比(DEC-122)。跑法:cd research  PYTHONPATH=. python run_lh_seat_pick.py"""
"""运营者 2026-08-23:择时收益对重仓席位不公平,能不能挑「赚钱多 + 大席位」。
同一套样本外框架(run_lh_phase1 P3):训练 < cut 选人,测试 ≥ cut 看组信号控动量 t。
新增准则:总盈亏×仓位 合成 / 大席位里挑赚钱多 / alpha 里挑大席位。"""
import numpy as np, pandas as pd
import run_lh_phase1 as P
df, main = P.build()
tr_full = P.seat_alpha(df)
# 每家平均净持仓绝对值(手)与在榜天数
sz = (df.groupby("member_key")["net"].apply(lambda s: s.abs().mean())).rename("avg_abs_net")
t = tr_full.join(sz)
t["alpha_share"] = (t["alpha"] / t["pnl"]).where(t["pnl"].abs() > 0.05)
print("=== 全样本(2021~2026-08)每家:总盈亏/择时/仓位 ===")
cols = ["pnl", "alpha", "avg_abs_net", "days"]
print(t.sort_values("pnl", ascending=False)[cols].head(12).round(2).to_string())
print("\n兴证期货:", t.loc["兴证期货", cols].round(2).to_dict() if "兴证期货" in t.index else "不在")

def pick(tr, rule, k=5):
    tr = tr.copy()
    if rule == "alpha": return tr.sort_values("alpha", ascending=False).head(k).index.tolist()
    if rule == "pnl": return tr.sort_values("pnl", ascending=False).head(k).index.tolist()
    if rule == "size": return tr.sort_values("avg_abs_net", ascending=False).head(k).index.tolist()
    if rule == "pnl×size":   # 秩和:总盈亏名次 + 仓位名次
        r = tr["pnl"].rank(ascending=False) + tr["avg_abs_net"].rank(ascending=False)
        return r.sort_values().head(k).index.tolist()
    if rule == "大席位里挑赚钱多":  # 仓位 ≥ 中位 之内按总盈亏
        big = tr[tr["avg_abs_net"] >= tr["avg_abs_net"].median()]
        return big.sort_values("pnl", ascending=False).head(k).index.tolist()
    if rule == "前10大里挑赚钱多":
        big = tr.sort_values("avg_abs_net", ascending=False).head(10)
        return big.sort_values("pnl", ascending=False).head(k).index.tolist()
    if rule == "alpha(仓位≥中位)":
        big = tr[tr["avg_abs_net"] >= tr["avg_abs_net"].median()]
        return big.sort_values("alpha", ascending=False).head(k).index.tolist()
    if rule == "pnl×alpha":
        r = tr["pnl"].rank(ascending=False) + tr["alpha"].rank(ascending=False)
        return r.sort_values().head(k).index.tolist()

RULES = ["alpha", "pnl", "size", "pnl×size", "大席位里挑赚钱多", "前10大里挑赚钱多", "alpha(仓位≥中位)", "pnl×alpha"]
for cut in (pd.Timestamp("2024-07-01"), pd.Timestamp("2025-07-01"), pd.Timestamp("2026-01-01")):
    tr = P.seat_alpha(df, hi=cut).join(df[df.trade_date < cut].groupby("member_key")["net"].apply(lambda s: s.abs().mean()).rename("avg_abs_net"))
    te = df[df["trade_date"] >= cut]; tm = main[main.index >= cut]
    print(f"\n=== 训练 < {cut.date()},测试 ≥ 之(测试 {tm.shape[0]} 日) ===")
    print(f"  {'准则':18s}{'t':>7s}{'偏相关':>8s}  组")
    for r in RULES:
        g = pick(tr, r)
        res = P.power_variety(P.group_signal_variety(te, g), tm)
        if res: print(f"  {r:18s}{res['t']:>+7.2f}{res['partial']:>+8.3f}  {'、'.join(g)}")

print("\n\n=== 走前(walk-forward):每年 5/1 按各准则重选,拼出全程样本外信号,看逐年与合计 t ===")
cuts = pd.date_range("2024-05-01", "2026-05-01", freq="6MS")
def wf(rule):
    sigs = []
    for i, cut in enumerate(cuts):
        end = cuts[i+1] if i+1 < len(cuts) else pd.Timestamp("2099-01-01")
        tr = P.seat_alpha(df, hi=cut).join(df[df.trade_date < cut].groupby("member_key")["net"].apply(lambda s: s.abs().mean()).rename("avg_abs_net"))
        if len(tr) < 5: continue
        g = pick(tr, rule)
        te = df[(df.trade_date >= cut) & (df.trade_date < end)]
        sigs.append(P.group_signal_variety(te, g))
    return pd.concat(sigs).sort_index()
print(f"  {'准则':18s}{'合计t':>7s}" + "".join(f"{y:>8d}" for y in range(2024, 2027)))
for r in RULES:
    s = wf(r)
    tot = P.power_variety(s, main)
    ys = []
    for y in range(2024, 2027):
        res = P.power_variety(s[s.index.year == y], main[main.index.year == y])
        ys.append(f"{res['t']:>+8.2f}" if res else f"{'—':>8s}")
    print(f"  {r:18s}{tot['t']:>+7.2f}" + "".join(ys))
