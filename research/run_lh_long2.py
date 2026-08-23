"""生猪做多腿的干净隔离(运营者拍板要上之前必须分清):
多单只由「机构净空且本轮卸仓≥50%」触发,流量 z≥1 的正值一律压成 0(不许顺带做多)。"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402


def tstat(a):
    a = np.array(a, dtype=float)
    return a.mean() / a.std(ddof=1) * np.sqrt(len(a)) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan


def sharpe(d):
    d = d.fillna(0)
    return float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan


def cum(d):
    return float((1 + d.fillna(0)).prod() - 1) * 100


sig, mkt, rdf, op, st, groups, unload = R.load("LH")
v = H.use("LH")
H.CURRENT = {"code": "LH", **v}
cc = H.inst_cost_series(sig, mkt, groups)
side = cc["side"].reindex(mkt.index)
unl = unload.reindex(mkt.index)
orig = H.entry_exit_signals
z_flow, _ = orig(sig, rdf)
z_flow = z_flow.reindex(mkt.index)
enter = H.RULES["enter"]


def run(z_in):
    H.RULES["long_enabled"] = True
    H.RULES["long_needs_dip"] = False
    H.entry_exit_signals = lambda s, r, _z=z_in: (_z, r["rz"])
    try:
        tr, _, d = H.replay(sig, mkt, rdf, op, st)
    finally:
        H.entry_exit_signals = orig
        H.RULES["long_enabled"] = False
        H.RULES["long_needs_dip"] = True
    return [t for t in tr if t["exit_date"]], d


trA, _, dA = H.replay(sig, mkt, rdf, op, st)
trA = [t for t in trA if t["exit_date"]]
z_short_only = z_flow.where(~(z_flow > 0), 0.0)          # 正值压掉:流量不许做多
res = {"现行只做空": (trA, dA)}
for X in (0.30, 0.50, 0.70):
    cond = ((side < 0) & (unl >= X)).fillna(False)
    z = z_short_only.where(~(cond & ~(z_short_only <= -enter)), enter + 0.5)
    res[f"只由 净空且卸仓≥{X:.0%} 做多"] = run(z)
res["流量 z≥1 做多(参照)"] = run(z_flow)
both = z_flow.where(~(((side < 0) & (unl >= 0.5)).fillna(False) & ~(z_flow <= -enter)), enter + 0.5)
res["两者都做多(昨天那版)"] = run(both)
print(f"  {'方案':<26}{'笔数':>5}{'多单':>5}{'累计':>9}{'夏普':>7}{'回撤':>9}{'多单均值':>9}{'多单t':>7}{'多单胜率':>8}")
for k, (tr, d) in res.items():
    longs = [t["ret_pct"] for t in tr if t["side"] == "long"]
    w = sum(1 for x in longs if x > 0) / len(longs) * 100 if longs else float("nan")
    print(f"  {k:<26}{len(tr):>5}{len(longs):>5}{cum(d):>+8.1f}%{sharpe(d):>7.2f}{H._perf(d)['max_dd_pct']:>+8.1f}%"
          f"{(np.mean(longs) if longs else float('nan')):>+8.2f}%{tstat(longs):>+7.2f}{w:>7.0f}%")
print("\n  多单逐笔(只由 净空且卸仓≥50%):")
for t in res["只由 净空且卸仓≥50% 做多"][0]:
    if t["side"] == "long":
        print(f"    {t['entry_date']} → {t['exit_date']}  {t['contract']}  @{t['entry_px']}→{t['exit_px']}  {t['ret_pct']:+.2f}%  {t['exit_reason']}")
