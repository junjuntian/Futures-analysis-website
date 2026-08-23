"""玻璃:把 2025-10 起的席位组里华泰换成国泰君安(可调权重),用引擎现行玻璃策略回放,看 2026 收益变化。
跑法:仓库根目录 python research/run_fg_swap_gtja.py"""
import sys, pathlib, numpy as np, pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
D = pathlib.Path(__file__).resolve().parent / "data"
price = H.clean_price(pd.read_csv(D / "fg_price.csv.gz")); seat0 = H.clean_seat(pd.read_csv(D / "fg_seat.csv.gz"))
v = H.use("FG"); H.CURRENT = {"code": "FG", **v}
mkt = H.main_series(price); op, st = H.contract_prices(price); mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
rdf, _ = H.retail_series(seat0, mkt.index)
roll, log, cuts = H.rolling_groups(seat0, price, mkt.index)
SINCE = pd.Timestamp("2025-10-01")
def swapped(groups, old, new):
    g = groups.copy()
    for d in g.index:
        if d >= SINCE and g[d] is not None and old in g[d]:
            g[d] = tuple(new if m == old else m for m in g[d])
    return g
def run(groups, seat, label):
    sig = H.signal_series(seat, groups)
    if H.RULES["signal_source"] == "cost": sig = H.attach_cost_signal(sig, seat, mkt, groups)
    if H.RULES["exit_mode"] == "inst": sig = H.attach_inst_exit(sig, seat, mkt, groups)
    if H.RULES["long_mode"] == "unload_bounce": sig = H.attach_bounce_long(sig, seat, mkt, groups)
    tr, pos, daily = H.replay(sig, mkt, rdf, op, st)
    out = {}
    for name, lo, hi in (("2026", "2026-01-01", None), ("2025-10 起", "2025-10-01", None), ("全样本", None, None)):
        dd = daily if lo is None else daily[daily.index >= lo]
        t = [x for x in tr if lo is None or (x["exit_date"] and x["exit_date"] >= lo)]
        p = H._perf(dd); w = [x for x in t if x["ret_pct"] > 0]
        out[name] = (p["cum_pct"], p["sharpe"], p["max_dd_pct"], len(t), round(len(w) / len(t) * 100) if t else None)
    return out, [x for x in tr if x["entry_date"] >= "2026-01-01"]
def weighted_seat(seat, member, w):
    s = seat.copy()
    m = s.member_key == member
    for c in ("net", "net_off", "long_volume", "short_volume", "long", "short"):
        if c in s.columns: s.loc[m, c] = s.loc[m, c] * w
    return s
print(f"玻璃策略:signal_source={H.RULES['signal_source']} exit_mode={H.RULES['exit_mode']} long_enabled={H.RULES['long_enabled']}")
print(f"现行 2025-10 起席位组:{roll.iloc[-1]}")
cases = [("现行(含华泰)", roll, seat0)]
g2 = swapped(roll, "华泰期货", "国泰君安")
for w in (1.0, 0.5, 0.3):
    cases.append((f"华泰→国泰君安 权重{w:g}", g2, weighted_seat(seat0, "国泰君安", w) if w != 1.0 else seat0))
res = {}
for label, g, s in cases:
    res[label], tr26 = run(g, s, label)
    res[label + "_tr"] = tr26
for per in ("2026", "2025-10 起", "全样本"):
    print(f"\n=== {per} ===\n  {'方案':22s}{'累计%':>8s}{'夏普':>7s}{'回撤%':>8s}{'笔':>4s}{'胜%':>5s}")
    for label, _, _ in cases:
        c, sh, dd, n, wn = res[label][per]
        print(f"  {label:22s}{c:>+8.1f}{(sh if sh is not None else float('nan')):>7.2f}{dd:>8.1f}{n:>4d}{(wn if wn is not None else float('nan')):>5.0f}")
print("\n=== 2026 逐笔(现行 vs 权重1 换国泰君安) ===")
for label in ("现行(含华泰)", "华泰→国泰君安 权重1"):
    print(f"  [{label}]")
    for x in res[label + "_tr"]:
        print(f"    {x['side']:<5}{x['entry_date']}→{x['exit_date'] or '持有'} {x['contract']} {x['ret_pct']:+6.2f}% {x['exit_reason']}")
