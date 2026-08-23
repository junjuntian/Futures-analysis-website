"""生猪做多门「本轮卸仓 ≥X%」扫门槛(DEC-127):固定 5 家 + long_since 2026-01-01 + 现行策略,只换 long_unload_min。
跑法:仓库根目录 python research/run_lh_unload_min.py"""
import sys, pathlib, pandas as pd, numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
D = pathlib.Path(__file__).resolve().parent / "data"
price = H.clean_price(pd.read_csv(D / "lh_price.csv.gz")); seat = H.clean_seat(pd.read_csv(D / "lh_seat.csv.gz"))
v = H.use("LH"); H.CURRENT = {"code": "LH", **v}
mkt = H.main_series(price); op, st = H.contract_prices(price); mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
rdf, _ = H.retail_series(seat, mkt.index)
g, _, _ = H.fixed_groups(H.RULES["fixed_members"], seat, price, mkt.index, "2026-08-23")
sig0 = H.signal_series(seat, g)
unl = H.unload_series(sig0, seat, g)["pct"].reindex(mkt.index)
print(f"固定 5 家 2026 卸仓分布:最高 {unl[unl.index>='2026-01-01'].max()*100:.0f}%;≥30% 天数 {(unl[unl.index>='2026-01-01']>=0.3).sum()},≥35% {(unl[unl.index>='2026-01-01']>=0.35).sum()},≥40% {(unl[unl.index>='2026-01-01']>=0.4).sum()},≥45% {(unl[unl.index>='2026-01-01']>=0.45).sum()}")
print(f"\n{'门槛':>5}{'2026做多笔':>9}{'做多累计%':>9}{'做多胜%':>7}{'2026全部累计%':>12}{'夏普':>6}{'回撤%':>7}{'近一年累计%':>10}{'夏普':>6}{'回撤%':>7}  2026 做多逐笔")
for th in (0.50, 0.45, 0.40, 0.35, 0.30, 0.25):
    H.RULES["long_unload_min"] = th
    sig = H.attach_bounce_long(sig0.copy(), seat, mkt, g)
    tr, pos, daily = H.replay(sig, mkt, rdf, op, st)
    y = daily[daily.index >= "2026-01-01"]; p26 = H._perf(y); py = H._perf(daily[daily.index >= mkt.index[-1] - pd.Timedelta(days=365)])
    L = [t for t in tr if t["side"] == "long" and pd.Timestamp(t["entry_date"]) >= pd.Timestamp("2026-01-01")]
    cum = (np.prod([1 + t["ret_pct"] / 100 for t in L]) - 1) * 100 if L else 0.0
    win = (sum(t["ret_pct"] > 0 for t in L) / len(L) * 100) if L else float("nan")
    detail = " ".join(f"{t['entry_date'][5:]}→{(t['exit_date'] or '持有')[5:]}{t['ret_pct']:+.1f}" for t in L)
    print(f"{th*100:>4.0f}%{len(L):>9}{cum:>+9.1f}{win:>7.0f}{p26['cum_pct']:>+12.1f}{p26['sharpe']:>6.2f}{p26['max_dd_pct']:>7.1f}{py['cum_pct']:>+10.1f}{py['sharpe']:>6.2f}{py['max_dd_pct']:>7.1f}  {detail}")
