# -*- coding: utf-8 -*-
"""玻璃:东证 vs 永安,谁的择时与赚钱能力更强(2026-09-01 运营者三连问)。

运营者:「东证为什么会加多,这太奇怪了,东证的择时能力和赚钱能力怎么样,
永安更好还是东证更好」。

**三个问题分三节答,每节都用可核对的数**:
  一、赚钱能力 —— 逐日盯市盈亏,并拆成「方向」与「价差」两块
      (口径与 run_jm_dz_pnl.py 一字不差:昨日净持仓 ×(今结算−昨结算)× 点值;
       方向部分 = 当日净持仓合计 × 主力结算价变动 × 点值;价差 = 总账 − 方向);
  二、择时能力 —— 在场状态法的**择时增益**(照它方向做 vs 同样这些天一律做多)
      + p(方向)(在场日不动、只重掷每段方向)。**退化的明说不报**,
      不硬凑 p(PITFALLS 第 10 条,今天已经踩过三次);
  三、翻边之后行情怎么走 —— 这才是运营者真正想知道的:
      东证这次由空翻多,历史上它每次翻边之后 5/10/20 日价格是涨是跌。

**口径缺陷照旧**:席位=该会员名下全部客户+自营(不是它自己的钱)、榜单只有
每个合约前 20、看不到成交价所以算不出已实现部分。

跑法:python research/run_fg_dz_vs_ya.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

PAIR = ["东证期货", "永安期货"]
MULT = 20.0                       # 玻璃点值,与合约窗、跟随卡同一个数
CARRY = 20
SIMS = 5000
rng = np.random.default_rng(20260901)

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "fg_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "fg_seat.csv.gz"))
H.use("FG")
mkt = H.main_series(price)
op, st = H.contract_prices(price)
idx = mkt.index[mkt.index >= seat["trade_date"].min()]
bv = mkt["ret_open"].reindex(idx).fillna(0.0).values
n = len(idx)
main_settle = pd.Series(
    [st[m].asof(d) if isinstance(m, str) and m in st.columns else np.nan
     for d, m in zip(idx, mkt["main"].reindex(idx))], index=idx)
d_main = main_settle.diff()


def blocks_of(s):
    out, i = [], 0
    while i < n:
        if s[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < n and s[j + 1] == s[i]:
            j += 1
        out.append((i, j + 1, s[i]))
        i = j + 1
    return out


def state_vec(d):
    s = np.zeros(n)
    locs = idx.get_indexer(d.index)
    sgn = np.sign(d.values)
    for i, lo in enumerate(locs):
        if lo < 0:
            continue
        nx = locs[i + 1] if i + 1 < len(locs) else None
        end = nx if (nx is not None and nx - lo <= CARRY) else min(lo + CARRY + 1, n)
        s[lo:end] = sgn[i]
    return s


def edge_of(s, lag=2):
    p = np.concatenate([np.zeros(lag), s[:-lag]])
    live = p != 0
    if live.sum() < 1:
        return np.nan, np.nan, np.nan, 0
    k = 242 / live.sum()
    mine = (float(np.prod(1 + (p * bv)[live]) ** k) - 1) * 100
    lng = (float(np.prod(1 + bv[live]) ** k) - 1) * 100
    return mine - lng, mine, lng, int(live.sum())


def p_direction(s):
    bl = blocks_of(s)
    if len(bl) < 5:
        return np.nan, f"段数仅 {len(bl)},不足 5"
    fl = float(np.mean([1.0 if d > 0 else 0.0 for _, _, d in bl]))
    if fl in (0.0, 1.0):
        return np.nan, "各段方向全同,重掷等于没掷"
    e = edge_of(s)[0]
    sims = np.empty(SIMS)
    for k in range(SIMS):
        alt = np.zeros(n)
        for a, b, _ in bl:
            alt[a:b] = 1.0 if rng.random() < fl else -1.0
        sims[k] = edge_of(alt)[0]
    return float(np.nanmean(sims >= e)), ""


L = [f"玻璃:东证 vs 永安(样本 {idx[0].date()} ~ {idx[-1].date()},{n} 个交易日)", ""]
L.append("口径:盯市 = 昨日净持仓 ×(今结算−昨结算)× 点值 20,逐合约各算各的再相加;")
L.append("方向部分 = 当日净持仓合计 × 主力结算价变动 × 点值;价差部分 = 总账 − 方向。")
L.append("**席位 = 该会员名下全部客户 + 自营**,不是它自己的钱;看不到成交价,算不出已实现部分。")
L.append("")

info = {}
for m in PAIR:
    sub = seat[seat["member_key"] == m]
    net = sub.pivot_table(index="trade_date", columns="contract",
                          values="net_off", aggfunc="sum").reindex(idx)
    daily = pd.Series(0.0, index=idx)
    for c in net.columns:
        if not isinstance(c, str) or c not in st.columns:
            continue
        ds = st[c].reindex(idx).diff()
        ok = net[c].shift(1).notna() & ds.notna()
        daily += (net[c].shift(1) * ds * MULT).where(ok, 0.0).fillna(0.0)
    dirp = (net.sum(axis=1, min_count=1).shift(1) * d_main * MULT).fillna(0.0)
    tot = net.sum(axis=1, min_count=1)
    d0 = tot.dropna()
    sv = state_vec(np.sign(d0[d0 != 0]))
    info[m] = {"daily": daily, "dir": dirp, "sp": daily - dirp, "net": tot, "state": sv}

L.append("## 一、赚钱能力:三年盯市盈亏,拆成方向与价差")
L.append("")
L.append(f"{'席位':<10}{'总盯市':>12}{'方向部分':>12}{'价差部分':>12}{'当前净持仓':>12}")
L.append("-" * 60)
for m in PAIR:
    v = info[m]
    L.append(f"{m:<10}{v['daily'].sum()/1e4:>+12.0f}{v['dir'].sum()/1e4:>+12.0f}"
             f"{v['sp'].sum()/1e4:>+12.0f}{int(v['net'].dropna().iloc[-1]):>12,}")
L.append("(单位:万元)")
L.append("")
L.append("逐年盯市盈亏(万元):")
yrs = sorted({d.year for d in idx})
L.append(f"{'席位':<10}" + "".join(f"{y:>12}" for y in yrs))
L.append("-" * (10 + 12 * len(yrs)))
for m in PAIR:
    g = info[m]["daily"].groupby(info[m]["daily"].index.year).sum()
    L.append(f"{m:<10}" + "".join(f"{g.get(y, 0.0)/1e4:>+12.0f}" for y in yrs))
L.append("")

L.append("## 二、择时能力:照它的方向做,比同样这些天一律做多强多少")
L.append("")
L.append(f"{'席位':<10}{'择时增益':>10}{'在场年化':>10}{'同期做多':>10}{'在场天':>8}{'占比':>7}"
         f"{'翻向':>6}{'p(方向)':>10}  说明")
L.append("-" * 84)
for m in PAIR:
    s = info[m]["state"]
    e, ann, lng, days = edge_of(s)
    pv, why = p_direction(s)
    ps = "  —  " if not np.isfinite(pv) else f"{pv:.4f}"
    L.append(f"{m:<10}{e:>+10.1f}{ann:>+10.1f}{lng:>+10.1f}{days:>8}{days/n*100:>6.0f}%"
             f"{max(len(blocks_of(s))-1,0):>6}{ps:>10}  {why}")
L.append("")

L.append("## 三、它翻边之后,行情怎么走(运营者真正想知道的)")
L.append("")
L.append("**翻边 = 净持仓由多转空或由空转多那一天。** T+1 开盘起算,看 5/10/20 日。")
L.append("")
close = mkt["settle"].reindex(idx)
for m in PAIR:
    tot = info[m]["net"].dropna()
    sgn = np.sign(tot[tot != 0])
    flips = [(d, int(v)) for d, v, pv in zip(sgn.index[1:], sgn.values[1:], sgn.values[:-1])
             if v != pv]
    if not flips:
        L.append(f"### {m}:样本内没有翻边")
        continue
    L.append(f"### {m} —— 共 {len(flips)} 次翻边")
    L.append(f"{'翻边日':<12}{'转向':>6}{'当日净持仓':>12}{'5日':>9}{'10日':>9}{'20日':>9}")
    L.append("-" * 58)
    rows = []
    for d, v in flips:
        k = idx.get_loc(d)
        base = close.iloc[min(k + 1, n - 1)]
        vals = []
        for w in (5, 10, 20):
            j = min(k + 1 + w, n - 1)
            vals.append((float(close.iloc[j]) / float(base) - 1) * 100
                        if np.isfinite(base) and base else np.nan)
        rows.append((d, v, vals))
    for d, v, vals in rows[-10:]:
        L.append(f"{str(d.date()):<12}{'转多' if v > 0 else '转空':>6}"
                 f"{int(tot.loc[d]):>12,}" + "".join(f"{x:>+9.1f}" for x in vals))
    L.append("-" * 58)
    for lab, sel in (("全部", rows), ("转多", [r for r in rows if r[1] > 0]),
                     ("转空", [r for r in rows if r[1] < 0])):
        if not sel:
            continue
        arr = np.array([r[2] for r in sel], dtype=float)
        # 转空时「对」意味着跌,所以按方向记分
        sc = arr * (1 if lab == "转多" else (-1 if lab == "转空" else 1))
        hit = [(np.nansum(sc[:, i] > 0), np.sum(np.isfinite(sc[:, i]))) for i in range(3)]
        L.append(f"  {lab}({len(sel)} 次)均值 " +
                 "  ".join(f"{w}日 {np.nanmean(arr[:, i]):+.1f}%" for i, w in enumerate((5, 10, 20))) +
                 "  | 方向对的比例 " +
                 "  ".join(f"{a}/{b}" for a, b in hit))
    L.append("")

L.append("**怎么读**:「方向对的比例」按它的转向记分 —— 转多之后涨算对、转空之后跌算对。")
L.append("样本少的时候均值很容易被一两次极端值带偏,所以比例和均值要一起看。")
io.open(OUT / "fg_dz_vs_ya.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
