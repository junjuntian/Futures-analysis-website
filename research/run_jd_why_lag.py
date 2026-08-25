"""鸡蛋战役制为什么落后:量化「确认等待成本」。跑法:python research/run_jd_why_lag.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
import campaign as C

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / "jd_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "jd_seat.csv.gz"))
v = H.use("JD")
_rs = pd.Timestamp(H.RULES["replay_start"])
price = price[price["trade_date"] >= _rs]
seat = seat[seat["trade_date"] >= _rs]
_ok = price.dropna(subset=["open_interest"])["trade_date"].unique()
price = price[price["trade_date"].isin(_ok)]
seat = seat[seat["trade_date"].isin(_ok)]
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= _rs]
roll, _, _ = H.rolling_groups(seat, price, mkt.index)
GRP = list(roll.dropna().iloc[-1])
K = 0.37
H.RULES["campaign"] = {"add_min": 1000.0 * K, "confirm": 5000.0 * K, "gap": 3, "tail": 10,
                       "unload": 0.30, "share": 0.25, "max_units": 3}
H.RULES["strategy"] = "campaign"
out = C.run(seat, mkt, op, st, GRP, H.RULES)
trades = [t for t in out["trades"] if t["exit_date"] is not None]

lags, missed, holds = [], [], []
for t in trades:
    c, side = t["contract"], (1 if t["side"] == "long" else -1)
    px = st[c].dropna()
    w = C.camp_frame(seat, c, GRP, px)
    net = w.sum(axis=1)
    e = pd.Timestamp(t["entry_date"])
    # 机构方向确立日:进场前,阵营净持仓最后一次翻成本方向的那天(连续同向段起点)
    pre = net[net.index <= e]
    sgn = np.sign(pre)
    est = None
    run = sgn[sgn == side]
    if len(run):
        # 找包含进场日的连续同向段起点
        idxs = sgn.index.tolist()
        j = len(idxs) - 1
        while j > 0 and sgn.iloc[j - 1] == side:
            j -= 1
        est = idxs[j] if sgn.iloc[-1] == side else None
    if est is None:
        continue
    p0, p1 = px.asof(est), px.asof(e)
    if not (np.isfinite(p0) and np.isfinite(p1) and p0):
        continue
    lags.append(int(np.busday_count(est.date(), e.date())))
    missed.append(side * (float(p1) / float(p0) - 1) * 100)
    holds.append(t["hold_days"])

L = [f"鸡蛋战役制「确认等待成本」(可归因 {len(lags)}/{len(trades)} 笔)", ""]
L.append(f"机构方向确立 -> 战役制进场:滞后中位 {np.median(lags):.0f} 个交易日(均值 {np.mean(lags):.1f})")
L.append(f"这段里已走掉的有利行情:中位 {np.median(missed):+.2f}%  均值 {np.mean(missed):+.2f}%  合计 {np.sum(missed):+.1f}pp")
L.append(f"(对照:战役制已平仓 64 笔合计 +62.4pp —— 等确认漏掉的量级与吃到的量级同一档)")
L.append(f"进场后持有中位 {np.median(holds):.0f} 日;现行成本引擎进场时机构建仓轮龄中位 4 日(DEC-112 实测)")
d = pd.Series(missed)
L.append(f"滞后期行情 >1% 已走掉的占 {(d>1).mean()*100:.0f}%,>3% 的占 {(d>3).mean()*100:.0f}%")
txt = "\n".join(L)
io.open(OUT / "jd_why_lag.txt", "w", encoding="utf-8").write(txt)
