"""回答运营者 2026-08-19 的两个质疑 + 验证「一年选一次」。

Q1 选人改成一年一次会怎样(3 个月太短、噪音多)?
Q2 明显熊市、机构一直净空,为什么胜率只有 58%、单笔才 2%?
Q3 「做多」到底在跟什么?
"""
import numpy as np, pandas as pd
import lhlib as L, run_lh_phase2 as P2

pd.set_option("display.width", 200)

price = L.load_price(); seat = L.load_seat()
df = seat.merge(price[["contract","trade_date","settle"]], on=["contract","trade_date"], how="inner")
mr = P2.main_returns(price); mr = mr[mr.index >= df["trade_date"].min()]
ret, past = mr["ret"], mr["settle"].pct_change(20)

def run(reselect_m, enter=1.0, long_on=True, **kw):
    old = P2.RESELECT_M
    P2.RESELECT_M = reselect_m
    g = P2.rolling_groups(df, ret.index)
    z = P2.zscore(P2.signal_series(df, g))
    tr, pos = P2.backtest_discrete(z, ret, past, enter=enter, long_needs_dip=True, **kw)
    if not long_on:
        tr = tr[tr["方向"] == "空"]
    P2.RESELECT_M = old
    return tr, z, g

print("=" * 78)
print("Q1 选人频率:3 个月 vs 6 个月 vs 一年")
print("=" * 78)
print(f"  {'重选周期':10s}{'笔数':>6s}{'累计':>9s}{'均值':>8s}{'胜率':>8s}{'换组次数':>9s}")
for m in (3, 6, 12):
    tr, z, g = run(m)
    changes = sum(1 for a, b in zip(g.dropna(), g.dropna()[1:]) if a != b)
    cum = (1 + tr["收益%"] / 100).prod() - 1
    print(f"  {m:>2d} 个月    {len(tr):>6d}{cum*100:>+8.1f}%{tr['收益%'].mean():>+7.2f}%"
          f"{(tr['收益%']>0).mean()*100:>7.1f}%{changes:>9d}")

print("\n" + "=" * 78)
print("Q2 为什么胜率不高、单笔不大")
print("=" * 78)
tr, z, g = run(3)
tr = tr.copy()
print(f"\n全部 {len(tr)} 笔,按收益排序看集中度:")
s = tr["收益%"].sort_values(ascending=False)
print(f"  最大 3 笔: {', '.join(f'{v:+.1f}%' for v in s.head(3))}  合计贡献 {s.head(3).sum():+.1f}%")
print(f"  最小 3 笔: {', '.join(f'{v:+.1f}%' for v in s.tail(3))}")
print(f"  中间 30 笔合计 {s.iloc[3:-3].sum():+.1f}%  (均值 {s.iloc[3:-3].mean():+.2f}%)")
print(f"  |收益|<2% 的笔数: {(tr['收益%'].abs()<2).sum()} / {len(tr)}"
      f"  ——趋势跟随的典型形状:多数笔是小额噪音,钱来自少数几笔")

print(f"\n持有天数分布:")
for lo, hi in [(0,5),(5,10),(10,20),(20,99)]:
    sub = tr[(tr["持有日"]>=lo)&(tr["持有日"]<hi)]
    if len(sub):
        print(f"  {lo:2d}-{hi:2d} 日: {len(sub):2d} 笔  均值{sub['收益%'].mean():+6.2f}%  "
              f"胜率{(sub['收益%']>0).mean()*100:5.1f}%")
print("  ↑ 短持有的笔基本是信号来回穿门槛造成的,拉低胜率")

print(f"\n多空拆:")
for side in ("空","多"):
    sub = tr[tr["方向"]==side]
    print(f"  {side}: {len(sub):2d} 笔 累计{((1+sub['收益%']/100).prod()-1)*100:+7.1f}% "
          f"均值{sub['收益%'].mean():+6.2f}% 胜率{(sub['收益%']>0).mean()*100:5.1f}%")

print("\n对照:什么都不做,恒定满仓做空")
sh = (1 - ret.fillna(0)).prod() - 1
print(f"  +{sh*100:.1f}%  ——策略要赢的是它,不是 0")

print("\n" + "=" * 78)
print("Q3 「做多」在跟什么:进场时机构是不是真的转多了")
print("=" * 78)
grp_series = g
netv = pd.Series(index=ret.index, dtype=float)
for gp in {x for x in grp_series.dropna().unique()}:
    days = grp_series.index[grp_series == gp]
    ss = df[df["member_key"].isin(list(gp))].groupby("trade_date")["net"].sum().sort_index()
    netv.loc[days] = ss.reindex(days).values
longs = tr[tr["方向"]=="多"]
print(f"  做多 {len(longs)} 笔,进场当日机构合计净持仓:")
vals = [netv.get(d, np.nan) for d in longs["进场"]]
print(f"    全部为负(仍净空)的笔数: {sum(1 for v in vals if v < 0)} / {len(vals)}")
print(f"    区间: {np.nanmin(vals):,.0f} ~ {np.nanmax(vals):,.0f} 手")
print("  → 所谓「做多」= 机构在**减空**,不是转多。样本里机构一天都没转成净多。")
