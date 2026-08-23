"""任一品种:固定席位组 vs 现行滚动 alpha 组,用引擎该品种现行策略全流程回放(DEC-122/125 同口径)。
跑法:仓库根目录 python research/run_fixed_groups.py JM 国泰君安,东证期货,永安期货,浙商期货,东吴期货"""
import sys, pathlib, pandas as pd, numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
code = sys.argv[1]; fixed = tuple(sys.argv[2].split(","))
D = pathlib.Path(__file__).resolve().parent / "data"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz")); seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code); H.CURRENT = {"code": code, **v}
H.RULES["fixed_members"] = None   # 对照组要跑滚动
mkt = H.main_series(price); op, st = H.contract_prices(price); mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
rdf, _ = H.retail_series(seat, mkt.index)
roll, _, _ = H.rolling_groups(seat, price, mkt.index)
def run(groups):
    sig = H.signal_series(seat, groups)
    if H.RULES["signal_source"] == "cost": sig = H.attach_cost_signal(sig, seat, mkt, groups)
    if H.RULES["exit_mode"] == "inst": sig = H.attach_inst_exit(sig, seat, mkt, groups)
    if H.RULES["long_mode"] == "unload_bounce": sig = H.attach_bounce_long(sig, seat, mkt, groups)
    trades, pos, daily = H.replay(sig, mkt, rdf, op, st)
    out = {}
    for name, lo in (("全样本", None), ("近一年", mkt.index[-1] - pd.Timedelta(days=365)), ("2026", pd.Timestamp("2026-01-01"))):
        dd = daily if lo is None else daily[daily.index >= lo]
        tr = [t for t in trades if lo is None or (t["exit_date"] and pd.Timestamp(t["exit_date"]) >= lo)]
        p = H._perf(dd); w = [t for t in tr if t["ret_pct"] > 0]
        out[name] = (p["cum_pct"], p["sharpe"], p["max_dd_pct"], len(tr), round(len(w) / len(tr) * 100) if tr else None)
    return out, trades
const = pd.Series([fixed] * len(mkt.index), index=mkt.index, dtype=object)
res = {"现行滚动alpha组": run(roll), f"固定{len(fixed)}家": run(const)}
print(f"{v['name']} 策略:signal_source={H.RULES['signal_source']} long_mode={H.RULES['long_mode']} exit_mode={H.RULES['exit_mode']} long_enabled={H.RULES['long_enabled']} | 固定名单 {'、'.join(fixed)} | 滚动当前组 {'、'.join(roll.iloc[-1])}")
for per in ("近一年", "2026", "全样本"):
    print(f"\n=== {per} ===\n  {'席位组':16s}{'累计%':>8s}{'夏普':>7s}{'回撤%':>8s}{'笔':>4s}{'胜%':>5s}")
    for k, (r, _) in res.items():
        c, s, d, n, w = r[per]; print(f"  {k:16s}{c:>+8.1f}{(s if s is not None else float('nan')):>7.2f}{d:>8.1f}{n:>4d}{(w if w is not None else float('nan')):>5.0f}")
_, tr = res[f"固定{len(fixed)}家"]
open_ = [t for t in tr if t["exit_date"] is None]
print("\n固定名单下当前持仓:", open_[0] if open_ else "无")
_, tr0 = res["现行滚动alpha组"]; open0 = [t for t in tr0 if t["exit_date"] is None]
print("滚动组下当前持仓:", open0[0] if open0 else "无")
