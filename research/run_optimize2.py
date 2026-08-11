# -*- coding: utf-8 -*-
"""v2 收尾:共振过滤 + 深回调档 + 最优组合的分年度/逐笔。"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_optimize import CORE7, run_trades, summarize, score_series, expanding_weights, build_events

pd.set_option("display.width", 220)
pd.set_option("display.max_rows", 100)


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")
    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    op = price.set_index(["contract", "trade_date"])["open_price"].unstack(0)
    cont = cont.copy()
    cont["open"] = [op.at[d, m] if m in op.columns else np.nan for d, m in zip(mc["trade_date"], mc["main"])]
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]

    ev = build_events(md, cont, CORE7)
    years = range(cont.index[0].year, cont.index[-1].year + 1)
    weights = expanding_weights(ev, years)
    score = score_series(ev, cont, weights)

    close = cont["adj_close"]
    conf = (close >= close.rolling(20).max() * 0.999) & (close > close.rolling(200).mean())

    # 共振家数:过去5日内有增多事件、且当年权重>0 的不同席位数
    dates = cont.index
    has_ev = ev.pivot_table(index="trade_date", columns="member", values="strength", aggfunc="max").reindex(dates)
    has5 = has_ev.notna().rolling(5, min_periods=1).max()
    wpos = pd.DataFrame({m: [1.0 if weights[d.year].get(m, 0) > 0 else 0.0 for d in dates] for m in CORE7},
                        index=dates)
    nseat = (has5[CORE7].fillna(0) * wpos).sum(axis=1)

    rows = []
    combos = []
    for theta in (4.0, 5.0, 6.0):
        for k in (1, 2, 3):
            cand = list(dates[(score >= theta) & (nseat >= k) & conf])
            for pb in (0.02, 0.025, 0.03):
                tr, miss = run_trades(cont, cand, entry="pullback", pullback=pb, stop=0.03)
                s = summarize(tr, miss)
                rows.append({"θ": theta, "共振家数>=": k, "回调%": pb * 100, **s})
                combos.append((theta, k, pb, tr))
    res = pd.DataFrame(rows)
    print("== 共振 × 深回调网格(止损3%) ==")
    print(res.to_string(index=False))

    # 选达标率最高且笔数>=20 的组合展开
    res2 = res[res["笔数"] >= 20].sort_values(["达标率%", "均收益%"], ascending=False)
    print("\n== 笔数>=20 中按达标率排序前5 ==")
    print(res2.head(5).to_string(index=False))
    bi = res2.index[0]
    theta, k, pb, tr = combos[bi]
    print(f"\n== 最优组合 θ={theta} 共振>={k} 回调{pb*100}% 分年度 ==")
    t2 = tr.copy()
    t2["年"] = pd.to_datetime(t2["进场日"]).dt.year
    print(t2.groupby("年").agg(笔数=("收益%", "size"), 均收益=("收益%", "mean"),
                              达标=("结果", lambda x: (x == "目标").mean() * 100)).round(2).to_string())
    print("\n== 逐笔 ==")
    print(tr.round(2).to_string(index=False))
    tr.to_pickle(aulib.OUT / "trades_v2_best.pkl")
    print("\n已写出 out/trades_v2_best.pkl")


if __name__ == "__main__":
    sys.exit(main())
