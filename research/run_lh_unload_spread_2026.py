"""2026 生猪跨月价差 × 机构本轮卸仓比例(固定 5 家):卸到多少时价差处于高位/低位、之后 20 日价差怎么走(DEC-128 立项)。
跑法:仓库根目录 python research/run_lh_unload_spread_2026.py"""
import sys, pathlib, numpy as np, pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
D = pathlib.Path(__file__).resolve().parent / "data"
price = H.clean_price(pd.read_csv(D / "lh_price.csv.gz")); seat = H.clean_seat(pd.read_csv(D / "lh_seat.csv.gz"))
H.use("LH"); mkt = H.main_series(price); mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
g, _, _ = H.fixed_groups(H.RULES["fixed_members"], seat, price, mkt.index, "2026-08-23")
sig = H.signal_series(seat, g); us = H.unload_series(sig, seat, g)
unl = us["pct"].reindex(mkt.index); side = np.sign(sig["net"]).reindex(mkt.index)
sm = pd.read_csv(D / "spread_monitor_daily.csv.gz", parse_dates=["trade_date"])
sm = sm[(sm.instrument_1 == "LH") & (sm.instrument_2 == "LH") & (sm.is_cross_variety.astype(str).str.lower().isin(["f", "false", "0"]))]
sm = sm[sm.trade_date >= "2026-01-01"].copy()
sm["unload"] = sm.trade_date.map(unl); sm["side"] = sm.trade_date.map(side)
sm = sm.sort_values(["contract_1", "contract_2", "trade_date"])
# 未来 20 日价差变化(同对,元/吨)与 20 日内最大上行(反弹幅度)
grp = sm.groupby(["contract_1", "contract_2"])
sm["fwd20"] = grp["spread"].shift(-20) - sm["spread"]
sm["fwd10"] = grp["spread"].shift(-10) - sm["spread"]
sm["max_up20"] = grp["spread"].transform(lambda s: s[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1)) - sm["spread"]
sm = sm[sm.side < 0].dropna(subset=["unload", "pair_position"])
print(f"2026 生猪跨月组合 × 机构净空日:{len(sm)} 组合日,{sm.groupby(['contract_1','contract_2']).ngroups} 个组合,{sm.trade_date.nunique()} 个交易日")
bins = [0, .1, .2, .3, .4, .5, 1.01]; labels = ["0-10%", "10-20%", "20-30%", "30-40%", "40-50%", "≥50%"]
sm["ub"] = pd.cut(sm.unload, bins, labels=labels, right=False)
def pos_lab(p): return "低位(<30%)" if p < .3 else ("高位(>70%)" if p > .7 else "中间")
sm["pos"] = sm.pair_position.apply(pos_lab)
print("\n=== ① 按卸仓档:价差当年位置分布、之后 10/20 日价差变化(元/吨,正=近强远弱=牛市价差赚)===")
print(f"{'卸仓档':<8}{'组合日':>6}{'均位置':>7}{'低位占比':>8}{'高位占比':>8}{'10日Δ':>8}{'20日Δ':>8}{'20日涨比':>8}{'20日内最大上行':>12}")
for lab in labels:
    x = sm[sm.ub == lab]
    if len(x) == 0: continue
    print(f"{lab:<8}{len(x):>6}{x.pair_position.mean()*100:>6.0f}%{(x.pair_position<.3).mean()*100:>7.0f}%{(x.pair_position>.7).mean()*100:>7.0f}%{x.fwd10.mean():>+8.0f}{x.fwd20.mean():>+8.0f}{(x.fwd20>0).mean()*100:>7.0f}%{x.max_up20.mean():>+12.0f}")
print("\n=== ② 卸仓档 × 价差位置:之后 20 日价差变化 / 涨的比例 / 组合日 ===")
pt = sm.pivot_table(index="ub", columns="pos", values="fwd20", aggfunc=["mean", lambda s: (s > 0).mean() * 100, "size"], observed=True)
print(pt.round(0).to_string())
print("\n=== ③ 逐日时间线(每周五):主力、卸仓、主要跨月组合位置 ===")
main_pairs = sm.groupby(["contract_1", "contract_2"]).size().sort_values(ascending=False).head(4).index.tolist()
wk = sorted(d for d in sm.trade_date.unique() if pd.Timestamp(d).weekday() == 4)
print(f"{'日期':<11}{'卸仓':>5}" + "".join(f"{a+'-'+b[2:]:>20}" for a, b in main_pairs))
for d in wk:
    row = f"{pd.Timestamp(d).date()}  {unl.get(pd.Timestamp(d), np.nan)*100:>4.0f}%"
    for a, b in main_pairs:
        r = sm[(sm.trade_date == d) & (sm.contract_1 == a) & (sm.contract_2 == b)]
        row += f"{(f'{r.spread.iloc[0]:+.0f} @{r.pair_position.iloc[0]*100:.0f}%' if len(r) else '—'):>20}"
    print(row)
