"""焦煤「机构出场」五道闸门 —— 按 PLAN_JM_INST_EXIT_v1 预注册执行,判据原样。

进场 = 焦煤新生产配置(DEC-116 开做多不要 dip),A = 现行四件套出场,
B = 机构翻向或卸仓>30% 才走(止损/交割保留,关散户翻向与持满)。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402


def tstat(r):
    a = np.array(r, dtype=float)
    return a.mean() / a.std(ddof=1) * np.sqrt(len(a)) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan


def sharpe(d):
    d = d.fillna(0)
    return float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan


def cum(d):
    return float((1 + d.fillna(0)).prod() - 1) * 100


def yearly(d):
    return {y: cum(d[d.index.year == y]) for y in sorted({x.year for x in d.index})}


def run(sig, mkt, rdf, op, st, extra=None, disable_reverse=False, no_cap=False):
    old = dict(H.RULES)
    try:
        if no_cap:
            H.RULES["max_hold"] = 10**6
        tr, _, d = H.replay(sig, mkt, rdf, op, st, extra_exit=extra, disable_reverse=disable_reverse)
    finally:
        H.RULES.clear()
        H.RULES.update(old)
    return [t for t in tr if t["exit_date"]], d


sig, mkt, rdf, op, st, groups, unload = R.load("JM")
v = H.use("JM")                       # 新生产配置:开做多、不要 dip、流量共振
H.CURRENT = {"code": "JM", **v}
assert H.RULES["long_enabled"] is True and H.RULES["signal_source"] == "resonance", "要先上 DEC-116"
cc = H.inst_cost_series(sig, mkt, groups)
side = cc["side"].reindex(mkt.index)
flip = (side.notna() & side.shift(1).notna() & (side != side.shift(1))).fillna(False)
unl = unload.reindex(mkt.index)
u30, u50 = (unl > 0.30).fillna(False), (unl > 0.50).fillna(False)


def persist(flags, n):
    return (flags.astype(int).rolling(n).sum() >= n).fillna(False)


trA, dA = run(sig, mkt, rdf, op, st)
trB, dB = run(sig, mkt, rdf, op, st, flip | u30, True, True)
print("=" * 90)
print(f"[JM 开做多] A 现行 {len(trA)} 笔 {cum(dA):+.1f}%/{sharpe(dA):.2f}/{H._perf(dA)['max_dd_pct']:+.1f}%   "
      f"B 机构出场 {len(trB)} 笔 {cum(dB):+.1f}%/{sharpe(dB):.2f}/{H._perf(dB)['max_dd_pct']:+.1f}%")
print("=" * 90)
ya, yb = yearly(dA), yearly(dB)
wins = sum(1 for y in ya if yb.get(y, 0) > ya[y])
pos = sum(1 for y in yb if yb[y] > 0)
for y in sorted(ya):
    print(f"    {y}: B {yb.get(y, float('nan')):>+7.1f}%  A {ya[y]:>+7.1f}%  {'✓' if yb.get(y, 0) > ya[y] else ' '}")
g1 = wins >= len(ya) / 2 and pos >= len(yb) / 2
print(f"闸门1 逐年:赢 {wins}/{len(ya)},正 {pos}/{len(yb)}  [{'过' if g1 else '不过'}]")
ys = sorted(ya)
chain, picks = [], []
for j, y in enumerate(ys):
    arm = "A"
    if j:
        pr = ys[:j]
        sa, sb = sharpe(dA[dA.index.year.isin(pr)]), sharpe(dB[dB.index.year.isin(pr)])
        arm = "B" if (np.isfinite(sb) and (not np.isfinite(sa) or sb > sa)) else "A"
    picks.append(arm)
    src = dB if arm == "B" else dA
    chain.append(src[src.index.year == y])
chain = pd.concat(chain)
g2 = cum(chain) >= cum(dA)
print(f"闸门2 选臂 walk-forward:链 {cum(chain):+.1f}% vs 一直 A {cum(dA):+.1f}%({''.join(picks)})  [{'过' if g2 else '不过'}]")
_, d50 = run(sig, mkt, rdf, op, st, flip | u50, True, True)
g3 = sharpe(d50) > sharpe(dA)
print(f"闸门3 阈值 50%:{cum(d50):+.1f}%/{sharpe(d50):.2f} vs A {sharpe(dA):.2f}  [{'过' if g3 else '不过'}]")
_, dp = run(sig, mkt, rdf, op, st, persist(flip | u30, 2), True, True)
g4 = sharpe(dp) >= sharpe(dA)
print(f"闸门4 去抖连续 2 日:{cum(dp):+.1f}%/{sharpe(dp):.2f} vs A {sharpe(dA):.2f}  [{'过' if g4 else '不过'}]")
tb, ta = tstat([t["ret_pct"] for t in trB]), tstat([t["ret_pct"] for t in trA])
g5 = np.isfinite(tb) and tb > 0
print(f"闸门5 单笔 t:B {tb:+.2f}(A {ta:+.2f})  [{'过' if g5 else '不过'}]")
holdA = np.mean([t["hold_days"] for t in trA])
holdB = np.mean([t["hold_days"] for t in trB])
print(f"  均持有 A {holdA:.0f} 日 / B {holdB:.0f} 日;B 多单 {sum(1 for t in trB if t['side']=='long')} 笔")
_, do = run(sig, mkt, rdf, op, st, u30, True, True)
print(f"  参照:只卸>30% {cum(do):+.1f}%/{sharpe(do):.2f}")
print(f"\n★ 五关通过 {sum([g1, g2, g3, g4, g5])}/5")
