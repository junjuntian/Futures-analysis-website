# -*- coding: utf-8 -*-
"""IH:找与「摩根大通 + 高盛」同频、且自己也赚钱的第三家(2026-09-01 运营者)。

运营者:「看看还有没有和摩根+高盛同频的席位,再选出一个赚钱的席位就行了」。

**为什么需要第三家**(`REPORT_IH_TRIO_v1`):摩根与高盛在十二年里只共振过 3 段,
全挤在 2021-07~2022-04,而且**高盛 2023-09 就离场了** —— 就算那个信号是真的,
现在也不可能再触发。要让这条路活着,得找一个**还在场**的第三家顶上。

三个筛子(预注册):
  1. **同频** —— 与摩根、与高盛各自的重叠期里方向一致率要高;
  2. **自己赚钱** —— 它自己的择时增益 > 0(照它做要比同期一律做多强);
  3. **还活着** —— 末次上榜够近,否则和高盛一样是历史现象。

**丑话先写**:这是在 100 多家里挑,与 IH 那两轮同一个选择偏差陷阱
(`research/PITFALLS.md` 第 5 条)。所以同时报 Bonferroni 阈值、前后半、
以及「同向率」的零假设基准 —— 两条随机的 ±1 序列同向率期望就是 50%,
高出多少才算同频,得跟这个比。

跑法:python research/run_ih_sync.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

ANCHOR = ["摩根大通", "高盛期货"]
CARRY = 20                        # 与前两轮同一档,不调参
SIMS = 5000
MIN_OVERLAP = 40                  # 与锚点的重叠天下限,少于此同向率不稳
MIN_OWN_DAYS = 100                # 自身在场天下限,与 STATE_SCAN 同
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
for m, g in seat.groupby("member_key"):
    d = g.groupby("trade_date")["net_off"].sum()
    d = d[d.index.isin(idx) & (d != 0)]
    if len(d) >= 5:
        DAILY[m] = d


def state_vec(d, carry=CARRY):
    st = np.zeros(n)
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


def perf(st, lag=2):
    """(择时增益, 在场年化, 在场天数)。"""
    p = np.concatenate([np.zeros(lag), st[:-lag]])
    live = p != 0
    if live.sum() < 1:
        return np.nan, np.nan, 0
    k = 242 / live.sum()
    mine = (float(np.prod(1 + (p * bv)[live]) ** k) - 1) * 100
    longs = (float(np.prod(1 + bv[live]) ** k) - 1) * 100
    return mine - longs, mine, int(live.sum())


def window_p(st, ann, days, lag=2):
    """同在场天数、同多空比例、随机时段。在场占比高时会退化,调用处把关。"""
    if days < 20 or not np.isfinite(ann):
        return np.nan
    frac_long = float((st[st != 0] > 0).mean())
    starts = rng.integers(0, max(n - days - 3, 1), SIMS)
    dirs = np.where(rng.random(SIMS) < frac_long, 1.0, -1.0)
    sims = np.empty(SIMS)
    for k in range(SIMS):
        seg = bv[starts[k] + lag: starts[k] + lag + days]
        sims[k] = ((float(np.prod(1 + dirs[k] * seg) ** (242 / max(len(seg), 1))) - 1) * 100
                   if len(seg) else 0.0)
    return float((sims >= ann).mean())


S = {m: state_vec(d) for m, d in DAILY.items()}
A = {m: S[m] for m in ANCHOR if m in S}
LAST = {m: d.index.max() for m, d in DAILY.items()}

L = [f"IH:找与「{' + '.join(ANCHOR)}」同频、且自己赚钱的第三家",
     f"(样本 {idx[0].date()} ~ {idx[-1].date()},{n} 个交易日;掉榜沿用 {CARRY} 日)", ""]
L.append("**零基准不是 50%**(我第一版就是这么写的,错了)。IH 前 20 席位绝大多数")
L.append("常年**净空** —— 两家都常年做空,同向率天然就高。正确的基准是**按各自多空")
L.append("比例随机配对的期望同向率** p·q+(1−p)(1−q)。下表报的是**超额同向率**")
L.append("(实际 − 期望):它才回答「除了都在做空之外,它们还有没有真的同步」。")
L.append("")

rows = []
for m, st in S.items():
    if m in ANCHOR:
        continue
    edge, ann, days = perf(st)
    if days < MIN_OWN_DAYS or not np.isfinite(edge):
        continue
    rec = {"m": m, "edge": edge, "ann": ann, "days": days,
           "last": LAST[m], "flips": max(len(blocks_of(st)) - 1, 0)}
    ok = True
    for a in ANCHOR:
        both = (st != 0) & (A[a] != 0)
        k = int(both.sum())
        rec[f"ov_{a}"] = k
        if k:
            ag = float((st[both] == A[a][both]).mean() * 100)
            # 期望同向率:两边各按自己在重叠期内的多头占比独立配对
            pl = float((st[both] > 0).mean())
            ql = float((A[a][both] > 0).mean())
            exp = (pl * ql + (1 - pl) * (1 - ql)) * 100
            rec[f"ag_{a}"], rec[f"ex_{a}"] = ag, ag - exp
        else:
            rec[f"ag_{a}"] = rec[f"ex_{a}"] = np.nan
        if k < MIN_OVERLAP:
            ok = False
    rec["enough"] = ok
    rows.append(rec)

# 同频度 = 与两个锚点的同向率取较小者(两个都得像才算同频,不能靠一个撑)
for r in rows:
    vals = [r[f"ex_{a}"] for a in ANCHOR if np.isfinite(r.get(f"ex_{a}", np.nan))]
    # 两个锚点都要像才算同频,取较小者 —— 不能靠一个撑起来
    r["sync"] = min(vals) if len(vals) == len(ANCHOR) else np.nan

cand = [r for r in rows if r["enough"] and np.isfinite(r["sync"])]
cand.sort(key=lambda r: -r["sync"])

L.append(f"## 一、同频排行(与两个锚点各自重叠 ≥{MIN_OVERLAP} 天、自身在场 ≥{MIN_OWN_DAYS} 天的 {len(cand)} 家)")
L.append("")
L.append(f"{'席位':<10}{'摩根重叠':>9}{'同向':>7}{'超额':>7}{'高盛重叠':>9}{'同向':>7}{'超额':>7}"
         f"{'同频度':>8}{'自身增益':>9}{'在场天':>7}{'末次上榜':>12}")
L.append("-" * 96)
for r in cand[:20]:
    L.append(f"{r['m']:<10}{r['ov_摩根大通']:>9}{r['ag_摩根大通']:>6.0f}%{r['ex_摩根大通']:>+7.0f}"
             f"{r['ov_高盛期货']:>9}{r['ag_高盛期货']:>6.0f}%{r['ex_高盛期货']:>+7.0f}"
             f"{r['sync']:>+8.0f}{r['edge']:>+9.1f}{r['days']:>7}{str(r['last'].date()):>12}")
L.append("")

# ------------------------------------------------ 二、把它加进来,共振测一遍
ALIVE = pd.Timestamp("2025-01-01")      # 末次上榜晚于此才算「还活着」
picks = [r for r in cand if r["sync"] >= 10 and r["edge"] > 0][:8]
L.append("## 二、把候选加进来做三方共振(≥2 家在场且同向)")
L.append("")
L.append(f"筛出**超额同向率 ≥10 个百分点**(即除了都在做空之外确有同步)"
         f"且自身择时增益为正的 {len(picks)} 家。")
L.append("")
L.append(f"{'第三家':<10}{'共振天':>7}{'段数':>5}{'在场年化':>9}{'择时增益':>9}{'p(时段)':>9}"
         f"{'正年':>7}{'还活着':>8}")
L.append("-" * 66)
scored = []
for r in picks:
    m = r["m"]
    M = np.array([A[ANCHOR[0]], A[ANCHOR[1]], S[m]])
    on = (M != 0).sum(axis=0)
    ssum = M.sum(axis=0)
    agree = (on >= 2) & (np.abs(ssum) == on)
    st = np.where(agree, np.sign(ssum), 0.0)
    edge, ann, days = perf(st)
    if days < 20:
        L.append(f"{m:<10}{days:>7}{'共振太少,不判':>30}")
        continue
    p_w = window_p(st, ann, days)
    p2 = np.concatenate([np.zeros(2), st[:-2]])
    s = pd.Series(p2 * bv, index=idx)[p2 != 0]
    ys = {y: (float(np.prod(1 + g)) - 1) * 100 for y, g in s.groupby(s.index.year)}
    py = sum(1 for v in ys.values() if v > 0)
    alive = "是" if r["last"] >= ALIVE else "否"
    L.append(f"{m:<10}{days:>7}{len(blocks_of(st)):>5}{ann:>+9.1f}{edge:>+9.1f}"
             f"{p_w:>9.4f}{py:>4}/{len(ys):<2}{alive:>8}")
    scored.append((p_w, m, ann, edge, days, py, len(ys), alive, ys))
L.append("")

scored.sort()
N = max(len(scored), 1)
L.append("## 判定")
L.append("")
L.append(f"**多重检验**:候选是从 {len(cand)} 家里筛出来的,这里报了 {len(scored)} 组共振;")
L.append(f"按报出来的组数算 Bonferroni 阈值 = 0.05/{N} = {0.05/N:.4f}"
         f"(按初筛的 {len(cand)} 家算则是 {0.05/max(len(cand),1):.5f},更严)。")
L.append("")
if scored:
    p_w, m, ann, edge, days, py, ny, alive, ys = scored[0]
    L.append(f"最好的一档:**{m}** —— 共振 {days} 天、在场年化 {ann:+.1f}%、"
             f"择时增益 {edge:+.1f}%、p(时段)={p_w:.4f}、正年 {py}/{ny}、还活着:{alive}。")
    L.append("  逐年:" + "  ".join(f"{y}:{v:+.0f}%" for y, v in sorted(ys.items())))
    L.append("")
    if p_w < 0.05 / N:
        L.append("  **过了按报出组数算的 Bonferroni。** 但它仍是从上百家里挑出来的,")
        L.append("  真正的检验是「事前选不选得出」—— 那要另做 walk-forward。")
    else:
        L.append("  **不过 Bonferroni。** 在这个样本上它不构成可上线的信号。")
else:
    L.append("没有候选能形成够样本量的共振 —— 这条路到此为止。")
L.append("")
L.append("**共同的硬伤没有变**:高盛 2023-09 已离场,任何含它的组合**现在都不可能触发**;")
L.append("要活的信号,得看「摩根 + 还活着的那家」两方版,而两方版的样本比三方还薄。")
io.open(OUT / "ih_sync.txt", "w", encoding="utf-8").write("\n".join(L))
print(f"done: {len(cand)} candidates, {len(scored)} scored")
