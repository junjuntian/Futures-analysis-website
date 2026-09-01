# -*- coding: utf-8 -*-
"""IH:严测「国泰君安方向 + 摩根/高盛附议」(2026-09-01 运营者:「可以,测试吧」)。

**这条线的由来,以及它不是什么。** `REPORT_IH_SYNC_v1` 证否了「同频」那个假设:
IH 前 20 席位常年净空,原始同向率 90%+ 全是平凡的,**超额同向率最高才 +9 个百分点**。
所以本文测的**不是**「三家共振」,而是另一个假设:

    国泰君安自己的在场方向带信息;摩根或高盛同时在场且同向时,信号更干净。

它凭什么值得测:国泰君安 **2,764/2,766 天都在榜**(几乎不掉榜,信号不断)、
翻向次数全场最多档(它真在做方向判断,不是躺着套保),自身择时增益 +7.1%。

**零假设先写死,不跑完再挑**(今天已经在这上面摔过三次,PITFALLS 第 10 条):
  · `p(方向)`:在场日子一天不动,只把**每一段的方向**按观测到的多空比例随机重掷。
    问的是「它挑的方向有没有信息」。**段数 ≥5 且多空都有**才报,否则退化。
  · `p(时段)`:同在场天数、同多空比例,随机换时段。问的是「它在场的那几段日子
    挑得好不好」。**在场占比 ≤70%** 才报 —— 国泰君安单独是 100% 在场,
    这个检验对它必然退化,**故意不报**。

四个信号并排(预注册,全部要报,不挑):
  S0 国泰君安单独          —— 基线:过滤器到底加没加东西,全看它
  S1 + 摩根附议
  S2 + 高盛附议
  S3 + 摩根或高盛附议      —— 809 天那一版

跑法:python research/run_ih_gtja.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

MAIN = "国泰君安"
FILTERS = {"摩根": "摩根大通", "高盛": "高盛期货"}
CARRY = 20
SIMS = 5000
MAX_ON_FOR_WINDOW = 0.70          # 在场占比高于此,随机时段检验退化,不报
MIN_BLOCKS_FOR_DIR = 5            # 段数少于此,方向重掷检验退化,不报
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


def perf(st, lag=2, cost=0.0):
    """(择时增益, 在场年化, 同期一律做多年化, 在场天数)。"""
    p = np.concatenate([np.zeros(lag), st[:-lag]])
    live = p != 0
    if live.sum() < 1:
        return np.nan, np.nan, np.nan, 0
    turn = np.abs(np.diff(np.concatenate([[0], p]))) > 0
    k = 242 / live.sum()
    mine = (float(np.prod(1 + (p * bv - turn * cost)[live]) ** k) - 1) * 100
    longs = (float(np.prod(1 + bv[live]) ** k) - 1) * 100
    return mine - longs, mine, longs, int(live.sum())


def p_direction(st, lag=2):
    """在场不动,只重掷每段方向。退化时返回 (nan, 原因)。"""
    bl = blocks_of(st)
    if len(bl) < MIN_BLOCKS_FOR_DIR:
        return np.nan, f"段数仅 {len(bl)},不足 {MIN_BLOCKS_FOR_DIR}"
    fl = float(np.mean([1.0 if d > 0 else 0.0 for _, _, d in bl]))
    if fl in (0.0, 1.0):
        return np.nan, "各段方向全同,重掷等于没掷"
    edge = perf(st, lag)[0]
    sims = np.empty(SIMS)
    for k in range(SIMS):
        alt = np.zeros(n)
        for a, b, _ in bl:
            alt[a:b] = 1.0 if rng.random() < fl else -1.0
        sims[k] = perf(alt, lag)[0]
    return float(np.nanmean(sims >= edge)), ""


def p_window(st, lag=2):
    """同长度随机时段。在场占比过高时退化,返回 (nan, 原因)。"""
    _, ann, _, days = perf(st, lag)
    if days / n > MAX_ON_FOR_WINDOW:
        return np.nan, f"在场占比 {days/n*100:.0f}%,随机时段无从对比"
    if days < 20 or not np.isfinite(ann):
        return np.nan, "样本太薄"
    fl = float((st[st != 0] > 0).mean())
    starts = rng.integers(0, max(n - days - 3, 1), SIMS)
    dirs = np.where(rng.random(SIMS) < fl, 1.0, -1.0)
    sims = np.empty(SIMS)
    for k in range(SIMS):
        seg = bv[starts[k] + lag: starts[k] + lag + days]
        sims[k] = ((float(np.prod(1 + dirs[k] * seg) ** (242 / max(len(seg), 1))) - 1) * 100
                   if len(seg) else 0.0)
    return float((sims >= ann).mean()), ""


def yearly(st, lag=2):
    p = np.concatenate([np.zeros(lag), st[:-lag]])
    s = pd.Series(p * bv, index=idx)[p != 0]
    return {y: (float(np.prod(1 + g)) - 1) * 100 for y, g in s.groupby(s.index.year)}


S = {m: state_vec(d) for m, d in DAILY.items()}
g0 = S[MAIN]


def with_filter(names):
    """国泰君安在场,且指定过滤器里**至少一家**同时在场且同向。"""
    ok = np.zeros(n, dtype=bool)
    for nm in names:
        f = S[FILTERS[nm]]
        ok |= (f != 0) & (f == g0)
    return np.where(ok, g0, 0.0)


SIGNALS = {
    "S0 国泰君安单独": g0,
    "S1 +摩根附议": with_filter(["摩根"]),
    "S2 +高盛附议": with_filter(["高盛"]),
    "S3 +摩根或高盛": with_filter(["摩根", "高盛"]),
}

bench = (float(np.prod(1 + bv) ** (242 / n)) - 1) * 100
L = [f"IH:「国泰君安方向 + 摩根/高盛附议」严测(样本 {idx[0].date()} ~ {idx[-1].date()},{n} 日)", ""]
L.append(f"同期恒多年化 {bench:+.1f}%。**这不是「三方共振」** —— 同频那个假设已被")
L.append("`REPORT_IH_SYNC_v1` 证否(超额同向率最高才 +9pp)。这里测的是另一件事:")
L.append("**国泰君安自己的方向带不带信息,摩根/高盛的附议加不加东西。**")
L.append("")
L.append("## 一、四个信号并排")
L.append("")
L.append(f"{'信号':<16}{'在场天':>7}{'占比':>6}{'段数':>5}{'择时增益':>9}{'在场年化':>9}"
         f"{'同期做多':>9}{'扣成本':>8}{'正年':>7}")
L.append("-" * 78)
rows = {}
for name, st in SIGNALS.items():
    edge, ann, lng, days = perf(st)
    ys = yearly(st)
    rows[name] = (st, edge, ann, days, ys)
    L.append(f"{name:<16}{days:>7}{days/n*100:>5.0f}%{len(blocks_of(st)):>5}{edge:>+9.1f}"
             f"{ann:>+9.1f}{lng:>+9.1f}{perf(st, cost=0.001)[1]:>+8.1f}"
             f"{sum(1 for v in ys.values() if v > 0):>4}/{len(ys):<2}")
L.append("")
e0 = rows["S0 国泰君安单独"][1]
L.append(f"**过滤器加了多少**(与 S0 的择时增益 {e0:+.1f}% 比):")
for name in ("S1 +摩根附议", "S2 +高盛附议", "S3 +摩根或高盛"):
    L.append(f"  {name}: {rows[name][1] - e0:+.1f} 个百分点")
L.append("")

# --------------------------------------------------- 二、安慰剂(带退化守门)
L.append("## 二、安慰剂检验(退化的**明说不报**,不硬凑一个 p)")
L.append("")
L.append(f"{'信号':<16}{'p(方向)':>10}{'p(时段)':>10}  说明")
L.append("-" * 62)
pvals = {}
for name, (st, *_rest) in rows.items():
    pd_, why_d = p_direction(st)
    pw_, why_w = p_window(st)
    pvals[name] = (pd_, pw_)
    L.append(f"{name:<16}{('  —  ' if not np.isfinite(pd_) else f'{pd_:.4f}'):>10}"
             f"{('  —  ' if not np.isfinite(pw_) else f'{pw_:.4f}'):>10}  "
             + "；".join(x for x in (why_d, why_w) if x))
L.append("")

# --------------------------------------------------- 三、稳健性
L.append("## 三、稳健性:延迟 / 前后半 / 逐年")
L.append("")
L.append(f"{'信号':<16}{'T+1':>8}{'T+2':>8}{'T+3':>8}{'T+5':>8}{'前半增益':>9}{'后半增益':>9}")
L.append("-" * 66)
h = n // 2
for name, (st, *_r) in rows.items():
    lags = [perf(st, lag=l)[0] for l in (2, 3, 4, 6)]
    e1 = perf(np.concatenate([st[:h], np.zeros(n - h)]))[0]
    e2 = perf(np.concatenate([np.zeros(h), st[h:]]))[0]
    f2 = lambda v: "    —" if not np.isfinite(v) else f"{v:+8.1f}"  # noqa: E731
    L.append(f"{name:<16}" + "".join(f2(v) for v in lags) + f2(e1) + f2(e2))
L.append("")
L.append("逐年择时收益(在场期间):")
yrs = sorted({y for _, (_, _, _, _, ys) in rows.items() for y in ys})
L.append(f"{'信号':<16}" + "".join(f"{y:>8}" for y in yrs))
L.append("-" * (16 + 8 * len(yrs)))
for name, (_, _, _, _, ys) in rows.items():
    L.append(f"{name:<16}" + "".join(
        (f"{ys[y]:>+8.0f}" if y in ys else "       —") for y in yrs))
L.append("")

# --------------------------------------------------- 四、walk-forward(选人过程)
# 我是从 77 家里挑中国泰君安的 —— 单独给它算 p 值答不了「事前选不选得出」。
# 这里把**选人这件事本身**放进历史:每年初,只用那一年之前的数据,按择时增益
# 选当时最好的一家(要求过去在场 ≥100 天且当时还在榜),再用它交易下一年。
L.append("## 四、Walk-forward:每年初只用过去的数据选人,交易下一年")
L.append("")
L.append("**为什么必须做**:国泰君安是我从 77 家里挑的。单独给它算 p 值回答不了")
L.append("「**事前**选不选得出」——IH 那一轮正是在这一步把 +25.8% 打回 +6.3% 的。")
L.append("")
years = sorted({d.year for d in idx})[1:]
picked, wf = [], np.zeros(n)
for y in years:
    past = idx < pd.Timestamp(f"{y}-01-01")
    fut = (idx >= pd.Timestamp(f"{y}-01-01")) & (idx < pd.Timestamp(f"{y+1}-01-01"))
    if past.sum() < 250 or fut.sum() < 20:
        continue
    bestm, beste = None, -1e9
    for m, st in S.items():
        stp = np.where(past, st, 0.0)
        e, _a, _l, dd = perf(stp)
        if dd < 100 or not np.isfinite(e):
            continue
        if (st[past][-60:] != 0).sum() == 0:      # 选人时它得还在榜
            continue
        if e > beste:
            bestm, beste = m, e
    if bestm is None:
        continue
    picked.append((y, bestm, beste))
    wf = np.where(fut, S[bestm], wf)
L.append(f"{'年份':<6}{'当年初选中':<12}{'其过去增益':>10}{'次年实际增益':>12}{'次年在场天':>11}")
L.append("-" * 52)
for y, m, e in picked:
    fut = (idx >= pd.Timestamp(f"{y}-01-01")) & (idx < pd.Timestamp(f"{y+1}-01-01"))
    st = np.where(fut, S[m], 0.0)
    ee, _a, _l, dd = perf(st)
    L.append(f"{y:<6}{m:<12}{e:>+10.1f}{(ee if np.isfinite(ee) else float('nan')):>+12.1f}{dd:>11}")
L.append("-" * 52)
edge_wf, ann_wf, lng_wf, days_wf = perf(wf)
L.append(f"合计:样本外在场 {days_wf} 天、年化 {ann_wf:+.1f}%、"
         f"同期一律做多 {lng_wf:+.1f}%、**择时增益 {edge_wf:+.1f}%**")
L.append(f"事前选中过国泰君安的年份:{sum(1 for _, m, _ in picked if m == MAIN)}/{len(picked)}")
L.append("")
# ---------------------------------------- 五、国泰君安到底特不特别(经验零分布)
# **逻辑先说破**:S1 在场那些天,国泰君安与摩根**按定义同向** —— 所以「谁是主信号、
# 谁是过滤器」分不开,它等价于「摩根的方向,只在国泰君安点头时才做」。
# 那真正该问的是:**换任何一家来点头,是不是都能把摩根的增益抬这么高?**
# 这个有经验分布可以测:把 77 家逐个当过滤器,看国泰君安排第几。
L.append("")
L.append("## 五、换任何一家当过滤器都行吗(国泰君安到底特不特别)")
L.append("")
L.append("**先说破一个逻辑**:S1 在场的那些天,国泰君安与摩根**按定义同向** ——")
L.append("所以「国泰君安是主信号、摩根是过滤器」这个说法反过来一样成立,两者分不开。")
L.append("它等价于:**摩根的方向,只在某家点头时才做**。那就该问:换谁点头都行吗?")
L.append("")
for anc_label, anc in (("摩根大通", S["摩根大通"]), ("高盛期货", S["高盛期货"])):
    base_e, base_a, _l, base_d = perf(anc)
    dist = []
    for m, st in S.items():
        if m in (anc_label,):
            continue
        both = (st != 0) & (st == anc)
        if both.sum() < 60:
            continue
        f = np.where(both, anc, 0.0)
        e, a, _lg, dd = perf(f)
        if np.isfinite(e):
            dist.append((e, m, a, dd))
    if not dist:
        continue
    dist.sort(reverse=True)
    es = np.array([x[0] for x in dist])
    rank = next((i + 1 for i, x in enumerate(dist) if x[1] == MAIN), None)
    L.append(f"### 以 {anc_label} 为方向源(它单独:增益 {base_e:+.1f}%、在场 {base_d} 天)")
    L.append(f"{len(dist)} 家可当过滤器(重叠同向 ≥60 天)。加过滤器后的增益分布:")
    L.append(f"  中位 {np.median(es):+.1f}%  ·  四分位 {np.percentile(es,25):+.1f}% ~ "
             f"{np.percentile(es,75):+.1f}%  ·  最高 {es.max():+.1f}%")
    top = [f"{m}({e:+.0f}%)" for e, m, _a, _d in dist[:5]]
    L.append("  前五:" + "、".join(top))
    if rank:
        e, m, a, dd = dist[rank - 1]
        pct = (es >= e).mean() * 100
        L.append(f"  **{MAIN} 排第 {rank}/{len(dist)}(前 {pct:.0f}%),增益 {e:+.1f}%、在场 {dd} 天**")
    L.append(f"  **{len([1 for e,*_ in dist if e > base_e])}/{len(dist)} 家**加进去都能抬高"
             f"{anc_label}单独的 {base_e:+.1f}% —— 这个比例才说明「点头」是不是普遍有效。")
    L.append("")

L.append("## 判定")
L.append("")
L.append("(结论看上面四节,尤其是「过滤器加了多少」与 walk-forward 那两处。)")
io.open(OUT / "ih_gtja.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
