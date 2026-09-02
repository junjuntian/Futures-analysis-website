# -*- coding: utf-8 -*-
"""铁矿石该用哪三家散户席位(2026-09-02 运营者答「换一组」)。

## 为什么要换

全站现在用的是**跨品种固定**的三家:东方财富 / 平安期货 / 徽商期货
(进判据),外加方正中期 / 中信建投 只做五窗展示。铁矿石页面上这几家
**大面积「未上榜」**,五窗里一半格子是空的,对照没有意义。

## 选人的判据 —— **结构性的,不是「反着做最赚钱」**

散户席位当初是按**它是什么**选的,不是按**反着做回测多好看**选的:
「这几家长期站多头、长期亏钱,所以反向取用」。铁矿石这里照同一条路走,
判据**跑之前写死**:

  1. **上榜率 >= 60%** —— 当日不在榜就没法当日用,窗口里全是空格;
  2. **常年偏多:净多天数占比 >= 70%** —— 「散户站多头」是这条线的前提,
     一个多空各半的席位不是散户代表,是做市或对冲盘;
  3. **盯市盈亏累计为负** —— 「长期亏钱」是反向取用的另一半前提。

三条全过才进候选。**「反向跟它的择时增益」只报不选** —— 它是事后检查,
拿它来挑人就又变成 47 选 1 那种事(REPORT_I_FOLLOW_v1 刚栽过)。

## 这个脚本不改任何配置

它只产出名单与证据。真要换,是运营者看过之后的事。

跑法:python research/run_i_retail.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

MIN_BOARD_RATIO = 0.60      # 上榜率门槛
MIN_LONG_RATIO = 0.70       # 净多天数占比门槛
CURRENT_SEED = ["东方财富", "平安期货", "徽商期货"]
CURRENT_PANEL = ["东方财富", "方正中期", "徽商期货", "平安期货", "中信建投"]

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "i_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "i_seat.csv.gz"))
H.use("I")
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
idx = mkt.index
n = len(idx)
mult = float(H.RULES["multiplier"])
_op, st = H.contract_prices(price)
ropen = mkt["ret_open"].fillna(0.0).values


def profile(member: str) -> dict | None:
    """一家席位的画像:上榜率、偏多程度、盯市盈亏、反向择时增益。"""
    sub = seat[seat["member_key"] == member]
    if sub.empty:
        return None
    # 逐日净持仓(全合约合计)。只算它真上榜的那些天,不 ffill ——
    # 「掉榜不等于清仓」,但这里要量的恰恰是「它在不在榜」。
    daily = sub.groupby("trade_date")["net"].sum()
    daily = daily[daily.index.isin(idx)]
    board = len(daily)
    if board < 20:
        return None
    long_days = int((daily > 0).sum())

    # 盯市盈亏:昨日净持仓 ×(今结算 − 昨结算)× 点值,逐合约各算各的再相加。
    pnl = 0.0
    wide = sub.pivot_table(index="trade_date", columns="contract",
                           values="net", aggfunc="sum").reindex(idx)
    for c in wide.columns:
        if not isinstance(c, str) or c not in st.columns:
            continue
        ds = st[c].reindex(idx).diff()
        ok = wide[c].shift(1).notna() & ds.notna()
        pnl += float((wide[c].shift(1) * ds * mult).where(ok, 0.0).fillna(0.0).sum())

    # 反向择时:照它方向的**反面**做,与「同期一律做多」比。只报不选。
    pos = np.sign(daily.reindex(idx).ffill())
    pos[pos == 0] = np.nan
    p = (-pos).shift(2)
    live = p.notna() & (p != 0)
    edge = np.nan
    if live.sum() >= 60:
        k = 242 / live.sum()
        mine = (float(np.prod(1 + (p * mkt["ret_open"])[live])) ** k - 1) * 100
        lng = (float(np.prod(1 + mkt["ret_open"][live])) ** k - 1) * 100
        edge = mine - lng
    return {
        "member": member,
        "board": board,
        "board_ratio": board / n,
        "long_ratio": long_days / board,
        "pnl_wan": pnl / 1e4,
        "rev_edge": edge,
    }


rows = [r for r in (profile(m) for m in sorted(seat["member_key"].unique())) if r]
table = pd.DataFrame(rows)

L = [f"铁矿石散户席位选人(样本 {idx[0].date()} ~ {idx[-1].date()},{n} 个交易日)", ""]
L.append("判据**跑前写死**,与全站散户名单同一条思路(结构性,不是「反着做最赚」):")
L.append(f"  ① 上榜率 >= {MIN_BOARD_RATIO*100:.0f}%  ② 净多天数占比 >= {MIN_LONG_RATIO*100:.0f}%"
         f"  ③ 盯市盈亏累计为负")
L.append("「反向择时增益」只报不选 —— 拿它挑人就又是一次事后选优。")
L.append("")

L.append("## 一、现在这五家在铁矿石上是什么样")
L.append("")
L.append(f"{'席位':<12}{'上榜天':>7}{'上榜率':>8}{'净多占比':>9}{'盯市(万)':>11}{'反向增益':>9}  在用")
L.append("-" * 70)
for m in CURRENT_PANEL:
    r = table[table["member"] == m]
    role = "判据三家" if m in CURRENT_SEED else "仅展示"
    if r.empty:
        L.append(f"{m:<12}{'—':>7}{'—':>8}{'—':>9}{'—':>11}{'—':>9}  {role}(样本不足/从未上榜)")
        continue
    r = r.iloc[0]
    ed = "  —  " if not np.isfinite(r["rev_edge"]) else f"{r['rev_edge']:+.1f}"
    L.append(f"{m:<12}{r['board']:>7.0f}{r['board_ratio']*100:>7.0f}%{r['long_ratio']*100:>8.0f}%"
             f"{r['pnl_wan']:>+11.0f}{ed:>9}  {role}")
L.append("")

# --------------------------------------------------------------- 候选池
cand = table[(table["board_ratio"] >= MIN_BOARD_RATIO)
             & (table["long_ratio"] >= MIN_LONG_RATIO)
             & (table["pnl_wan"] < 0)].copy()
L.append("## 二、三条判据全过的候选")
L.append("")
if cand.empty:
    L.append("**一家都没有。** 不硬凑 —— 判据是先写死的,过不了就是过不了。")
else:
    cand = cand.sort_values("pnl_wan")
    L.append(f"{'席位':<12}{'上榜天':>7}{'上榜率':>8}{'净多占比':>9}{'盯市(万)':>11}{'反向增益':>9}")
    L.append("-" * 60)
    for _, r in cand.iterrows():
        ed = "  —  " if not np.isfinite(r["rev_edge"]) else f"{r['rev_edge']:+.1f}"
        L.append(f"{r['member']:<12}{r['board']:>7.0f}{r['board_ratio']*100:>7.0f}%"
                 f"{r['long_ratio']*100:>8.0f}%{r['pnl_wan']:>+11.0f}{ed:>9}")
L.append("")

L.append("## 三、放宽一档看看边上有谁(只为让运营者看清取舍,不是候选)")
L.append("")
near = table[(table["board_ratio"] >= 0.40) & (table["long_ratio"] >= 0.60)
             & (table["pnl_wan"] < 0)].sort_values("pnl_wan").head(12)
L.append(f"{'席位':<12}{'上榜率':>8}{'净多占比':>9}{'盯市(万)':>11}{'反向增益':>9}")
L.append("-" * 52)
for _, r in near.iterrows():
    ed = "  —  " if not np.isfinite(r["rev_edge"]) else f"{r['rev_edge']:+.1f}"
    L.append(f"{r['member']:<12}{r['board_ratio']*100:>7.0f}%{r['long_ratio']*100:>8.0f}%"
             f"{r['pnl_wan']:>+11.0f}{ed:>9}")
L.append("")
L.append("## 四、怎么用这张表")
L.append("")
L.append("**判据三家(进进出场判据)与五窗展示是两份名单**,别混:前者动策略,")
L.append("后者只做对照。换判据那三家会改变方案 C 的进出场,属于改引擎,")
L.append("要按品种专属研究走(运营者已同意立项,见第 1 问)。")
L.append("**五窗展示那五家可以先换** —— 它不进任何判据,换了只是让对照有东西可看。")
io.open(OUT / "i_retail.txt", "w", encoding="utf-8").write("\n".join(L))
print("done ->", OUT / "i_retail.txt")
