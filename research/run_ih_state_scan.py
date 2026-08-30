# -*- coding: utf-8 -*-
"""IH 全席位「在场状态」法重测(2026-08-30 运营者:按摩根大通那个方法全测一遍)。

方法冻结(与 REPORT_IH_JPM_v1 一字不差,不再调参):
  在榜即在场,方向=净持仓符号,T+1 执行,掉榜沿用 20 个交易日。
判定不靠单一 p 值——**多重检验下 144 家里纯随机也会有 7 家 p<0.05**,
所以每家同时算五项稳健性(沿用旋钮 5 档 / 延迟 4 档 / 逐年 / 扣成本 / 分半),
真信号应当在扰动下同向,挖出来的假信号不会。

跑法:python research/run_ih_state_scan.py
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / "ih_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "ih_seat.csv.gz"))
H.use("IH")
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
idx = mkt.index
bench = mkt["ret_open"].fillna(0.0)
bv = bench.values
n = len(idx)
MIN_DAYS = 100          # 在场天数下限:少于此的年化不稳,预注册排除
SIMS = 1000
rng = np.random.default_rng(20260830)

daily_by_member = {}
for m, g in seat.groupby("member_key"):
    d = g.groupby("trade_date")["net_off"].sum()
    d = d[d.index.isin(idx) & (d != 0)]
    if len(d) >= 5:
        daily_by_member[m] = d


def state_vec(d, carry):
    """在场状态向量(±1/0)。"""
    st = np.zeros(n)
    locs = idx.get_indexer(d.index)
    sgn = np.sign(d.values)
    for i, lo in enumerate(locs):
        end = locs[i + 1] if i + 1 < len(locs) and locs[i + 1] - lo <= carry else min(lo + carry + 1, n)
        st[lo:end] = sgn[i]
    return st


def ann_of(st, lag=2):
    """在场期间年化(%)。"""
    p = np.concatenate([np.zeros(lag), st[:-lag]])
    r = p * bv
    live = r[p != 0]
    if len(live) < MIN_DAYS:
        return np.nan, 0
    return (float(np.prod(1 + live) ** (242 / len(live))) - 1) * 100, len(live)


rows = []
for m, d in daily_by_member.items():
    st = state_vec(d, 20)
    a, days = ann_of(st)
    if not np.isfinite(a) or days < MIN_DAYS:
        continue
    # 安慰剂:同在场天数、同方向比例,随机时段
    frac_long = float((st[st != 0] > 0).mean())
    sims = np.empty(SIMS)
    starts = rng.integers(0, max(n - days - 3, 1), SIMS)
    dirs = np.where(rng.random(SIMS) < frac_long, 1.0, -1.0)
    for k in range(SIMS):
        seg = bv[starts[k] + 2: starts[k] + 2 + days]
        sims[k] = (float(np.prod(1 + dirs[k] * seg) ** (242 / max(len(seg), 1))) - 1) * 100 if len(seg) else 0.0
    p_val = float((sims >= a).mean())
    # 稳健性
    knobs = [ann_of(state_vec(d, c))[0] for c in (5, 10, 20, 30, 40)]
    knob_pos = sum(1 for v in knobs if np.isfinite(v) and v > 0)
    lags = [ann_of(st, lag=l)[0] for l in (2, 3, 4, 6)]
    lag_pos = sum(1 for v in lags if np.isfinite(v) and v > 0)
    pos_lag = np.concatenate([np.zeros(2), st[:-2]])
    r = pd.Series(pos_lag * bv, index=idx)
    r = r[pos_lag != 0]
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in r.groupby(r.index.year)}
    pos_years = sum(1 for v in ys.values() if v > 0)
    turn = np.abs(np.diff(np.concatenate([[0], pos_lag]))) > 0
    r_net = pos_lag * bv - turn * 0.001
    live_net = r_net[pos_lag != 0]
    ann_net = (float(np.prod(1 + live_net) ** (242 / len(live_net))) - 1) * 100 if len(live_net) else np.nan
    h = n // 2
    a1, _ = ann_of(np.concatenate([st[:h], np.zeros(n - h)]))
    a2, _ = ann_of(np.concatenate([np.zeros(h), st[h:]]))
    half_ok = (np.isfinite(a1) and a1 > 0) and (np.isfinite(a2) and a2 > 0)
    rows.append({"m": m, "ann": a, "days": days, "p": p_val, "knob": knob_pos, "lag": lag_pos,
                 "py": pos_years, "ny": len(ys), "net": ann_net, "half": half_ok,
                 "long": frac_long, "med": float(d.abs().median())})

rows.sort(key=lambda r: r["p"])
bench_ann = (float(np.prod(1 + bv) ** (242 / n)) - 1) * 100
N = len(rows)
L = [f"IH 全席位「在场状态」法重测(在场≥{MIN_DAYS}天的 {N} 家;恒多年化 {bench_ann:+.1f}%)", ""]
L.append(f"多重检验:{N} 家同时测,纯随机预期 {N*0.05:.1f} 家会 p<0.05;")
L.append(f"Bonferroni 阈值 = 0.05/{N} = {0.05/N:.5f};下表按 p 排序,**看稳健性列决定信不信**。")
L.append("")
L.append(f"{'席位':<8}{'在场年化':>8}{'在场天':>6}{'p值':>7}{'旋钮':>5}{'延迟':>5}{'正年':>7}{'扣成本':>8}{'分半':>5}  {'方向':<5}{'中位手数':>8}")
L.append("-" * 92)
for r in rows[:25]:
    L.append(f"{r['m']:<8}{r['ann']:>+8.1f}{r['days']:>6}{r['p']:>7.3f}{r['knob']:>4}/5{r['lag']:>4}/4"
             f"{r['py']:>5}/{r['ny']:<2}{r['net']:>+8.1f}{'  ✓' if r['half'] else '  ✗':>5}  "
             f"{'净多' if r['long']>0.5 else '净空':<5}{r['med']:>8,.0f}")
L.append("")
strong = [r for r in rows if r["p"] < 0.05 and r["knob"] == 5 and r["lag"] == 4 and r["half"]
          and r["py"] >= 0.7 * r["ny"] and np.isfinite(r["net"]) and r["net"] > bench_ann]
L.append(f"**全项通过(p<0.05 且 旋钮5/5 且 延迟4/4 且 分半均正 且 正年≥70% 且 扣成本赢基准):{len(strong)} 家**")
for r in strong:
    L.append(f"   ★ {r['m']}: 年化 {r['ann']:+.1f}%  p={r['p']:.3f}  在场 {r['days']} 天"
             f"  {'净多' if r['long']>0.5 else '净空'}  中位 {r['med']:,.0f} 手")
L.append("")
L.append(f"参考:纯随机下同时满足这 6 项的期望家数 ≈ {N*0.05*0.5*0.5*0.5:.2f}(粗估,各项非独立)")
io.open(OUT / "ih_state_scan.txt", "w", encoding="utf-8").write("\n".join(L))
print(f"done: {N} seats, {len(strong)} strong")
