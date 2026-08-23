"""生猪怎么做多 —— 运营者假设(2026-08-23):「机构空了之后开始减仓,就会翻多」。

## 两步,判据先写死

一、事件检验(不碰策略):机构组净空且本轮卸仓首次越过 X%(30/50/70)的那天,
   之后 5/10/20/40 日主力(同合约)收益,对照全样本同口径均值。另看「机构净空翻净多」
   那一天之后的收益。**要的是均值为正、且 20/40 日 t > 2**,否则只是噪音。
二、加做多腿(空单进出场一字不动):做多条件 = 机构组净空 且 本轮卸仓 ≥ X%(主规格 50%,
   30/70 只作敏感性),且当天没有做空信号;出场沿用四件套(散户翻向/止损/持满/交割)。
   与现行只做空比:夏普不能降、回撤不能恶化 >3pp、多单那部分单笔 t > 0。
   另跑一个参照:做多条件换成「流量 z≥1」(= DEC-111 重扫里已否决的开做多),
   看运营者这条是不是比它好。

样本只有三年(2023-08 起),这句丑话对每个数字都成立。
"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402


def tstat(a):
    a = np.array(a, dtype=float)
    a = a[np.isfinite(a)]
    return a.mean() / a.std(ddof=1) * np.sqrt(len(a)) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan


def sharpe(d):
    d = d.fillna(0)
    return float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan


def cum(d):
    return float((1 + d.fillna(0)).prod() - 1) * 100


sig, mkt, rdf, op, st, groups, unload = R.load("LH")
v = H.use("LH")
H.CURRENT = {"code": "LH", **v}
assert H.RULES["signal_source"] == "resonance" and H.RULES["long_enabled"] is False
cc = H.inst_cost_series(sig, mkt, groups)
side = cc["side"].reindex(mkt.index)
unl = unload.reindex(mkt.index)
idx = mkt.index
# 同合约前瞻收益:用逐日 ret(已处理换月)连乘
ret = mkt["ret"].fillna(0)


def fwd(i, h):
    if i + h >= len(idx):
        return np.nan
    return float((1 + ret.iloc[i + 1:i + 1 + h]).prod() - 1) * 100


print("=" * 90)
print("一、事件检验:机构净空 + 本轮卸仓首次越过 X% 之后,主力走势(同合约连乘,%)")
print("=" * 90)
base = {h: np.nanmean([fwd(i, h) for i in range(len(idx))]) for h in (5, 10, 20, 40)}
print(f"  全样本无条件均值:  5日 {base[5]:+.2f}  10日 {base[10]:+.2f}  20日 {base[20]:+.2f}  40日 {base[40]:+.2f}")
for X in (0.30, 0.50, 0.70):
    ev = []
    armed = True
    for i, d in enumerate(idx):
        s, u = side.iloc[i], unl.iloc[i]
        if not (np.isfinite(s) and s < 0):
            armed = True
            continue
        if np.isfinite(u) and u >= X and armed:
            ev.append(i)
            armed = False
    rows = {h: [fwd(i, h) for i in ev] for h in (5, 10, 20, 40)}
    print(f"  净空且卸仓首越 {X:.0%}:{len(ev):>3} 次 | " +
          "  ".join(f"{h}日 {np.nanmean(rows[h]):+.2f}(t{tstat(rows[h]):+.1f})" for h in (5, 10, 20, 40)))
# 净空翻净多
flips = [i for i in range(1, len(idx)) if np.isfinite(side.iloc[i]) and np.isfinite(side.iloc[i - 1])
         and side.iloc[i - 1] < 0 and side.iloc[i] > 0]
rows = {h: [fwd(i, h) for i in flips] for h in (5, 10, 20, 40)}
print(f"  机构净空翻净多:      {len(flips):>3} 次 | " +
      "  ".join(f"{h}日 {np.nanmean(rows[h]):+.2f}(t{tstat(rows[h]):+.1f})" for h in (5, 10, 20, 40)))

print()
print("=" * 90)
print("二、加做多腿回测(空单逻辑不动;出场四件套)")
print("=" * 90)
H.RULES["signal_source"] = "resonance"
orig = H.entry_exit_signals
z_flow, _ = orig(sig, rdf)
z_flow = z_flow.reindex(idx)


def run_with_long(long_cond):
    # 有做空信号优先;否则满足做多条件就 +1.5
    z_in = z_flow.copy()
    inject = long_cond & ~(z_in <= -H.RULES["enter"])
    z_in = z_in.where(~inject, 1.5)
    H.RULES["long_enabled"] = True
    H.RULES["long_needs_dip"] = False
    H.entry_exit_signals = lambda s, r, _z=z_in: (_z, r["rz"])
    try:
        tr, _, d = H.replay(sig, mkt, rdf, op, st)
    finally:
        H.entry_exit_signals = orig
        H.RULES["long_enabled"] = False
    return [t for t in tr if t["exit_date"]], d


trA, _, dA = H.replay(sig, mkt, rdf, op, st)
trA = [t for t in trA if t["exit_date"]]
print(f"  {'方案':<24}{'笔数':>5}{'多单':>5}{'累计':>9}{'夏普':>7}{'回撤':>9}{'多单均值':>9}{'多单t':>7}")
print(f"  {'现行只做空':<24}{len(trA):>5}{0:>5}{cum(dA):>+8.1f}%{sharpe(dA):>7.2f}{H._perf(dA)['max_dd_pct']:>+8.1f}%{'—':>9}{'—':>7}")
cands = {}
for X in (0.30, 0.50, 0.70):
    cond = ((side < 0) & (unl >= X)).fillna(False)
    tr, d = run_with_long(cond)
    longs = [t["ret_pct"] for t in tr if t["side"] == "long"]
    cands[f"净空且卸仓≥{X:.0%}"] = (tr, d, longs)
cond_flow = (z_flow >= H.RULES["enter"]).fillna(False)
tr, d = run_with_long(cond_flow)
cands["参照:流量 z≥1 做多"] = (tr, d, [t["ret_pct"] for t in tr if t["side"] == "long"])
for k, (tr, d, longs) in cands.items():
    print(f"  {k:<24}{len(tr):>5}{len(longs):>5}{cum(d):>+8.1f}%{sharpe(d):>7.2f}{H._perf(d)['max_dd_pct']:>+8.1f}%"
          f"{(np.mean(longs) if longs else float('nan')):>+8.2f}%{tstat(longs):>+7.2f}")
print()
print("  多单逐笔(主规格 50%):")
for t in cands["净空且卸仓≥50%"][0]:
    if t["side"] == "long":
        print(f"    {t['entry_date']} → {t['exit_date']}  {t['contract']}  @{t['entry_px']}→{t['exit_px']}  {t['ret_pct']:+.2f}%  {t['exit_reason']}")
