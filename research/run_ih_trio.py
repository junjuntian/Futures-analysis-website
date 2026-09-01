# -*- coding: utf-8 -*-
"""IH 三方共振:摩根大通 / 高盛 / 中财(2026-09-01 运营者点名)。

运营者:「核心比较准的席位,应该是摩根大通、高盛、中财,核心看这几个席位,
时间拉长一点儿,看看能否三方共振」。

**为什么这个问法比单家更有希望**:单家太稀 —— 摩根十二年只出手 13 轮,
p(方向) 永远压不到 0.05(`REPORT_IH_JUDGE12_v1`)。而「几家同时在场且同向」
是个**更强的条件**,如果它们各自都带一点信息,叠起来可能出现单家看不到的东西。
代价是共振日更少,功效更差 —— 所以「时间拉长」是必须的:沿用窗从 20 日拉到
40/60/90 日,它们的在场期才有机会重叠。

**方法与 run_ih_judge12.py 一字不差**,不另起一套:
  在场即持仓,方向=净持仓符号,T+1 执行(lag=2);
  择时增益 = 照它方向做 − 同样这些天一律做多;
  p(方向) = 在场日子不动,只把每段方向随机重掷。

三种共振口径(预注册,不是跑完再挑):
  A. **全体同向**:三家都在场且方向一致;
  B. **多数同向**:≥2 家在场且在场的全部同向(允许第三家不在场);
  C. **票数表决**:三家状态求和取符号(允许有人反对,少数服从多数)。

跑法:python research/run_ih_trio.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

TRIO = ["摩根大通", "高盛期货", "中财期货"]
CARRIES = (20, 40, 60, 90)
SIMS = 5000
MIN_DAYS = 60                     # 共振天数下限:低于此不给判定,只报数
rng = np.random.default_rng(20260901)

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "ih_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "ih_seat.csv.gz"))
H.use("IH")
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
idx = mkt.index
bv = mkt["ret_open"].fillna(0.0).values
n = len(idx)

DAILY = {}
for m in TRIO:
    g = seat[seat["member_key"] == m]
    d = g.groupby("trade_date")["net_off"].sum()
    DAILY[m] = d[d.index.isin(idx) & (d != 0)]


def state_vec(d, carry):
    st = np.zeros(n)
    if not len(d):
        return st
    locs = idx.get_indexer(d.index)
    sgn = np.sign(d.values)
    for i, lo in enumerate(locs):
        end = locs[i + 1] if i + 1 < len(locs) and locs[i + 1] - lo <= carry else min(lo + carry + 1, n)
        st[lo:end] = sgn[i]
    return st


def blocks_of(st):
    out, i = [], 0
    while i < n:
        if st[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < n and st[j + 1] == st[i]:
            j += 1
        out.append((i, j + 1, st[i]))
        i = j + 1
    return out


def timing_edge(st, lag=2):
    """(择时增益, 在场年化, 同期一律做多的年化, 在场天数)。"""
    p = np.concatenate([np.zeros(lag), st[:-lag]])
    live = p != 0
    if live.sum() < 1:
        return np.nan, np.nan, np.nan, 0
    k = 242 / live.sum()
    mine = (float(np.prod(1 + (p * bv)[live]) ** k) - 1) * 100
    longs = (float(np.prod(1 + bv[live]) ** k) - 1) * 100
    return mine - longs, mine, longs, int(live.sum())


def direction_p(st, edge, lag=2):
    bl = blocks_of(st)
    if len(bl) < 2 or not np.isfinite(edge):
        return np.nan
    frac_long = float(np.mean([1.0 if d > 0 else 0.0 for _, _, d in bl]))
    sims = np.empty(SIMS)
    for k in range(SIMS):
        alt = np.zeros(n)
        for a, b, _ in bl:
            alt[a:b] = 1.0 if rng.random() < frac_long else -1.0
        sims[k] = timing_edge(alt, lag)[0]
    return float(np.nanmean(sims >= edge))


def net_cost(st, lag=2):
    p = np.concatenate([np.zeros(lag), st[:-lag]])
    live = p != 0
    if live.sum() < 1:
        return np.nan
    turn = np.abs(np.diff(np.concatenate([[0], p]))) > 0
    r = (p * bv - turn * 0.001)[live]
    return (float(np.prod(1 + r) ** (242 / live.sum())) - 1) * 100


def yearly(st, lag=2):
    p = np.concatenate([np.zeros(lag), st[:-lag]])
    s = pd.Series(p * bv, index=idx)[p != 0]
    return {y: (float(np.prod(1 + g)) - 1) * 100 for y, g in s.groupby(s.index.year)}


bench = (float(np.prod(1 + bv) ** (242 / n)) - 1) * 100
L = [f"IH 三方共振:{' / '.join(TRIO)}(样本 {idx[0].date()} ~ {idx[-1].date()},{n} 个交易日)", ""]
L.append(f"同期恒多年化 {bench:+.1f}%。方法与 REPORT_IH_JUDGE12_v1 一字不差,只把")
L.append("「单家在场」换成「几家同时在场且同向」,并把掉榜沿用窗从 20 日拉到 40/60/90 日。")
L.append("")

# ---------------------------------------------------------- 一、三家各自的底子
L.append("## 一、三家各自(先看清各自的料,再谈共振)")
L.append("")
L.append(f"{'席位':<10}{'沿用':>5}{'在场天':>7}{'在场占比':>9}{'择时增益':>9}{'在场年化':>9}{'翻向':>5}{'末次上榜':>12}")
L.append("-" * 68)
for m in TRIO:
    d = DAILY[m]
    last = str(d.index.max().date()) if len(d) else "—"
    for c in CARRIES:
        st = state_vec(d, c)
        edge, ann, _, days = timing_edge(st)
        flips = max(len(blocks_of(st)) - 1, 0)
        L.append(f"{m if c == CARRIES[0] else '':<10}{c:>5}{days:>7}{days/n*100:>8.0f}%"
                 f"{edge:>+9.1f}{ann:>+9.1f}{flips:>5}{last if c == CARRIES[0] else '':>12}")
L.append("")

# ---------------------------------------------------------- 二、重叠有多少
L.append("## 二、它们到底有多少天同时在场(共振的原料够不够)")
L.append("")
L.append(f"{'沿用':>5}{'≥1家在场':>10}{'≥2家在场':>10}{'3家全在':>9}{'≥2且同向':>10}{'3家全同向':>11}")
L.append("-" * 56)
overlap = {}
for c in CARRIES:
    S = np.array([state_vec(DAILY[m], c) for m in TRIO])
    on = (S != 0).sum(axis=0)
    ssum = S.sum(axis=0)
    agree2 = (on >= 2) & (np.abs(ssum) == on)      # 在场的那些家方向全一致
    agree3 = (on == 3) & (np.abs(ssum) == 3)
    overlap[c] = (S, on, ssum, agree2, agree3)
    L.append(f"{c:>5}{int((on>=1).sum()):>10}{int((on>=2).sum()):>10}{int((on==3).sum()):>9}"
             f"{int(agree2.sum()):>10}{int(agree3.sum()):>11}")
L.append("")

# ---------------------------------------------------------- 三、三种共振口径
L.append("## 三、三种共振口径(预注册,不是跑完再挑)")
L.append("")
L.append(f"{'口径':<12}{'沿用':>5}{'在场天':>7}{'择时增益':>9}{'在场年化':>9}{'p(方向)':>9}"
         f"{'翻向':>5}{'扣成本':>8}{'正年':>7}")
L.append("-" * 74)
best = []
for c in CARRIES:
    S, on, ssum, agree2, agree3 = overlap[c]
    cands = {
        "A 全体同向": np.where(agree3, np.sign(ssum), 0.0),
        "B 多数同向": np.where(agree2, np.sign(ssum), 0.0),
        "C 票数表决": np.where(on >= 2, np.sign(ssum), 0.0),
    }
    for name, st in cands.items():
        edge, ann, _, days = timing_edge(st)
        if days < 5:
            L.append(f"{name:<12}{c:>5}{days:>7}{'样本太薄':>27}")
            continue
        pdir = direction_p(st, edge) if days >= MIN_DAYS else np.nan
        ys = yearly(st)
        py = sum(1 for v in ys.values() if v > 0)
        ps = "  —  " if not np.isfinite(pdir) else f"{pdir:.3f}"
        L.append(f"{name:<12}{c:>5}{days:>7}{edge:>+9.1f}{ann:>+9.1f}{ps:>9}"
                 f"{max(len(blocks_of(st))-1,0):>5}{net_cost(st):>+8.1f}{py:>4}/{len(ys):<2}")
        if days >= MIN_DAYS and np.isfinite(pdir):
            best.append((pdir, name, c, edge, ann, days, py, len(ys), net_cost(st)))
    L.append("")

# ------------------------------------------- 四、共振段逐段明细 + 有效的安慰剂
# 上一节 p(方向)=1.000 是**退化**的:三段方向全一样,随机重掷方向等于没掷
# (PITFALLS 第 10 条,我第二次踩)。方向上没得测,那就测**时段**:
# 共振只占全样本 5%,「随机抽同样长的时段」这个检验在这里是有效的。
L.append("## 四、共振段逐段明细(全部样本就这几段)与有效的安慰剂")
L.append("")
for c in CARRIES:
    S, on, ssum, agree2, agree3 = overlap[c]
    st = np.where(agree2, np.sign(ssum), 0.0)
    bl = blocks_of(st)
    if not bl:
        continue
    L.append(f"### 沿用 {c} 日")
    L.append(f"{'#':>3}{'起':>12}{'止':>12}{'方向':>6}{'天数':>6}{'本段涨跌':>10}{'照它做':>9}  在场的是谁")
    L.append("-" * 76)
    for k, (a, b, sgn) in enumerate(bl, 1):
        ea, eb = min(a + 2, n - 1), min(b + 2, n)
        seg = bv[ea:eb]
        if not len(seg):
            continue
        who = set()
        for j, m in enumerate(TRIO):
            if (S[j][a:b] != 0).any():
                who.add(m)
        mv = (float(np.prod(1 + seg)) - 1) * 100
        mine = (float(np.prod(1 + sgn * seg)) - 1) * 100
        L.append(f"{k:>3}{str(idx[a].date()):>12}{str(idx[min(b-1,n-1)].date()):>12}"
                 f"{'净多' if sgn > 0 else '净空':>6}{b-a:>6}{mv:>+10.2f}{mine:>+9.2f}  {'+'.join(sorted(who))}")
    # 有效的安慰剂:同在场天数、同方向比例,随机时段(在场只占 5%,不退化)
    edge, ann, _, days = timing_edge(st)
    frac_long = float((st[st != 0] > 0).mean())
    starts = rng.integers(0, max(n - days - 3, 1), SIMS)
    dirs = np.where(rng.random(SIMS) < frac_long, 1.0, -1.0)
    sims = np.empty(SIMS)
    for k in range(SIMS):
        seg = bv[starts[k] + 2: starts[k] + 2 + days]
        sims[k] = ((float(np.prod(1 + dirs[k] * seg) ** (242 / max(len(seg), 1))) - 1) * 100
                   if len(seg) else 0.0)
    p_win = float((sims >= ann).mean())
    L.append(f"  段数 {len(bl)} · 方向 {'全净空' if frac_long == 0 else ('全净多' if frac_long == 1 else '有多有空')}"
             f" · **p(时段)={p_win:.4f}**(同长度随机时段 {SIMS} 次)")
    L.append("")

best.sort()
L.append("## 判定")
L.append("")
if best:
    pdir, name, c, edge, ann, days, py, ny, nc = best[0]
    L.append(f"最好的一档:**{name} · 沿用 {c} 日** —— 择时增益 {edge:+.1f}%/年、"
             f"在场年化 {ann:+.1f}%、p(方向)={pdir:.3f}、在场 {days} 天、正年 {py}/{ny}、"
             f"扣成本 {nc:+.1f}%。")
    L.append("")
    nn = len([b for b in best])
    L.append(f"**多重检验**:一共报了 {nn} 个通过样本量门槛的组合(3 口径 × {len(CARRIES)} 档沿用),"
             f"Bonferroni 阈值 = 0.05/{nn} = {0.05/nn:.4f}。")
    L.append(f"最好那一档 {'**过**' if pdir < 0.05/nn else '**不过**'}校正后的门槛。")
else:
    L.append("没有任何一个组合达到样本量门槛 —— 三家的在场期重叠太少,共振这条路上无米下锅。")
L.append("")
L.append("**丑话**:①共振天数天然比单家少,功效更差,看不出显著不等于没有;")
L.append("②沿用窗拉长是把「掉榜」当「仍在场」,拉得越长这个假设越可疑 —— 三禾实测")
L.append("沿用 20 日的一致率只有 55%~88%(REPORT_IH_PLAN_V3),拉到 90 日只会更糟;")
L.append("③三家是运营者点名的,不是数据挑的 —— 这一点反而比事后择优干净。")
io.open(OUT / "ih_trio.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
