# -*- coding: utf-8 -*-
"""焦煤:各席位的盯市盈亏,拆成「方向」与「价差」两块(2026-09-01)。

运营者先问东证赚没赚(`REPORT_JM_DZ_PNL_v1`:方向赚、价差三年亏 1.16 亿),
再问「中信也是这样对冲,中信赚钱吗」。与其一家一家跑,不如把在场够多的席位
全测一遍 —— 顺带回答那个更该问的问题:**焦煤上到底有没有哪家做价差是赚的。**

口径与 `run_jm_dz_pnl.py` 一字不差:
  当日盈亏 = 昨日净持仓 × (今结算 − 昨结算) × 点值 60,逐合约各算各的再相加;
  方向部分 = 当日净持仓合计 × 主力结算价变动 × 点值;
  价差部分 = 总盯市 − 方向部分。
**跟随卡等比缩两腿,复制的是价差那一块** —— 所以该看的是价差列。

口径缺陷照旧(席位=该会员名下全部客户+自营、掉榜日不计、看不到成交价所以
算不出已实现部分、做市型套利赚的买卖价差这套完全看不见)。

跑法:python research/run_jm_seat_pnl.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

MULT = 60.0
MIN_DAYS = 150                    # 在场天数下限,少于此的数字不稳
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "jm_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "jm_seat.csv.gz"))
H.use("JM")
mkt = H.main_series(price)
op, st = H.contract_prices(price)
idx = mkt.index[mkt.index >= seat["trade_date"].min()]
main_settle = pd.Series(
    [st[m].asof(d) if isinstance(m, str) and m in st.columns else np.nan
     for d, m in zip(idx, mkt["main"].reindex(idx))], index=idx)
d_main = main_settle.diff()


def shape_of(row):
    """与净持仓页/跟随卡同一套判定:两腿比过 1:3 算纯趋势。"""
    r = row.dropna()
    lo = r[r > 0].sum()
    sh = -r[r < 0].sum()
    if lo <= 0 or sh <= 0:
        return "单边"
    if min(lo, sh) * 3 < max(lo, sh):
        return "纯趋势"
    return "多远空近" if r[r > 0].idxmax() > r[r < 0].idxmin() else "多近空远"


def analyse(member):
    sub = seat[seat["member_key"] == member]
    net = sub.pivot_table(index="trade_date", columns="contract",
                          values="net_off", aggfunc="sum").reindex(idx)
    daily = pd.Series(0.0, index=idx)
    for c in net.columns:
        if not isinstance(c, str) or c not in st.columns:
            continue
        ds = st[c].reindex(idx).diff()
        ok = net[c].shift(1).notna() & ds.notna()
        daily += (net[c].shift(1) * ds * MULT).where(ok, 0.0).fillna(0.0)
    dir_pnl = (net.sum(axis=1, min_count=1).shift(1) * d_main * MULT).fillna(0.0)
    sp_pnl = daily - dir_pnl
    shapes = pd.Series({d: shape_of(net.loc[d]) for d in idx})
    on = int(net.notna().any(axis=1).sum())
    cal = shapes.isin(["多远空近", "多近空远"])          # 真在做跨月的那些天
    return {"m": member, "on": on, "total": daily.sum(), "dir": dir_pnl.sum(),
            "sp": sp_pnl.sum(), "cal_days": int(cal.sum()),
            "cal_total": daily[cal].sum(), "cal_sp": sp_pnl[cal].sum(),
            "last_shape": shapes.iloc[-1]}


counts = seat.groupby("member_key")["trade_date"].nunique()
rows = [analyse(m) for m, c in counts.items() if c >= MIN_DAYS]
rows = [r for r in rows if r["on"] >= MIN_DAYS]
rows.sort(key=lambda r: -r["sp"])

L = [f"焦煤各席位盯市盈亏拆解(样本 {idx[0].date()} ~ {idx[-1].date()},{len(idx)} 个交易日)", ""]
L.append(f"在场 ≥{MIN_DAYS} 天的 {len(rows)} 家。单位:万元。**按「价差部分」排序** ——")
L.append("那才是跟随卡复制的那一块;「方向部分」是它压大方向赚赔的,复制不了也不该复制。")
L.append("")
L.append(f"{'席位':<10}{'在场天':>7}{'总盯市':>11}{'方向部分':>11}{'价差部分':>11}"
         f"{'跨月天数':>9}{'跨月期总账':>11}{'跨月期价差':>11}  当前形态")
L.append("-" * 96)
for r in rows:
    L.append(f"{r['m']:<10}{r['on']:>7}{r['total']/1e4:>+11.0f}{r['dir']/1e4:>+11.0f}"
             f"{r['sp']/1e4:>+11.0f}{r['cal_days']:>9}{r['cal_total']/1e4:>+11.0f}"
             f"{r['cal_sp']/1e4:>+11.0f}  {r['last_shape']}")
L.append("-" * 96)
pos = [r for r in rows if r["sp"] > 0]
L.append("")
L.append(f"**价差部分为正的席位:{len(pos)} / {len(rows)} 家。**")
L.append(f"全体合计:总盯市 {sum(r['total'] for r in rows)/1e4:+,.0f} 万 = "
         f"方向 {sum(r['dir'] for r in rows)/1e4:+,.0f} 万 + 价差 "
         f"{sum(r['sp'] for r in rows)/1e4:+,.0f} 万。")
L.append("")
# 这里原本写着「价差是零和场,全体加总必然接近负数」——**被本表自己的数推翻了**
# (全体价差合计是正的)。原因两条,都得说清楚,不能留一句想当然的话:
#   1. 我们只看得到**每个合约的前 20 名**,不是全市场。输钱的那些散户与小席位
#      根本不在表里,零和的另一半是缺失的;
#   2. 「价差部分」是**总盯市减去方向部分的残差**,里面还含着各合约相对主力的
#      基差变动,不是一笔干净的价差损益。
L.append("**别把「价差部分」当成干净的套利损益**:①榜单只有前 20 名,输钱的那一半")
L.append("不在表里,所以全体加总为正不代表这行当稳赚;②它是「总盯市 − 方向部分」的")
L.append("残差,还含着各合约对主力的基差变动。看**单家在真正跨月的那些天**赚没赚,")
L.append("才是对得上问题的那个数 —— 即右边「跨月期价差」那一列。")
io.open(OUT / "jm_seat_pnl.txt", "w", encoding="utf-8").write("\n".join(L))
print(f"done: {len(rows)} seats, {len(pos)} with positive spread P&L")
