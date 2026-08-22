"""出场实验第三步(运营者 2026-08-23 问:卸仓>50% 再走呢?)。
只加这一档,并把「翻向」与「卸仓」两条拆开,看谁在赶人。出场其余同 B。"""
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


def run(sig, mkt, rdf, op, st, extra=None, disable_reverse=False, no_cap=False):
    old = dict(H.RULES)
    try:
        if no_cap:
            H.RULES["max_hold"] = 10**6
        tr, _, daily = H.replay(sig, mkt, rdf, op, st, extra_exit=extra, disable_reverse=disable_reverse)
    finally:
        H.RULES.clear()
        H.RULES.update(old)
    return [t for t in tr if t["exit_date"]], daily


for code in ("FG", "SA", "JD", "JM", "LH"):
    sig0, mkt, rdf, op, st, groups, unload = R.load(code)
    seat = H.clean_seat(pd.read_csv(R.DATA / f"{code.lower()}_seat.csv.gz"))
    v = H.use(code)
    H.CURRENT = {"code": code, **v}
    sig = H.attach_cost_signal(sig0, seat, mkt, groups) if H.RULES["signal_source"] == "cost" else sig0
    cc = H.inst_cost_series(sig0, mkt, groups)
    side = cc["side"].reindex(mkt.index)
    flip = (side.notna() & side.shift(1).notna() & (side != side.shift(1))).fillna(False)
    unl = unload.reindex(mkt.index)
    u30 = (unl > 0.30).fillna(False)
    u50 = (unl > 0.50).fillna(False)
    trA, dA = run(sig, mkt, rdf, op, st)
    variants = {
        "A 现行": (trA, dA),
        "翻向|卸>30%": run(sig, mkt, rdf, op, st, flip | u30, True, True),
        "翻向|卸>50%": run(sig, mkt, rdf, op, st, flip | u50, True, True),
        "只翻向": run(sig, mkt, rdf, op, st, flip, True, True),
        "只卸>30%": run(sig, mkt, rdf, op, st, u30, True, True),
        "只卸>50%": run(sig, mkt, rdf, op, st, u50, True, True),
    }
    print("=" * 90)
    print(f"[{code}] 卸仓>50% 的天占 {u50.sum()/max(side.notna().sum(),1):.0%}(>30%:{u30.sum()/max(side.notna().sum(),1):.0%})")
    for k, (tr, d) in variants.items():
        hold = np.mean([t["hold_days"] for t in tr]) if tr else np.nan
        print(f"  {k:<11}{len(tr):>4} 笔  累计 {cum(d):>+7.1f}%  夏普 {sharpe(d):>5.2f}  回撤 {H._perf(d)['max_dd_pct']:>+6.1f}%  均持有 {hold:>4.0f}日")
