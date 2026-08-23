"""生猪:固定席位组(DEC-122;在仓库根目录跑:python research/run_lh_fixed_groups.py) vs 现行滚动 alpha 组,用引擎现行策略(use('LH'))全流程回放,看近一年。"""
import sys, pathlib, pandas as pd, numpy as np
sys.path.insert(0, "engine"); import hog_money as H
D = pathlib.Path("research/data")
price = H.clean_price(pd.read_csv(D/"lh_price.csv.gz")); seat = H.clean_seat(pd.read_csv(D/"lh_seat.csv.gz"))
H.use("LH"); H.CURRENT = {"code": "LH", **H.VARIETIES["LH"]}
mkt = H.main_series(price); op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
rdf, _ = H.retail_series(seat, mkt.index)
roll, log, cuts = H.rolling_groups(seat, price, mkt.index)

def run(groups, label):
    sig = H.signal_series(seat, groups)
    if H.RULES["signal_source"] == "cost": sig = H.attach_cost_signal(sig, seat, mkt, groups)
    if H.RULES["exit_mode"] == "inst": sig = H.attach_inst_exit(sig, seat, mkt, groups)
    if H.RULES["long_mode"] == "unload_bounce": sig = H.attach_bounce_long(sig, seat, mkt, groups)
    trades, pos, daily = H.replay(sig, mkt, rdf, op, st)
    out = {}
    for name, lo in (("全样本", None), ("近一年", pd.Timestamp("2025-08-22")), ("2026", pd.Timestamp("2026-01-01"))):
        d = daily if lo is None else daily[daily.index >= lo]
        tr = [t for t in trades if lo is None or pd.Timestamp(t["exit_date"] if "exit_date" in t else t.get("exit", t.get("out", ""))) >= lo] if trades else []
        p = H._perf(d)
        w = [t for t in tr if (t.get("pct") or t.get("ret_pct") or 0) > 0]
        out[name] = dict(cum=p["cum_pct"], sharpe=p["sharpe"], dd=p["max_dd_pct"], n=len(tr), win=round(len(w)/len(tr)*100) if tr else None)
    return out, trades

print("trade 键:", end=" ")
fixed5 = ("国泰君安", "东证期货", "东吴期货", "永安期货", "浙商期货")
fixed4 = ("国泰君安", "东证期货", "东吴期货", "浙商期货")
const5 = pd.Series([fixed5]*len(mkt.index), index=mkt.index, dtype=object)
const4 = pd.Series([fixed4]*len(mkt.index), index=mkt.index, dtype=object)
res = {}
for label, g in (("现行滚动alpha组", roll), ("固定5家(含永安)", const5), ("固定4家", const4)):
    res[label], tr = run(g, label)
    if label == "现行滚动alpha组": print(list(tr[0].keys()) if tr else None)
print(f"\n策略配置:signal_source={H.RULES['signal_source']} long_mode={H.RULES['long_mode']} exit_mode={H.RULES['exit_mode']}")
for per in ("近一年", "2026", "全样本"):
    print(f"\n=== {per} ===")
    print(f"  {'席位组':16s}{'累计%':>8s}{'夏普':>7s}{'最大回撤%':>9s}{'笔数':>5s}{'胜率%':>6s}")
    for label, r in res.items():
        x = r[per]; print(f"  {label:16s}{x['cum']:>+8.1f}{(x['sharpe'] if x['sharpe'] is not None else float('nan')):>7.2f}{x['dd']:>9.1f}{x['n']:>5d}{(x['win'] if x['win'] is not None else float('nan')):>6.0f}")
