# -*- coding: utf-8 -*-
"""东证期货在焦煤上到底赚没赚(2026-09-01 运营者:「东证这个对冲赚到钱了吗?」)。

**这与 REPORT_JM_CAL_BOOK_v1 问的不是同一件事。** 那份问的是「跟着它的结构做
赚不赚」(答案:扣成本三年 −15%);这份问的是**它自己的账**。两者会分叉,因为:
  * 它的账含**净敞口**(当前净空 4,284 手),跟随卡是按两腿比例等比缩的;
  * 我们看不到成交明细,只能按**持仓 × 结算价变动**逐日盯市推算 —— 与平台
    「盈亏商品」页(DEC-157)同一套口径,不另起一套。

**三个不可回避的口径缺陷,先说清楚**:
  1. **掉榜日持仓未知**:交易所只公布前 20,它掉出去那天我们不知道它持有多少。
     本文按「未知那天不计盈亏」处理(不是按 0,那会凭空造出一次清仓)。
  2. **看不到成交价**:建仓、平仓的真实价格无从得知,盯市只能算持仓期间的价格变动,
     **算不出它已实现的部分**。所以下面的数字是「持仓浮动盈亏之和」,不是它的真实损益表。
  3. **三禾口径补的是持仓不是成交**:焦煤席位史起于 2023-08,更早的没有。

跑法:python research/run_jm_dz_pnl.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

MEMBER = sys.argv[1] if len(sys.argv) > 1 else "东证期货"
MULT = 60.0                      # 焦煤点值,与合约窗、跟随卡同一个数
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "jm_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "jm_seat.csv.gz"))
H.use("JM")
mkt = H.main_series(price)
op, st = H.contract_prices(price)          # st = 逐合约结算价
idx = mkt.index[mkt.index >= seat["trade_date"].min()]

dz = seat[seat["member_key"] == MEMBER]
# 逐日逐合约净持仓(可见口径:掉榜那天没有行 = 未知,不补 0)
net = dz.pivot_table(index="trade_date", columns="contract", values="net_off", aggfunc="sum")
net = net.reindex(idx)

L = [f"东证期货在焦煤上的盯市盈亏(样本 {idx[0].date()} ~ {idx[-1].date()},{len(idx)} 个交易日)", ""]
L.append("口径:当日盈亏 = 昨日净持仓 × (今结算 − 昨结算) × 点值 60,逐合约各算各的再相加。")
L.append("**掉榜日不计**(持仓未知),**已实现部分算不出**(看不到成交价)——这是持仓浮动之和。")
L.append("")

daily = pd.Series(0.0, index=idx)
known = pd.Series(0, index=idx)
for c in net.columns:
    if not isinstance(c, str) or c not in st.columns:
        continue
    pos = net[c]
    settle = st[c].reindex(idx)
    d_settle = settle.diff()
    # 昨天在榜、今天有结算价差,才算得出这一天
    ok = pos.shift(1).notna() & d_settle.notna()
    contrib = (pos.shift(1) * d_settle * MULT).where(ok, 0.0)
    daily += contrib.fillna(0.0)
    known += ok.astype(int)

L.append("## 一、逐年")
L.append("")
L.append(f"{'年份':<8}{'盯市盈亏(万元)':>16}{'可算天数':>10}{'年末净持仓':>12}")
L.append("-" * 48)
for y, g in daily.groupby(daily.index.year):
    days = int((known[known.index.year == y] > 0).sum())
    last_day = net[net.index.year == y].dropna(how="all").index.max()
    year_net = net.loc[last_day].sum() if last_day is not None else np.nan
    L.append(f"{y:<8}{g.sum() / 1e4:>+16.1f}{days:>10}{int(year_net):>12,}")
L.append("-" * 48)
L.append(f"{'合计':<8}{daily.sum() / 1e4:>+16.1f}")
L.append("")

# ------------------------------------------------ 当前这一段跨月结构
L.append("## 二、当前这个跨月簿,从什么时候开始、赚了多少")
L.append("")


def shape_of(row):
    """与净持仓页/跟随卡同一套判定:两腿比过 1:3 算纯趋势。"""
    r = row.dropna()
    lo = r[r > 0].sum()
    sh = -r[r < 0].sum()
    if lo <= 0 or sh <= 0:
        return "单边"
    if min(lo, sh) * 3 < max(lo, sh):
        return "纯趋势"
    far = r[r > 0].idxmax()
    near = r[r < 0].idxmin()
    return "多远空近" if far > near else "多近空远"


shapes = pd.Series({d: shape_of(net.loc[d]) for d in idx})
cur = shapes.iloc[-1]
# 往回找这一段连续同形态的起点
start = idx[-1]
for d in reversed(idx[:-1]):
    if shapes[d] != cur:
        break
    start = d
seg = daily[daily.index >= start]
L.append(f"当前形态:**{cur}**,自 **{start.date()}** 起连续 {len(seg)} 个交易日。")
L.append(f"这一段的盯市盈亏:**{seg.sum() / 1e4:+.1f} 万元**。")
L.append("")
L.append(f"{'形态':<10}{'天数':>7}{'盯市盈亏(万元)':>16}{'日均(万元)':>12}")
L.append("-" * 46)
for name, g in daily.groupby(shapes):
    L.append(f"{name:<10}{len(g):>7}{g.sum() / 1e4:>+16.1f}{g.mean() / 1e4:>+12.2f}")
L.append("")
L.append("**「多远空近」那一行才是运营者问的那个对冲簿。** 它与「纯趋势」「单边」")
L.append("分开看,才知道它赚的钱是来自跨月结构,还是来自它单边压方向的那些日子。")


# ------------------------------------------ 三、把「方向」和「价差」拆开
# 运营者问的是「**这个对冲**赚没赚」,而总账里混着它净敞口跟大方向赚赔的部分
# (当前净空 4,284 手)。拆法:
#   方向部分 = 当日净持仓合计 × 主力结算价变动 × 点值   ——它压方向赚的
#   价差部分 = 总盯市 − 方向部分                        ——两腿之间相对变动赚的
# 这是**这张跟随卡真正在复制的那一块**:等比缩两腿,复制的是价差不是敞口。
L.append("")
L.append("## 三、拆开看:它赚的是**方向**还是**价差**")
L.append("")
main_settle = pd.Series(
    [st[m].asof(d) if isinstance(m, str) and m in st.columns else np.nan
     for d, m in zip(idx, mkt["main"].reindex(idx))], index=idx)
d_main = main_settle.diff()
total_net = net.sum(axis=1, min_count=1)
dir_pnl = (total_net.shift(1) * d_main * MULT).fillna(0.0)
sp_pnl = daily - dir_pnl
L.append(f"{'形态':<10}{'天数':>7}{'总盯市':>12}{'方向部分':>12}{'价差部分':>12}")
L.append("-" * 55)
for name, g in daily.groupby(shapes):
    m = shapes == name
    L.append(f"{name:<10}{len(g):>7}{g.sum()/1e4:>+12.1f}{dir_pnl[m].sum()/1e4:>+12.1f}"
             f"{sp_pnl[m].sum()/1e4:>+12.1f}")
L.append("-" * 55)
L.append(f"{'合计':<10}{len(daily):>7}{daily.sum()/1e4:>+12.1f}{dir_pnl.sum()/1e4:>+12.1f}"
         f"{sp_pnl.sum()/1e4:>+12.1f}")
L.append("(单位:万元)")
L.append("")
L.append("**跟随卡等比缩的是两腿比例,复制的是「价差部分」,不是「方向部分」。**")
L.append("所以判断这张卡值不值得看,该看的是价差那一列。")
L.append("")
seg_m = daily.index >= start
L.append(f"当前这一段({start.date()} 起 {len(seg)} 天):总盯市 {seg.sum()/1e4:+.1f} 万 = "
         f"方向 {dir_pnl[seg_m].sum()/1e4:+.1f} 万 + 价差 {sp_pnl[seg_m].sum()/1e4:+.1f} 万。")

io.open(OUT / "jm_dz_pnl.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
