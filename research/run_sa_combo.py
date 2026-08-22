"""纯碱「流量 × 成本」组合实验 —— 按 PLAN_SA_COMBO_v1 预注册规格执行,判据原样照搬。

主规格:等权组合,组合日收益 = (流量臂日收益 + 成本臂日收益)/2。零新参数。
副规格:单账户 OR(任一信号触发即进,出场同一路散户反向),只作参照。
成本臂参数沿用闸门原值(容差 0%、卸仓 ≤30%),一个数字不动。

过关判据(全过才提改动):
 1. 组合夏普 > 两个单臂各自夏普;
 2. 组合最大回撤 < 两个单臂各自回撤;
 3. 逐年:组合为正的年份 ≥ 单臂中较好者;
 4. 三臂选臂 walk-forward(只用之前年份的夏普选),链不输「一直用流量」;
 5. 只回答纯碱。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402


def sharpe(d):
    d = d.fillna(0)
    return float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan


def cum(d):
    return float((1 + d.fillna(0)).prod() - 1) * 100


def mdd(d):
    eq = (1 + d.fillna(0)).cumprod()
    return float((eq / eq.cummax() - 1).min()) * 100


def yearly(d):
    return {y: cum(d[d.index.year == y]) for y in sorted({x.year for x in d.index})}


sig, mkt, rdf, op, st, groups, unload = R.load("SA")
assert H.RULES["signal_source"] == "resonance"
# —— 流量臂:生产配置原样回放 ——
tr_f, _, day_f = H.replay(sig, mkt, rdf, op, st)
# —— 成本臂:闸门原值 ——
H.RULES["long_enabled"] = True
H.RULES["long_needs_dip"] = False
z_cost = R.build_entry(sig, mkt, groups, unload, 0.0, 0.3)
orig = H.entry_exit_signals
H.entry_exit_signals = lambda s, r, _z=z_cost: (_z, r["rz"])
try:
    tr_c, _, day_c = H.replay(sig, mkt, rdf, op, st)
finally:
    H.entry_exit_signals = orig
# —— 主规格:等权组合 ——
day_f, day_c = day_f.fillna(0), day_c.fillna(0).reindex(day_f.index).fillna(0)
day_m = (day_f + day_c) / 2
# —— 副规格:单账户 OR(成本信号优先,否则流量那路的 z_in;出场同为散户 rz)——
z_flow_in, _ = orig(sig, rdf)
z_or = z_cost.where(z_cost != 0, z_flow_in.reindex(z_cost.index))
H.entry_exit_signals = lambda s, r, _z=z_or: (_z, r["rz"])
try:
    tr_o, _, day_o = H.replay(sig, mkt, rdf, op, st)
finally:
    H.entry_exit_signals = orig

arms = {"流量(现行)": day_f, "成本": day_c, "等权组合": day_m, "OR(参照)": day_o}
print("=" * 86)
print("纯碱 流量 × 成本 组合实验(PLAN_SA_COMBO_v1)")
print("=" * 86)
print(f"{'臂':<10}{'累计':>9}{'夏普':>7}{'回撤':>9}{'正年份':>8}")
yr = {k: yearly(v) for k, v in arms.items()}
for k, v in arms.items():
    pos = sum(1 for x in yr[k].values() if x > 0)
    print(f"{k:<10}{cum(v):>+8.1f}%{sharpe(v):>7.2f}{mdd(v):>+8.1f}%{pos:>5}/{len(yr[k])}")
corr = float(day_f.corr(day_c))
print(f"\n两单臂日收益相关系数:{corr:+.3f}  (互补的直接量度)")

print("\n逐年:")
ys = sorted(yr["流量(现行)"])
print("  年份  " + "".join(f"{k:>11}" for k in arms))
for y in ys:
    print(f"  {y}  " + "".join(f"{yr[k].get(y, float('nan')):>+10.1f}%" for k in arms))

# —— 判据 1/2/3 ——
sf, sc, sm = sharpe(day_f), sharpe(day_c), sharpe(day_m)
g1 = sm > sf and sm > sc
g2 = mdd(day_m) > mdd(day_f) and mdd(day_m) > mdd(day_c)   # 回撤是负数,更大=更浅
pf = sum(1 for x in yr["流量(现行)"].values() if x > 0)
pc = sum(1 for x in yr["成本"].values() if x > 0)
pm = sum(1 for x in yr["等权组合"].values() if x > 0)
g3 = pm >= max(pf, pc)
print(f"\n判据1 组合夏普 {sm:.2f} > 流量 {sf:.2f} 且 > 成本 {sc:.2f}:[{'过' if g1 else '不过'}]")
print(f"判据2 组合回撤 {mdd(day_m):+.1f}% 浅于 流量 {mdd(day_f):+.1f}% 与 成本 {mdd(day_c):+.1f}%:"
      f"[{'过' if g2 else '不过'}]")
print(f"判据3 组合正年份 {pm} ≥ max(流量 {pf}, 成本 {pc}):[{'过' if g3 else '不过'}]")

# —— 判据 4:三臂选臂 walk-forward ——
three = {"流": day_f, "成": day_c, "组": day_m}
chain, picks = [], []
for j, y in enumerate(ys):
    if j == 0:
        arm = "流"
    else:
        prior = ys[:j]
        sc_ = {k: sharpe(v[v.index.year.isin(prior)]) for k, v in three.items()}
        sc_ = {k: (v if np.isfinite(v) else -np.inf) for k, v in sc_.items()}
        arm = max(sc_, key=sc_.get)
        if sc_[arm] == -np.inf:
            arm = "流"
    picks.append(arm)
    src = three[arm]
    chain.append(src[src.index.year == y])
chain = pd.concat(chain)
g4 = cum(chain) >= cum(day_f)
print(f"判据4 三臂选臂链 {cum(chain):+.1f}%(逐年 {''.join(picks)})vs 一直流量 {cum(day_f):+.1f}%:"
      f"[{'过' if g4 else '不过'}]")
n_pass = sum([g1, g2, g3, g4])
print(f"\n★ 四道数值判据通过 {n_pass}/4(第 5 条「只回答纯碱」由本脚本的范围保证)")
