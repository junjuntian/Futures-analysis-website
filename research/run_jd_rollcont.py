"""鸡蛋换月接力回测(DEC-147 扩展)。跑法:python research/run_jd_rollcont.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / "jd_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "jd_seat.csv.gz"))
v = H.use("JD")
_rs = pd.Timestamp(H.RULES["replay_start"])
price = price[price["trade_date"] >= _rs]; seat = seat[seat["trade_date"] >= _rs]
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= _rs]
groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
sig = H.signal_series(seat, groups)
sig = H.attach_cost_signal(sig, seat, mkt, groups)
rdf, rhave = H.retail_series(seat, mkt.index)

def perf(d):
    d = pd.Series(d).dropna()
    eq = (1 + d).cumprod()
    return ((float(eq.iloc[-1]) - 1) * 100,
            float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan,
            float((eq / eq.cummax() - 1).min()) * 100)

L = [f"鸡蛋换月接力回测(数据至 {mkt.index[-1].date()})", ""]
res = {}
for flag, tag in ((False, "现行"), (True, "开接力")):
    H.RULES["roll_continue"] = flag
    trades, pos, daily = H.replay(sig, mkt, rdf, op, st)
    res[flag] = trades
    closed = [t for t in trades if t["exit_date"]]
    rolls = [t for t in trades if t.get("rolled_from")]
    c_, s_, m_ = perf(daily)
    L.append(f"{tag}: {len(closed)} 笔已平  复利 {c_:+.1f}%  夏普 {s_:.2f}  回撤 {m_:+.1f}%  接力笔 {len(rolls)}")
H.RULES["roll_continue"] = False
L.append("")
rolls = [t for t in res[True] if t.get("rolled_from")]
L.append("接力明细:")
for t in rolls:
    L.append(f"  {t['entry_date']} {t['side']} {t['rolled_from']}→{t['contract']} @{t['entry_px']}  "
             f"出 {t['exit_date'] or '持有中'}({t['exit_reason'] or '—'})  {t['ret_pct']:+.2f}%  持 {t['hold_days']}日")
rr = [t["ret_pct"] for t in rolls if t["exit_date"]]
if rr:
    L.append(f"接力笔小计: {len(rr)} 笔  均 {np.mean(rr):+.2f}%  合计 {np.sum(rr):+.1f}pp  胜率 {(np.array(rr)>0).mean()*100:.0f}%")
txt = "\n".join(L)
io.open(OUT / "jd_rollcont.txt", "w", encoding="utf-8").write(txt)
