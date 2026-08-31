# -*- coding: utf-8 -*-
"""IH PLAN v3:用三禾**全会员**口径复核「在场跟随」(2026-08-31)。

运营者原话:「要看三禾全会员口径」。前三轮(REPORT_IH_MODEL_v1/v2、
REPORT_IH_JPM_v1、REPORT_IH_STATE_SCAN_v1)全部跑在**中金所官方前 20**上,
那份数据有个结构性盲区:**排名掉出前 20 与真的清仓,在数据里长得一模一样**。
之前只能拿「掉榜沿用 20 个交易日」硬凑,那是假设不是观测。三禾按会员报,
一家在不在场是**观测到的**,这一轮就是拿观测去替换那个假设。

**方法一律冻结,不调参**(与 REPORT_IH_JPM_v1 / STATE_SCAN_v1 一字不差):
在场即持仓,方向 = 净持仓符号,T+1 执行(lag=2)。唯一的差别是三禾口径下
`carry=0` —— 因为「今天没有这家」在三禾就是真的没持仓,不需要沿用。

**先说这一轮答不了什么**:三禾窗口只有 2023-08-30~2026-08-28 三年,而高盛末次
上榜 2023-09-21、安粮 2023-01-13 —— 它俩的活跃期基本在窗口之外。三年也撑不起
按年 walk-forward。所以本轮的定位是**校准官方口径的偏差有多大**,以及**复核
窗口内仍活跃的那几家**,不是替代 STATE_SCAN 的十二年结论。

跑法:python research/run_ih_plan_v3.py

`research/data/` 按设计不入库(见 research/.gitignore),`ih_seat_sanhe.csv.gz`
这么重新生成 —— 在 qh 上:

    . /var/lib/futures-platform/deployments/stable.env
    python3 "$previous_release_dir/backfill/to_csv.py" --what sanhe \
      --sanhe-dir /opt/futures-platform/sanhe-seats/raw-all --want IH
    gzip -c /opt/futures-platform/load/seat_sanhe.csv > /root/ih_seat_sanhe.csv.gz

约 3 分钟(要扫 101,838 个原始文件),下回本机 research/data/。
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

NAMED = ["摩根大通", "中财期货", "高盛期货", "安粮期货"]
MIN_DAYS = 100          # 在场天数下限,与 STATE_SCAN 同,预注册
SIMS = 5000
rng = np.random.default_rng(20260831)

price = H.clean_price(pd.read_csv(D / "ih_price.csv.gz"))
off = H.clean_seat(pd.read_csv(D / "ih_seat.csv.gz"))
san = H.clean_seat(pd.read_csv(D / "ih_seat_sanhe.csv.gz"))
H.use("IH")
mkt = H.main_series(price)

# 两份数据的公共窗口 —— 比较必须在同一段日子上做,否则差异里混着"谁覆盖得久"。
LO = max(off["trade_date"].min(), san["trade_date"].min())
HI = min(off["trade_date"].max(), san["trade_date"].max())
mkt = mkt[(mkt.index >= LO) & (mkt.index <= HI)]
idx = mkt.index
bv = mkt["ret_open"].fillna(0.0).values
n = len(idx)


def daily_net(seat):
    """会员 → 逐日净持仓(只留窗口内、非零)。"""
    out = {}
    for m, g in seat.groupby("member_key"):
        d = g.groupby("trade_date")["net_off"].sum()
        d = d[d.index.isin(idx) & (d != 0)]
        if len(d):
            out[m] = d
    return out


OFF = daily_net(off)
SAN = daily_net(san)


def state_vec(d, carry):
    st = np.zeros(n)
    locs = idx.get_indexer(d.index)
    sgn = np.sign(d.values)
    for i, lo in enumerate(locs):
        end = locs[i + 1] if i + 1 < len(locs) and locs[i + 1] - lo <= carry else min(lo + carry + 1, n)
        st[lo:end] = sgn[i]
    return st


def ann_of(st, lag=2, floor=MIN_DAYS):
    p = np.concatenate([np.zeros(lag), st[:-lag]])
    r = p * bv
    live = r[p != 0]
    if len(live) < floor:
        return np.nan, len(live)
    return (float(np.prod(1 + live) ** (242 / len(live))) - 1) * 100, len(live)


def placebo_p(st, ann, days):
    """同在场天数、同多空比例、随机时段。

    **只在在场占比不高时才有意义。** 抽的是「另一段同样长的日子」,当一家
    721/725 天都在场,随机抽出来的几乎就是原来那一段,检验没有辨别力,p 必然
    趋近 0 —— 那不是显著,是没得比。调用处按在场占比决定要不要报它。
    """
    frac_long = float((st[st != 0] > 0).mean()) if (st != 0).any() else 0.5
    starts = rng.integers(0, max(n - days - 3, 1), SIMS)
    dirs = np.where(rng.random(SIMS) < frac_long, 1.0, -1.0)
    sims = np.empty(SIMS)
    for k in range(SIMS):
        seg = bv[starts[k] + 2: starts[k] + 2 + days]
        sims[k] = ((float(np.prod(1 + dirs[k] * seg) ** (242 / max(len(seg), 1))) - 1) * 100
                   if len(seg) else 0.0)
    return float((sims >= ann).mean())


def blocks_of(st):
    """把在场状态切成「连续同向段」,返回 [(起, 止, 方向), …]。"""
    out = []
    i = 0
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
    """**择时增益**:照它的方向做,比「同样这些天一律做多」多赚多少(年化百分点)。

    这才是「跟随」值不值的判据。一家常年净多、从不翻向,照它做 = 一路做多,
    增益天然为 0 —— 那不叫择时,叫单边持仓,跟不跟它没区别。
    """
    p = np.concatenate([np.zeros(lag), st[:-lag]])
    live = p != 0
    if live.sum() < 1:
        return np.nan, np.nan
    k = 242 / live.sum()
    mine = (float(np.prod(1 + (p * bv)[live]) ** k) - 1) * 100
    longs = (float(np.prod(1 + bv[live]) ** k) - 1) * 100
    return mine - longs, longs


def direction_p(st, edge, lag=2):
    """择时的安慰剂:**在场的日子一天不动,只把每一段的方向随机重掷**。

    问的是「它挑的方向有没有信息」,而不是「它在场的那段行情好不好」。
    对常年在场的席位这个检验依然有效(只要它翻过向);对从不翻向的席位它
    自动退化成没有功效 —— 那是如实反映:没有方向选择,就没有东西可检验。
    """
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


L = [f"IH PLAN v3:三禾全会员口径复核(公共窗口 {LO.date()} ~ {HI.date()},{n} 个交易日)", ""]
L.append(f"官方口径 {len(OFF)} 家上过榜;三禾口径 {len(SAN)} 家持过仓。")
L.append("")

# ------------------------------------------------------------------ 一、盲区
L.append("## 一、官方前 20 口径的盲区有多大")
L.append("")
L.append("每一行都是同一家、同一天,两份数据各自怎么说:")
L.append("")
L.append(f"{'席位':<10}{'三禾在场':>8}{'官方在榜':>9}{'掉榜但仍持仓':>14}{'占三禾在场':>11}{'隐形中位手数':>13}")
L.append("-" * 68)
blind_rows = []
for m, s in SAN.items():
    if len(s) < 20:
        continue
    o = OFF.get(m)
    s_days = set(s.index)
    o_days = set(o.index) if o is not None else set()
    hidden = sorted(s_days - o_days)
    blind_rows.append((m, len(s_days), len(o_days), len(hidden),
                       float(s.loc[hidden].abs().median()) if hidden else 0.0))
# 按盲区大小排,不是按在场天数排:常年在榜的大席位盲区天然为 0,排在前面什么也说明不了。
blind_rows.sort(key=lambda r: -r[3])
for m, sd, od, hd, med in blind_rows[:15]:
    L.append(f"{m:<10}{sd:>8}{od:>9}{hd:>14}{hd / sd * 100:>10.0f}%{med:>13,.0f}")
tot_s = sum(r[1] for r in blind_rows)
tot_h = sum(r[3] for r in blind_rows)
L.append("-" * 68)
L.append(f"{'合计':<10}{tot_s:>8}{sum(r[2] for r in blind_rows):>9}{tot_h:>14}{tot_h / max(tot_s, 1) * 100:>10.0f}%")
L.append("")
L.append("**「掉榜但仍持仓」就是官方口径看不见的那部分。** 之前用「沿用 20 日」去补它,")
L.append("补得对不对,只有拿三禾比过才知道 —— 下一节直接比。")
L.append("")

# ---------------------------------------------- 二、沿用 20 日这个假设对不对
L.append("## 二、「掉榜沿用 20 日」这个假设,补对了多少")
L.append("")
L.append("拿官方序列按沿用 20 日推出的在场状态,与三禾观测到的真实在场状态逐日比:")
L.append("")
L.append(f"{'席位':<10}{'两者一致':>9}{'官方多算':>9}{'官方少算':>9}{'方向反了':>9}{'一致率':>8}")
L.append("-" * 56)
agree_rows = []
for m, sd, od, hd, med in blind_rows[:15]:
    s_vec = state_vec(SAN[m], 0)
    o_vec = state_vec(OFF[m], 20) if m in OFF else np.zeros(n)
    same = int(((s_vec == o_vec)).sum())
    over = int(((o_vec != 0) & (s_vec == 0)).sum())
    under = int(((o_vec == 0) & (s_vec != 0)).sum())
    flip = int(((o_vec != 0) & (s_vec != 0) & (o_vec != s_vec)).sum())
    agree_rows.append((m, same, over, under, flip))
    L.append(f"{m:<10}{same:>9}{over:>9}{under:>9}{flip:>9}{same / n * 100:>7.0f}%")
L.append("")
L.append("**「官方多算」= 三禾说已经平了、官方还以为它在场** —— 跟着这种信号会白扛风险。")
L.append("**「方向反了」最凶**:官方以为在做多,实际人家已经翻空。")
L.append("")
L.append("### 那这个假设会不会**凭空造出** alpha?")
L.append("")
L.append("同一家、同一段日子,两种口径各自测一遍在场年化。差出来的部分不是它的本事,")
L.append("是沿用假设加上去的:")
L.append("")
L.append(f"{'席位':<10}{'官方沿用20日':>13}{'三禾真实':>10}{'假设加了多少':>13}{'一致率':>8}")
L.append("-" * 56)
infl = []
for m, sd, od, hd, med in blind_rows[:15]:
    a_off, d_off = ann_of(state_vec(OFF[m], 20), floor=30) if m in OFF else (np.nan, 0)
    a_san, d_san = ann_of(state_vec(SAN[m], 0), floor=30)
    if not (np.isfinite(a_off) and np.isfinite(a_san)):
        continue
    same = next(r[1] for r in agree_rows if r[0] == m) if any(r[0] == m for r in agree_rows) else np.nan
    infl.append(a_off - a_san)
    L.append(f"{m:<10}{a_off:>+13.1f}{a_san:>+10.1f}{a_off - a_san:>+13.1f}{same / n * 100:>7.0f}%")
L.append("-" * 56)
if infl:
    L.append(f"{'中位':<10}{'':>13}{'':>10}{np.median(infl):>+13.1f}")
L.append("")

# ------------------------------------------------------ 三、四家点名的复核
L.append("## 三、四家点名席位,三禾口径下的复核")
L.append("")
named_rows = []
for m in NAMED:
    s = SAN.get(m)
    if s is None or len(s) == 0:
        named_rows.append((m, "三禾窗口内**从未持仓**", None, None, None, None, None, None, None))
        continue
    st = state_vec(s, 0)
    a, days = ann_of(st, floor=1)
    edge, _ = timing_edge(st)
    flips = max(len(blocks_of(st)) - 1, 0)
    p_d = direction_p(st, edge) if days >= 20 else np.nan
    # 两个 p 问的是两件事,必须并排看:
    #   p(时段) —— 它**在场的那几段日子**挑得好不好(同长度随机时段作对照);
    #   p(方向) —— 它**每段做多还是做空**挑得对不对(时段不动,只重掷方向)。
    # 一家常年只做多,方向没有选择,p(方向) 恒为 1;它的本事(如果有)只可能在时段上。
    p_w = placebo_p(st, a, days) if (days >= 20 and np.isfinite(a) and days / n <= 0.70) else np.nan
    named_rows.append((m, "", a, days, edge, flips, p_w, p_d, s.index.max()))
L.append(f"{'席位':<10}{'在场天':>7}{'在场年化':>10}{'择时增益':>10}{'翻向':>5}{'p(时段)':>9}{'p(方向)':>9}{'末次持仓':>12}  备注")
L.append("-" * 90)
for m, note, a, days, edge, flips, p_w, p_d, last in named_rows:
    if note:
        L.append(f"{m:<10}{'—':>7}{'—':>10}{'—':>10}{'—':>5}{'—':>9}{'—':>9}{'—':>12}  {note}")
        continue
    tag = "样本太薄,不判" if days < MIN_DAYS else ""
    if flips >= 1 and np.isfinite(p_d) and p_d >= 0.999:
        tag = (tag + ";" if tag else "") + "在场时**全程净多,方向无选择**"
    fmt = lambda v: "  —  " if not np.isfinite(v) else f"{v:.3f}"  # noqa: E731
    L.append(f"{m:<10}{days:>7}{a:>+10.1f}{edge:>+10.1f}{flips:>5}"
             f"{fmt(p_w):>9}{fmt(p_d):>9}{str(last.date()):>12}  {tag}")
L.append("")

# ------------------------------------------------- 四、三禾口径全会员扫描
L.append(f"## 四、三禾口径全会员扫描(在场 ≥{MIN_DAYS} 天)")
L.append("")
rows = []
for m, s in SAN.items():
    st = state_vec(s, 0)
    a, days = ann_of(st)
    if not np.isfinite(a):
        continue
    edge, long_same = timing_edge(st)
    flips = max(len(blocks_of(st)) - 1, 0)
    p_dir = direction_p(st, edge)
    # 在场占比高于 70% 时,「随机时段」安慰剂没有辨别力,不报(见 placebo_p 说明)
    p = placebo_p(st, a, days) if days / n <= 0.70 else np.nan
    knobs = [ann_of(state_vec(s, c))[0] for c in (0, 5, 10, 20)]
    knob_pos = sum(1 for v in knobs if np.isfinite(v) and v > 0)
    lags = [ann_of(st, lag=l)[0] for l in (2, 3, 4, 6)]
    lag_pos = sum(1 for v in lags if np.isfinite(v) and v > 0)
    pos_lag = np.concatenate([np.zeros(2), st[:-2]])
    r = pd.Series(pos_lag * bv, index=idx)[pos_lag != 0]
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in r.groupby(r.index.year)}
    turn = np.abs(np.diff(np.concatenate([[0], pos_lag]))) > 0
    live_net = (pos_lag * bv - turn * 0.001)[pos_lag != 0]
    net = (float(np.prod(1 + live_net) ** (242 / len(live_net))) - 1) * 100 if len(live_net) else np.nan
    h = n // 2
    a1, _ = ann_of(np.concatenate([st[:h], np.zeros(n - h)]), floor=30)
    a2, _ = ann_of(np.concatenate([np.zeros(h), st[h:]]), floor=30)
    rows.append({"m": m, "ann": a, "days": days, "p": p, "knob": knob_pos, "lag": lag_pos,
                 "py": sum(1 for v in ys.values() if v > 0), "ny": len(ys), "net": net,
                 "half": (np.isfinite(a1) and a1 > 0) and (np.isfinite(a2) and a2 > 0),
                 "long": float((st[st != 0] > 0).mean()), "med": float(s.abs().median()),
                 "edge": edge, "flips": flips, "p_dir": p_dir, "long_same": long_same})
rows.sort(key=lambda r: (np.inf if not np.isfinite(r["p_dir"]) else r["p_dir"]))
N = len(rows)
bench_ann = (float(np.prod(1 + bv) ** (242 / n)) - 1) * 100
L.append(f"{N} 家同时测,纯随机预期 {N * 0.05:.1f} 家 p<0.05;Bonferroni 阈值 = {0.05 / max(N,1):.5f}。")
L.append(f"同期恒多年化 {bench_ann:+.1f}%。")
L.append("")
L.append("**判据是「择时增益」不是「在场年化」。** 增益 = 照它的方向做,比「同样这些天")
L.append("一律做多」多赚的年化百分点;p(方向) = 在场日子一天不动、只把每段方向随机重掷")
L.append("得到的安慰剂。常年净多从不翻向的席位,增益天然 ≈0 —— 跟它等于一路做多。")
L.append("")
L.append(f"{'席位':<10}{'择时增益':>9}{'p(方向)':>9}{'翻向':>5}{'在场年化':>9}{'在场天':>7}{'在场占比':>9}{'旋钮':>5}{'延迟':>5}{'扣成本':>8}{'分半':>5}  {'方向':<5}{'中位手数':>9}")
L.append("-" * 100)
for r in rows[:20]:
    pd_s = "  —  " if not np.isfinite(r["p_dir"]) else f"{r['p_dir']:.3f}"
    L.append(f"{r['m']:<10}{r['edge']:>+9.1f}{pd_s:>9}{r['flips']:>5}{r['ann']:>+9.1f}{r['days']:>7}"
             f"{r['days'] / n * 100:>8.0f}%{r['knob']:>4}/4{r['lag']:>4}/4{r['net']:>+8.1f}"
             f"{'  ✓' if r['half'] else '  ✗':>5}  {'净多' if r['long'] > 0.5 else '净空':<5}{r['med']:>9,.0f}")
# 过闸条件全部预注册:择时有增益、方向不是碰巧、翻过向、旋钮与延迟都扛得住、前后半均正。
strong = [r for r in rows
          if np.isfinite(r["edge"]) and r["edge"] > 0
          and np.isfinite(r["p_dir"]) and r["p_dir"] < 0.05
          and r["flips"] >= 4 and r["knob"] == 4 and r["lag"] == 4 and r["half"]]
L.append("")
L.append(f"**过闸(择时增益>0 且 p(方向)<0.05 且 翻向≥4 次 且 旋钮 4/4 且 延迟 4/4 且 分半均正)= {len(strong)} 家**")
for r in strong:
    bonf = "**过 Bonferroni**" if r["p_dir"] < 0.05 / max(N, 1) else "不过 Bonferroni"
    L.append(f"   ★ {r['m']}: 择时增益 {r['edge']:+.1f}%/年  p(方向)={r['p_dir']:.4f} {bonf}"
             f"  翻向 {r['flips']} 次  在场 {r['days']} 天({r['days']/n*100:.0f}%)"
             f"  中位 {r['med']:,.0f} 手")
if not strong:
    L.append("   (无)")
L.append("")
L.append("**三年窗口的功效有限**:同一家在十二年官方序列上能积累 400~800 个在场日,")
L.append("这里最多三百出头。看不出显著 ≠ 没有,看出显著也要防三年一段的运气。")

io.open(OUT / "ih_plan_v3.txt", "w", encoding="utf-8").write("\n".join(L))
print(f"done: 盲区 {len(blind_rows)} 家,扫描 {N} 家,过闸 {len(strong)} 家")
