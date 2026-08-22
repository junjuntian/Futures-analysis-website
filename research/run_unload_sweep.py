"""卸仓阈值 20%~70% 按 5% 一档全扫(运营者 2026-08-23 要求)。

判定写死在前面:
  · 看整条曲面:单调还是平稳还是尖峰 —— 尖峰不信;
  · 邻档稳定:某档要算「好」,它 ±5% 两个邻档也得不差;
  · **选档 walk-forward**:每年只用之前年份的夏普挑档,链起来与固定 30% 比 ——
    这才回答「事前能不能选出来」,全样本峰值回答不了。
生产用成本进场的是 FG(加仓+轮龄≥2)/SA/JD,按各自生产配置扫;JM/LH 成本不是生产
进场,放成本双向只作参考。
"""
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


def tstat(r):
    a = np.array(r, dtype=float)
    return a.mean() / a.std(ddof=1) * np.sqrt(len(a)) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan


for code in ("FG", "SA", "JD", "JM", "LH"):
    sig0, mkt, rdf, op, st, groups, unload = R.load(code)
    seat = H.clean_seat(pd.read_csv(R.DATA / f"{code.lower()}_seat.csv.gz"))
    v = H.use(code)
    H.CURRENT = {"code": code, **v}
    ref = H.RULES["signal_source"] != "cost"
    if ref:   # 参考:成本双向
        H.RULES["signal_source"] = "cost"
        H.RULES["long_enabled"] = True
        H.RULES["long_needs_dip"] = False
    dailies, rows = {}, []
    for u in GRID:
        H.RULES["cost_unload_max"] = u
        sig = H.attach_cost_signal(sig0, seat, mkt, groups)
        tr, _, d = H.replay(sig, mkt, rdf, op, st)
        cl = [t for t in tr if t["exit_date"]]
        dailies[u] = d
        rows.append((u, len(cl), cum(d), sharpe(d), H._perf(d)["max_dd_pct"],
                     tstat([t["ret_pct"] for t in cl])))
    H.RULES["cost_unload_max"] = 0.30
    print("=" * 84)
    print(f"[{code}]{'(参考:成本双向,非生产)' if ref else '(生产配置)'}")
    print(f"  {'阈值':>5}{'笔数':>6}{'累计':>9}{'夏普':>7}{'回撤':>9}{'t':>7}")
    best = max(rows, key=lambda r: r[3])
    for u, n, c, s, dd, t in rows:
        tag = " ← 现行" if abs(u - 0.30) < 1e-9 else (" ← 全样本峰" if u == best[0] else "")
        print(f"  {u:>5.0%}{n:>6}{c:>+8.1f}%{s:>7.2f}{dd:>+8.1f}%{t:>+7.2f}{tag}")
    # 邻档稳定性:峰值与两邻档均值
    sh = {u: s for u, _, _, s, _, _ in rows}
    i = GRID.index(best[0])
    nb = [sh[GRID[j]] for j in (i - 1, i + 1) if 0 <= j < len(GRID)]
    print(f"  全样本峰 {best[0]:.0%}:夏普 {best[3]:.2f},邻档均值 {np.mean(nb):.2f};现行 30%:{sh[0.30]:.2f}")
    # 选档 walk-forward
    years = sorted({x.year for x in mkt.index})
    chain, picks = [], []
    for j, y in enumerate(years):
        if j == 0:
            pick = 0.30
        else:
            prior = years[:j]
            sc = {u: sharpe(dailies[u][dailies[u].index.year.isin(prior)]) for u in GRID}
            sc = {u: (x if np.isfinite(x) else -np.inf) for u, x in sc.items()}
            pick = max(sc, key=sc.get)
        picks.append(f"{pick:.0%}")
        dd_ = dailies[pick]
        chain.append(dd_[dd_.index.year == y])
    chain = pd.concat(chain)
    print(f"  选档 walk-forward:链 {cum(chain):+.1f}%/{sharpe(chain):.2f} vs 固定 30% {cum(dailies[0.30]):+.1f}%/{sh[0.30]:.2f}"
          f"  (逐年选 {' '.join(picks)})")
