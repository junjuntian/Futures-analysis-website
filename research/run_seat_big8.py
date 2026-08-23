"""任一品种:「大席位(平均净持仓≥中位)里按总盈亏取前 8」+ 现行择时收益前 5 对照(DEC-122 同口径)。
跑法:仓库根目录 python research/run_seat_big8.py JM [截点 YYYY-MM-DD,缺省=引擎最近一次重选切点]"""
import sys, pathlib, numpy as np, pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
code = sys.argv[1] if len(sys.argv) > 1 else "JM"
D = pathlib.Path(__file__).resolve().parent / "data"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz")); seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
H.use(code); mkt = H.main_series(price); mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
cut = pd.Timestamp(sys.argv[2]) if len(sys.argv) > 2 else pd.Timestamp([c for c in cuts if pd.Timestamp(c) <= mkt.index[-1]][-1])
d = seat[seat["trade_date"] < cut].merge(price[["contract", "trade_date", "settle"]], on=["contract", "trade_date"], how="inner")
d = d.sort_values(["member_key", "contract", "trade_date"]); g = d.groupby(["member_key", "contract"])
d["prev_net"] = g["net"].shift(); d["prev_settle"] = g["settle"].shift(); gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
d = d[d["prev_net"].notna() & (gap <= 5)].copy(); d["dpx"] = (d["settle"] - d["prev_settle"]) * H.RULES["multiplier"]
grp = d.groupby("member_key")
t = pd.DataFrame({"总盈亏(亿)": grp.apply(lambda s: (s["dpx"] * s["prev_net"]).sum(), include_groups=False) / 1e8,
                  "择时收益(亿)": (grp.apply(lambda s: (s["dpx"] * s["prev_net"]).sum(), include_groups=False) - grp.apply(lambda s: (s["dpx"] * s["prev_net"].mean()).sum(), include_groups=False)) / 1e8,
                  "平均净持仓(手)": grp["prev_net"].apply(lambda s: s.abs().mean()), "最大净持仓": grp["prev_net"].apply(lambda s: s.abs().max()),
                  "在榜天数": grp["trade_date"].nunique()})
t = t[t["在榜天数"] >= H.RULES["member_min_days"]]
last = seat[seat["trade_date"] == seat["trade_date"].max()].groupby("member_key")["net"].sum()
t["当前净持仓"] = last.reindex(t.index)
med = t["平均净持仓(手)"].median(); big = t[t["平均净持仓(手)"] >= med]
print(f"{H.VARIETIES[code]['name']}  截点 {cut.date()}(只用截点前数据),候选 {len(t)} 家,仓位≥中位({med:.0f} 手)的大席位 {len(big)} 家;数据至 {seat['trade_date'].max().date()}")
print("\n=== 大席位按总盈亏 前 8 ===")
print(big.sort_values("总盈亏(亿)", ascending=False).head(8).round(2).to_string())
print("\n=== 对照:现行滚动择时收益 前 5 ===")
print(t.sort_values("择时收益(亿)", ascending=False).head(5).round(2).to_string())
print("\n引擎当前组:", list(groups.iloc[-1]), " 最近换人:", log[-1]["date"] if log else None)
