"""每个品种选「最优档」,收益变好还是变差?(运营者 2026-08-23 追问)
全样本内选最优必然不差 —— 同义反复。有意义的是:**前半段选档,后半段验**,
与固定 30% 在同一个后半段上比。"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402

GRID = [round(x, 2) for x in np.arange(0.20, 0.7001, 0.05)]


def sharpe(d):
    d = d.fillna(0)
    return float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan


def cum(d):
    return float((1 + d.fillna(0)).prod() - 1) * 100


print(f"{'品种':<6}{'全样本最优':>8}{'全样本 最优 vs 30%':>22}{'前半段选出':>9}{'后半段:选出档':>16}{'后半段:固定30%':>16}{'结果':>6}")
for code in ("FG", "SA", "JD", "JM", "LH"):
    sig0, mkt, rdf, op, st, groups, unload = R.load(code)
    seat = H.clean_seat(pd.read_csv(R.DATA / f"{code.lower()}_seat.csv.gz"))
    v = H.use(code)
    H.CURRENT = {"code": code, **v}
    if H.RULES["signal_source"] != "cost":
        H.RULES["signal_source"] = "cost"
        H.RULES["long_enabled"] = True
        H.RULES["long_needs_dip"] = False
    dailies = {}
    for u in GRID:
        H.RULES["cost_unload_max"] = u
        sig = H.attach_cost_signal(sig0, seat, mkt, groups)
        _, _, d = H.replay(sig, mkt, rdf, op, st)
        dailies[u] = d.fillna(0)
    H.RULES["cost_unload_max"] = 0.30
    full = {u: sharpe(d) for u, d in dailies.items()}
    best_full = max(full, key=full.get)
    mid = mkt.index[len(mkt.index) // 2]
    first = {u: sharpe(d[d.index < mid]) for u, d in dailies.items()}
    first = {u: (x if np.isfinite(x) else -np.inf) for u, x in first.items()}
    pick = max(first, key=first.get)
    later_pick = dailies[pick][dailies[pick].index >= mid]
    later_30 = dailies[0.30][dailies[0.30].index >= mid]
    verdict = "变好" if sharpe(later_pick) > sharpe(later_30) + 1e-9 else ("持平" if pick == 0.30 else "变差")
    print(f"{code:<6}{best_full:>8.0%}{cum(dailies[best_full]):>+9.1f}%/{full[best_full]:.2f} vs {cum(dailies[0.30]):+.1f}%/{full[0.30]:.2f}"
          f"{pick:>9.0%}{cum(later_pick):>+9.1f}%/{sharpe(later_pick):.2f}{cum(later_30):>+9.1f}%/{sharpe(later_30):.2f}{verdict:>6}")
