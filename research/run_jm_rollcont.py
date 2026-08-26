"""焦煤换月接力回测(DEC-147 候选)。跑法:python research/run_jm_rollcont.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / "jm_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "jm_seat.csv.gz"))
v = H.use("JM")
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
sig = H.signal_series(seat, groups)
sig = H.attach_cost_signal(sig, seat, mkt, groups)
sig = H.attach_inst_exit(sig, seat, mkt, groups)
rdf, rhave = H.retail_series(seat, mkt.index)

def perf(d):
    d = pd.Series(d).dropna()
    eq = (1 + d).cumprod()
    return ((float(eq.iloc[-1]) - 1) * 100,
            float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan,
            float((eq / eq.cummax() - 1).min()) * 100)

res = {}
for flag in (False, True):
    H.RULES["roll_continue"] = flag
    trades, pos, daily = H.replay(sig, mkt, rdf, op, st)
    res[flag] = (trades, daily)
H.RULES["roll_continue"] = False

L = [f"焦煤换月接力回测(数据至 {mkt.index[-1].date()})", ""]
for flag, tag in ((False, "现行(DEC-131 原样)"), (True, "开接力")):
    trades, daily = res[flag]
    closed = [t for t in trades if t["exit_date"]]
    c_, s_, m_ = perf(daily)
    rolls = [t for t in trades if t.get("rolled_from")]
    L.append(f"{tag}: {len(closed)} 笔已平  复利 {c_:+.1f}%  夏普 {s_:.2f}  回撤 {m_:+.1f}%  接力笔 {len(rolls)}")
L.append("")
trades_on, _ = res[True]
rolls = [t for t in trades_on if t.get("rolled_from")]
L.append("接力明细(开接力后新增/改变的笔):")
for t in rolls:
    L.append(f"  {t['entry_date']} {t['side']} {t['rolled_from']}→{t['contract']} "
             f"@{t['entry_px']}  出 {t['exit_date'] or '持有中'}({t['exit_reason'] or '—'})  "
             f"{t['ret_pct']:+.2f}%  持 {t['hold_days']}日")
# 8/17 那笔专案
case = [t for t in trades_on if t["entry_date"] >= "2026-08-14" and t.get("rolled_from") == "JM2609"]
L.append("")
if case:
    t = case[0]
    L.append(f"运营者点名的 8/17 案:JM2609 纪律出场后接力 → {t['contract']} {t['side']} "
             f"@{t['entry_px']},{'持有中' if not t['exit_date'] else '已出 ' + t['exit_date']},"
             f"至今 {t['ret_pct']:+.2f}%")
else:
    L.append("8/17 案:开接力后未生成 JM2609→接力笔(查当日门条件)")
# 逐年对比
L.append("")
for flag, tag in ((False, "现行"), (True, "开接力")):
    d_ = res[flag][1]
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in pd.Series(d_).groupby(pd.Series(d_).index.year)}
    L.append(f"{tag} 逐年: " + "  ".join(f"{y}:{vv:+.1f}%" for y, vv in sorted(ys.items())))
txt = "\n".join(L)
io.open(OUT / "jm_rollcont.txt", "w", encoding="utf-8").write(txt)
