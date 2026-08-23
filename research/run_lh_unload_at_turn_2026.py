"""2026 生猪跨月价差触底/见顶时,机构本轮已卸掉多少(固定 5 家)—— 运营者要的「反弹参考区间」:
历次价差到最小值(低位拐头)那天机构的卸仓比例落在什么范围;见顶同理。DEC-128。
跑法:仓库根目录 python research/run_lh_unload_at_turn_2026.py"""
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
sm = sm[(sm.trade_date >= "2025-12-01")].sort_values(["contract_1", "contract_2", "trade_date"])
W = 10   # 前后 10 日的极值
rows = []
for (a, b), grp in sm.groupby(["contract_1", "contract_2"]):
    s = grp.set_index("trade_date")["spread"]; p = grp.set_index("trade_date")["pair_position"]
    if len(s) < 2 * W + 5: continue
    width = max(s.max() - s.min(), 1)
    for i in range(W, len(s) - 5):
        d = s.index[i]
        if d < pd.Timestamp("2026-01-01"): continue
        seg = s.iloc[i - W:i + W + 1]; after = s.iloc[i + 1:i + 21]
        if s.iloc[i] == seg.min() and len(after) and (after.max() - s.iloc[i]) / width >= 0.10:
            rows.append(dict(kind="底", pair=f"{a}-{b[2:]}", date=d, spread=s.iloc[i], pos=p.iloc[i], unload=unl.get(d, np.nan), side=side.get(d, np.nan),
                             bounce=(after.max() - s.iloc[i]), bounce_pct=(after.max() - s.iloc[i]) / width * 100))
        if s.iloc[i] == seg.max() and len(after) and (s.iloc[i] - after.min()) / width >= 0.10:
            rows.append(dict(kind="顶", pair=f"{a}-{b[2:]}", date=d, spread=s.iloc[i], pos=p.iloc[i], unload=unl.get(d, np.nan), side=side.get(d, np.nan),
                             bounce=(s.iloc[i] - after.min()), bounce_pct=(s.iloc[i] - after.min()) / width * 100))
df = pd.DataFrame(rows).sort_values(["kind", "date"])
# 同日同波去重:同一天多对只留一条(取反弹最大的)看「独立事件」
ind = df.sort_values("bounce_pct", ascending=False).drop_duplicates(["kind", "date"]).sort_values(["kind", "date"])
for k in ("底", "顶"):
    x = df[df.kind == k]; xi = ind[ind.kind == k]
    print(f"\n=== 2026 生猪跨月价差「{k}」(±{W} 日极值,之后 20 日反向 ≥ 区间 10%):{len(x)} 个组合事件 / 独立日 {len(xi)} ===")
    print(f"{'日期':<11}{'组合':<14}{'价差':>7}{'当年位置':>7}{'机构':>4}{'卸仓%':>6}{'20日反向幅度':>10}")
    for _, r in xi.iterrows():
        print(f"{r.date.date()}  {r.pair:<14}{r.spread:>+7.0f}{r.pos*100:>6.0f}%{('空' if r.side<0 else '多'):>4}{r.unload*100:>6.0f}{r.bounce:>+10.0f}")
    u = xi.unload.dropna() * 100
    print(f"  机构净空占比 {(xi.side<0).mean()*100:.0f}% | 卸仓% 分位:最小 {u.min():.0f} / 25% {u.quantile(.25):.0f} / 中位 {u.median():.0f} / 75% {u.quantile(.75):.0f} / 最大 {u.max():.0f}  (均值 {u.mean():.0f})")
    ua = x.unload.dropna() * 100
    print(f"  全部组合事件卸仓% 分位:25% {ua.quantile(.25):.0f} / 中位 {ua.median():.0f} / 75% {ua.quantile(.75):.0f}")
print("\n对照:2026 机构净空日卸仓% 的无条件分布 —— 25% {:.0f} / 中位 {:.0f} / 75% {:.0f}".format(*(unl[(unl.index>='2026-01-01')&(side<0)].dropna().quantile([.25,.5,.75])*100)))
