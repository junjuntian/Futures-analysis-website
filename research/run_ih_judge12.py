# -*- coding: utf-8 -*-
"""IH:把 PLAN v3 造的**新判据**倒回去测十二年官方序列(2026-08-31)。

**这是 PLAN v3 漏掉的那个实验。** 运营者问「之前摩根大通不是通过了吗」,
问得对 —— PLAN v3 只把新判据用在三禾的三年窗口上,而摩根在那三年真实在场
只有 57 天,判不了。**"判不了"不等于"判否了"**,十二年官方序列必须用同一把
新尺子重量一遍。

新判据是什么(与 run_ih_plan_v3 一字不差):
  **择时增益** = 照它的方向做,比「同样这些天一律做多」多赚的年化百分点。
  `p(方向)` = 在场日子一天不动,只把每段方向随机重掷得到的安慰剂。
旧判据(REPORT_IH_JPM_v1 / STATE_SCAN_v1)测的是「在场年化 vs 随机时段」,
那把尺子答不了「它赚的是方向的钱还是行情的钱」—— 中财就是这么露馅的
(在场年化 +22.1%,择时增益 +0.0%)。

数据仍是中金所官方前 20 + 掉榜沿用 20 日。**沿用假设的误差(PLAN v3 实测
一致率 55%~88%)照样在里面**,这一轮换不掉它 —— 三禾只有三年。所以本轮的
定位是:**在与前三轮完全相同的数据上,只换判据,看结论变不变。**

跑法:python research/run_ih_judge12.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

NAMED = ["摩根大通", "高盛期货", "安粮期货", "中财期货"]
MIN_DAYS = 100
SIMS = 5000
CARRY = 20
rng = np.random.default_rng(20260831)

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


def timing_edge(st, lag=2):
    p = np.concatenate([np.zeros(lag), st[:-lag]])
    live = p != 0
    if live.sum() < 1:
        return np.nan, np.nan, 0
    k = 242 / live.sum()
    mine = (float(np.prod(1 + (p * bv)[live]) ** k) - 1) * 100
    longs = (float(np.prod(1 + bv[live]) ** k) - 1) * 100
    return mine - longs, mine, int(live.sum())


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


rows = []
for m, d in DAILY.items():
    st = state_vec(d)
    edge, ann, days = timing_edge(st)
    if days < MIN_DAYS or not np.isfinite(edge):
        continue
    flips = max(len(blocks_of(st)) - 1, 0)
    p_dir = direction_p(st, edge)
    # 稳健性:沿用档位 / 执行延迟 / 前后半,判的都是**增益**不是在场年化
    knobs = [timing_edge(state_vec(d, c))[0] for c in (5, 10, 20, 30, 40)]
    knob_pos = sum(1 for v in knobs if np.isfinite(v) and v > 0)
    lags = [timing_edge(st, lag=l)[0] for l in (2, 3, 4, 6)]
    lag_pos = sum(1 for v in lags if np.isfinite(v) and v > 0)
    h = n // 2
    e1 = timing_edge(np.concatenate([st[:h], np.zeros(n - h)]))[0]
    e2 = timing_edge(np.concatenate([np.zeros(h), st[h:]]))[0]
    rows.append({"m": m, "edge": edge, "ann": ann, "days": days, "flips": flips,
                 "p_dir": p_dir, "knob": knob_pos, "lag": lag_pos,
                 "half": (np.isfinite(e1) and e1 > 0) and (np.isfinite(e2) and e2 > 0),
                 "e1": e1, "e2": e2,
                 "long": float((st[st != 0] > 0).mean()), "med": float(d.abs().median())})
rows.sort(key=lambda r: (np.inf if not np.isfinite(r["p_dir"]) else r["p_dir"]))
N = len(rows)
bench = (float(np.prod(1 + bv) ** (242 / n)) - 1) * 100

L = [f"IH:新判据(择时增益)倒测十二年官方序列 —— {idx[0].date()} ~ {idx[-1].date()},{n} 个交易日", ""]
L.append(f"在场 ≥{MIN_DAYS} 天的 {N} 家;同期恒多年化 {bench:+.1f}%。")
L.append(f"Bonferroni 阈值 = 0.05/{N} = {0.05 / max(N,1):.5f}。")
L.append("")
L.append("**判据换了**:旧的问「在场年化比随机时段高吗」,新的问「照它的方向做,")
L.append("比同样这些天一律做多多赚吗」。前者会把「常年单边 + 行情配合」记成本事。")
L.append("")

# ---------------------------------------------------------- 一、点名四家
L.append("## 一、点名四家,新旧判据对照")
L.append("")
L.append(f"{'席位':<10}{'在场年化':>9}{'择时增益':>9}{'p(方向)':>9}{'翻向':>5}{'在场天':>7}{'旋钮':>5}{'延迟':>5}{'前半增益':>9}{'后半增益':>9}")
L.append("-" * 84)
by_name = {r["m"]: r for r in rows}
for m in NAMED:
    r = by_name.get(m)
    if r is None:
        d = DAILY.get(m)
        note = f"在场不足 {MIN_DAYS} 天" if d is not None else "十二年内无数据"
        L.append(f"{m:<10}{note}")
        continue
    ps = "  —  " if not np.isfinite(r["p_dir"]) else f"{r['p_dir']:.3f}"
    f2 = lambda v: "    —" if not np.isfinite(v) else f"{v:+9.1f}"  # noqa: E731
    L.append(f"{m:<10}{r['ann']:>+9.1f}{r['edge']:>+9.1f}{ps:>9}{r['flips']:>5}{r['days']:>7}"
             f"{r['knob']:>4}/5{r['lag']:>4}/4{f2(r['e1'])}{f2(r['e2'])}")
L.append("")

# ---------------------------------------------------------- 二、全席位排行
L.append("## 二、全席位按 p(方向) 排")
L.append("")
L.append(f"{'席位':<10}{'择时增益':>9}{'p(方向)':>9}{'翻向':>5}{'在场年化':>9}{'在场天':>7}{'旋钮':>5}{'延迟':>5}{'分半':>5}  {'方向':<5}{'中位手数':>9}")
L.append("-" * 88)
for r in rows[:22]:
    ps = "  —  " if not np.isfinite(r["p_dir"]) else f"{r['p_dir']:.3f}"
    star = " ★" if r["m"] in NAMED else ""
    L.append(f"{r['m']:<10}{r['edge']:>+9.1f}{ps:>9}{r['flips']:>5}{r['ann']:>+9.1f}{r['days']:>7}"
             f"{r['knob']:>4}/5{r['lag']:>4}/4{'  ✓' if r['half'] else '  ✗':>5}  "
             f"{'净多' if r['long'] > 0.5 else '净空':<5}{r['med']:>9,.0f}{star}")
strong = [r for r in rows
          if np.isfinite(r["edge"]) and r["edge"] > 0
          and np.isfinite(r["p_dir"]) and r["p_dir"] < 0.05
          and r["flips"] >= 4 and r["knob"] == 5 and r["lag"] == 4 and r["half"]]
L.append("")
L.append(f"**过闸(增益>0 且 p(方向)<0.05 且 翻向≥4 且 旋钮 5/5 且 延迟 4/4 且 分半均正)= {len(strong)} 家**")
L.append(f"(纯随机下期望 ≈ {N * 0.05 * 0.125:.2f} 家)")
for r in strong:
    bonf = "**过 Bonferroni**" if r["p_dir"] < 0.05 / max(N, 1) else "不过 Bonferroni"
    L.append(f"   ★ {r['m']}: 增益 {r['edge']:+.1f}%/年  p(方向)={r['p_dir']:.4f} {bonf}"
             f"  翻向 {r['flips']}  在场 {r['days']} 天  中位 {r['med']:,.0f} 手")
if not strong:
    L.append("   (无)")


# ------------------------------------------------ 三、摩根大通逐轮明细
L.append("")
L.append("## 三、摩根大通逐轮明细(十二年里全部进出场)")
L.append("")
L.append("**它的死穴是次数,不是胜率** —— 下表就是全部样本,一共这么多行。")
L.append("")
d = DAILY["摩根大通"]
st = state_vec(d)
L.append(f"{'#':>3}{'进场':>12}{'出场':>12}{'方向':>6}{'持有日':>7}{'本轮涨跌':>10}{'照它做':>9}{'结果':>6}")
L.append("-" * 68)
wins = 0
rounds = []
for k, (a, b, sgn) in enumerate(blocks_of(st), 1):
    ea, eb = min(a + 2, n - 1), min(b + 2, n)
    seg = bv[ea:eb]
    if len(seg) == 0:
        continue
    mkt_move = (float(np.prod(1 + seg)) - 1) * 100
    mine = (float(np.prod(1 + sgn * seg)) - 1) * 100
    wins += mine > 0
    rounds.append(mine)
    L.append(f"{k:>3}{str(idx[a].date()):>12}{str(idx[min(b-1, n-1)].date()):>12}"
             f"{'净多' if sgn > 0 else '净空':>6}{b - a:>7}{mkt_move:>+10.2f}{mine:>+9.2f}"
             f"{'  赢' if mine > 0 else '  输':>6}")
L.append("-" * 68)
L.append(f"{'合计':>3}{'':<24}{len(rounds)} 轮  胜 {wins} 负 {len(rounds)-wins}"
         f"  胜率 {wins/max(len(rounds),1)*100:.0f}%  均 {np.mean(rounds):+.2f}%/轮")
L.append("")
last_day = d.index.max()
last_net = float(d.loc[last_day])
in_now = st[-1] != 0
L.append(f"**当前状态**:末次上榜 {last_day.date()},净{'多' if last_net > 0 else '空'} "
         f"{abs(last_net):,.0f} 手;沿用 20 日口径下 {'**仍在场**' if in_now else '已出场'}。")
L.append("")
L.append("**统计上限说清楚**:12 轮就是十二年的全部样本(约一年一次)。方向重掷的")
L.append("安慰剂在 12 个段上分辨力极弱 —— 要把 p 压到 0.05 以下大约需要**翻一倍的轮次**,")
L.append("那意味着再等十二年。**它永远不会在这个检验上变显著,不是因为它不行,")
L.append("是因为它出手太少。** 用不用它,是「愿不愿在 p=0.136 上下注」的问题,")
L.append("不是「有没有信号」的问题。")

io.open(OUT / "ih_judge12.txt", "w", encoding="utf-8").write("\n".join(L))
print(f"done: {N} seats, {len(strong)} strong")
