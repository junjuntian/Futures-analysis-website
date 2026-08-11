# -*- coding: utf-8 -*-
"""规则搜索 v3:数据自己选规则(walk-forward),不再人工拍先验。

第一层(买点):枚举规则空间 1296 组合:
  θ(加权分数) x 共振家数 x 持续天数 x 趋势过滤 x 价格位置 x 进场方式
  出场固定:止损3%(运营者硬约束)/ 目标+10% / 超时120日
  逐年 walk-forward:每年初用 <当年 的已完结交易给规则打分,选最优规则在当年执行;
  全部年份的当年交易拼接 = 样本外(OOS)绩效,才是真实预期。
第二层(卖点):对 OOS 胜出信号族,对比五种出场。
"""
import sys
from itertools import product

import numpy as np
import pandas as pd

import aulib
from run_optimize import CORE7, build_events, expanding_weights, run_trades

pd.set_option("display.width", 220)
pd.set_option("display.max_rows", 120)

STOP = 0.03
WF_START = 2015  # 训练窗至少 2010-2014


def precompute(cont, md):
    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    op = price.set_index(["contract", "trade_date"])["open_price"].unstack(0)
    cont = cont.copy()
    cont["open"] = [op.at[d, m] if m in op.columns else np.nan for d, m in zip(mc["trade_date"], mc["main"])]
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]

    ev = build_events(md, cont, CORE7)
    years = range(cont.index[0].year, cont.index[-1].year + 1)
    weights = expanding_weights(ev, years)
    dates = cont.index

    strong = ev.pivot_table(index="trade_date", columns="member", values="strength", aggfunc="max").reindex(dates)
    wmat = pd.DataFrame({m: [weights[d.year].get(m, 0.0) for d in dates] for m in CORE7}, index=dates)
    contrib = strong[CORE7].fillna(0) * wmat
    feats = pd.DataFrame(index=dates)
    feats["score"] = contrib.rolling(5, min_periods=1).max().sum(axis=1)
    active = (strong[CORE7].notna() & (wmat > 0))
    feats["nseat5"] = active.rolling(5, min_periods=1).max().sum(axis=1)
    feats["pers10"] = active.any(axis=1).rolling(10, min_periods=1).sum()

    c = cont["adj_close"]
    feats["gt_ma50"] = c > c.rolling(50).mean()
    feats["gt_ma200"] = c > c.rolling(200).mean()
    feats["hh20"] = c >= c.rolling(20).max() * 0.999
    feats["hh10"] = c >= c.rolling(10).max() * 0.999
    return cont, feats


RULE_SPACE = {
    "theta": [2.0, 4.0, 6.0, 8.0],
    "k": [1, 2, 3],
    "pers": [1, 3, 5],
    "trend": ["none", "ma50", "ma200"],
    "pos": ["none", "hh10", "hh20"],
    "entry": [("breakout", 0.0), ("pullback", 0.01), ("pullback", 0.02), ("pullback", 0.03)],
}


def rule_candidates(feats, r):
    m = (feats["score"] >= r["theta"]) & (feats["nseat5"] >= r["k"]) & (feats["pers10"] >= r["pers"])
    if r["trend"] != "none":
        m &= feats[f"gt_{r['trend']}"]
    if r["pos"] != "none":
        m &= feats[r["pos"]]
    return list(feats.index[m])


def all_rules():
    keys = list(RULE_SPACE)
    for vals in product(*RULE_SPACE.values()):
        yield dict(zip(keys, vals))


def score_rule(tr, upto=None, min_n=12):
    """训练窗评分:保守期望 = 均收益 - 标准误;样本不足返回 -inf。"""
    d = tr if upto is None else tr[pd.to_datetime(tr["出场日"]) < upto]
    if len(d) < min_n:
        return -np.inf
    mu, se = d["收益%"].mean(), d["收益%"].std(ddof=1) / np.sqrt(len(d))
    return mu - se


def main():
    cont0 = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")
    cont, feats = precompute(cont0, md)

    print(f"规则空间: {np.prod([len(v) for v in RULE_SPACE.values()])} 组合;缓存各规则全期逐笔…")
    cache = []
    for i, r in enumerate(all_rules()):
        cand = rule_candidates(feats, r)
        if len(cand) < 15:
            cache.append((r, pd.DataFrame()))
            continue
        etype, pb = r["entry"]
        tr, _ = run_trades(cont, cand, entry=etype, pullback=pb, stop=STOP)
        cache.append((r, tr))
    n_ok = sum(1 for _, t in cache if len(t))
    print(f"有效规则(全期>=1笔): {n_ok}")

    # ===== walk-forward =====
    oos, picks = [], []
    for y in range(WF_START, cont.index[-1].year + 1):
        cutoff = pd.Timestamp(f"{y}-01-01")
        best_i, best_s = None, -np.inf
        for i, (r, tr) in enumerate(cache):
            if tr.empty:
                continue
            s = score_rule(tr, upto=cutoff)
            if s > best_s:
                best_i, best_s = i, s
        if best_i is None:
            continue
        r, tr = cache[best_i]
        yr = tr[pd.to_datetime(tr["进场日"]).dt.year == y]
        oos.append(yr)
        picks.append({"年": y, "θ": r["theta"], "共振": r["k"], "持续": r["pers"], "趋势": r["trend"],
                      "位置": r["pos"], "进场": f"{r['entry'][0]}{r['entry'][1] * 100:.0f}",
                      "训练分": round(best_s, 2), "当年笔数": len(yr)})
    oos_tr = pd.concat(oos, ignore_index=True) if oos else pd.DataFrame()
    print("\n== 逐年所选规则 ==")
    print(pd.DataFrame(picks).to_string(index=False))

    print(f"\n== OOS 拼接绩效({WF_START}-2026,每年执行上年选出的最优规则) ==")
    if len(oos_tr):
        d = oos_tr
        print(f"笔数 {len(d)},达标率 {(d['结果'] == '目标').mean() * 100:.1f}%,"
              f"止损率 {(d['结果'] == '止损').mean() * 100:.1f}%,"
              f"均收益 {d['收益%'].mean():.2f}%,总收益 {d['收益%'].sum():.1f}%")
        d2 = d.copy()
        d2["年"] = pd.to_datetime(d2["进场日"]).dt.year
        print(d2.groupby("年").agg(笔数=("收益%", "size"), 均收益=("收益%", "mean"),
                                  达标=("结果", lambda x: (x == "目标").mean() * 100)).round(2).to_string())

    # in-sample top5(仅参考)
    rows = []
    for r, tr in cache:
        if len(tr) < 20:
            continue
        rows.append({"θ": r["theta"], "共振": r["k"], "持续": r["pers"], "趋势": r["trend"], "位置": r["pos"],
                     "进场": f"{r['entry'][0]}{r['entry'][1] * 100:.0f}", "笔数": len(tr),
                     "达标率%": round((tr["结果"] == "目标").mean() * 100, 1),
                     "均收益%": round(tr["收益%"].mean(), 2), "保守分": round(score_rule(tr), 2)})
    top = pd.DataFrame(rows).sort_values("保守分", ascending=False)
    print("\n== 全期 in-sample top 10(过拟合参考,不作为主结论) ==")
    print(top.head(10).to_string(index=False))

    oos_tr.to_pickle(aulib.OUT / "oos_trades.pkl")
    pd.DataFrame(picks).to_pickle(aulib.OUT / "wf_picks.pkl")
    top.to_pickle(aulib.OUT / "insample_top.pkl")
    print("\n已写出 out/oos_trades.pkl, wf_picks.pkl, insample_top.pkl")


if __name__ == "__main__":
    sys.exit(main())
