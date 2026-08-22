"""出场实验第二步:为什么「跟随一轮」反而走得更快 —— 量机构状态序列的抖动,
并跑**一个**预注册的去抖变体(B2:翻向/卸仓>30% 须连续 3 个交易日才算一轮结束)。

只跑 3 日这一档,不扫参数 —— 扫出来的最优是挑运气(research/PITFALLS.md 六)。
判据同 run_exit_campaign:B2 夏普赢 A ≥3/5 且选臂不输。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402

UMAX = 0.30
PERSIST = 3


def sharpe(d):
    d = d.fillna(0)
    return float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan


def cum(d):
    return float((1 + d.fillna(0)).prod() - 1) * 100


def prod_sig(code, sig, mkt, seat, groups):
    v = H.use(code)
    H.CURRENT = {"code": code, **v}
    return H.attach_cost_signal(sig, seat, mkt, groups) if H.RULES["signal_source"] == "cost" else sig


def flags_raw(sig, mkt, groups, unload):
    cc = H.inst_cost_series(sig, mkt, groups)
    side = cc["side"].reindex(mkt.index)
    prev = side.shift(1)
    flip = side.notna() & prev.notna() & (side != prev)
    unl = unload.reindex(mkt.index)
    return flip.fillna(False), (unl > UMAX).fillna(False), side, unl


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


wins = 0
for code in ("FG", "SA", "JD", "JM", "LH"):
    sig0, mkt, rdf, op, st, groups, unload = R.load(code)
    seat = H.clean_seat(pd.read_csv(R.DATA / f"{code.lower()}_seat.csv.gz"))
    sig = prod_sig(code, sig0, mkt, seat, groups)
    flip, over, side, unl = flags_raw(sig0, mkt, groups, unload)
    n_days = int(side.notna().sum())
    # 一轮(同方向连续段)长度分布
    runs, cur, last = [], 0, None
    for v in side:
        if not np.isfinite(v) or v == 0:
            if cur:
                runs.append(cur)
            cur, last = 0, None
            continue
        if v == last:
            cur += 1
        else:
            if cur:
                runs.append(cur)
            cur, last = 1, v
    if cur:
        runs.append(cur)
    runs = np.array(runs)
    per_year = len(mkt.index) / 242
    print("=" * 96)
    print(f"[{code}] 机构状态抖动:翻向 {int(flip.sum())} 次({flip.sum()/per_year:.0f} 次/年),"
          f"卸仓>30% 的天占 {over.sum()/max(n_days,1):.0%};"
          f"同方向一轮长度 中位 {np.median(runs):.0f} 日 / 均值 {runs.mean():.0f} 日 / ≥20 日的占 {np.mean(runs>=20):.0%}")
    # 去抖:翻向或卸仓须连续 PERSIST 日
    raw = (flip | over).astype(int)
    persist = raw.rolling(PERSIST).sum() >= PERSIST
    persist = persist.fillna(False)
    trA, dA = run(sig, mkt, rdf, op, st)
    trB, dB = run(sig, mkt, rdf, op, st, extra=(flip | over), disable_reverse=True, no_cap=True)
    trB2, dB2 = run(sig, mkt, rdf, op, st, extra=persist, disable_reverse=True, no_cap=True)
    for k, (tr, d) in {"A 现行": (trA, dA), "B 逐日": (trB, dB), f"B2 连续{PERSIST}日": (trB2, dB2)}.items():
        hold = np.mean([t["hold_days"] for t in tr]) if tr else np.nan
        rs = {}
        for t in tr:
            rs[t["exit_reason"]] = rs.get(t["exit_reason"], 0) + 1
        print(f"  {k:<10}{len(tr):>4} 笔  累计 {cum(d):>+7.1f}%  夏普 {sharpe(d):>5.2f}  回撤 {H._perf(d)['max_dd_pct']:>+6.1f}%  均持有 {hold:>4.0f}日  出场 {rs}")
    w = sharpe(dB2) > sharpe(dA)
    wins += int(w)
    print(f"  → B2 夏普赢 A:{w}")
print(f"\n★ B2 赢 A:{wins}/5(判据 ≥3)")
