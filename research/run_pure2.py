# -*- coding: utf-8 -*-
"""纯席位 v4.1:加两个增强指标后的 walk-forward OOS 验证。

新增维度(运营者要求"加指标提升达标率、买到真正低点"):
  dist_low60  价格距60日最低收盘的涨幅上限(不追高;非滞后确认)
  netq        核心席位合计净多的250日分位上限(机构低仓新建仓;纯持仓指标)
空间 = θ(4) × 共振(3) × 持续(3) × dist(3) × netq(3) = 324 组合,逐年 walk-forward。
出场沿用纯席位口径评估:达标=先+10%后-3%;另附「信号消退」卖法的整轮收益。
"""
import sys
from itertools import product

import numpy as np
import pandas as pd

import aulib
from run_pure import features, run_trades_pure

pd.set_option("display.width", 220)
WF_START = 2015


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")
    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    op = price.set_index(["contract", "trade_date"])["open_price"].unstack(0)
    cont = cont.copy()
    cont["open"] = [op.at[d, m] if m in op.columns else np.nan for d, m in zip(mc["trade_date"], mc["main"])]
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]

    f = features(cont, md)
    c = cont["adj_close"]
    f["dist60"] = c / c.rolling(60).min() - 1
    f["netq"] = f["netsum"].rolling(250, min_periods=120).rank(pct=True)

    space = list(product([2.0, 4.0, 6.0, 8.0], [1, 2, 3], [1, 3, 5],
                         [None, 0.08, 0.12], [None, 0.4, 0.6]))
    cache = []
    for theta, k, p, dl, nq in space:
        m = (f["score5"] >= theta) & (f["nseat5"] >= k) & (f["pers10"] >= p)
        if dl is not None:
            m &= f["dist60"] < dl
        if nq is not None:
            m &= f["netq"] < nq
        cand = list(f.index[m])
        tr = run_trades_pure(cont, cand) if len(cand) >= 15 else pd.DataFrame()
        cache.append(((theta, k, p, dl, nq), tr))

    def sc(tr, upto):
        d = tr[pd.to_datetime(tr["出场日"]) < upto] if len(tr) else tr
        if len(d) < 12:
            return -np.inf
        return d["收益%"].mean() - d["收益%"].std(ddof=1) / np.sqrt(len(d))

    oos, picks = [], []
    for y in range(WF_START, cont.index[-1].year + 1):
        cutoff = pd.Timestamp(f"{y}-01-01")
        best = max(range(len(cache)), key=lambda i: sc(cache[i][1], cutoff))
        (theta, k, p, dl, nq), tr = cache[best]
        yr = tr[pd.to_datetime(tr["进场日"]).dt.year == y] if len(tr) else pd.DataFrame()
        oos.append(yr)
        picks.append({"年": y, "θ": theta, "共振": k, "持续": p,
                      "距低点<": dl if dl else "-", "净仓分位<": nq if nq else "-", "当年笔数": len(yr)})
    oos_tr = pd.concat(oos, ignore_index=True)
    print("== v4.1 逐年所选规则 ==")
    print(pd.DataFrame(picks).to_string(index=False))
    d = oos_tr
    print(f"\n== v4.1 纯席位+双增强 OOS:笔数 {len(d)},达标率 {(d['结果']=='目标').mean()*100:.1f}%,"
          f"止损率 {(d['结果']=='止损').mean()*100:.1f}%,均收益 {d['收益%'].mean():.2f}%,总 {d['收益%'].sum():.1f}% ==")
    d2 = d.copy()
    d2["年"] = pd.to_datetime(d2["进场日"]).dt.year
    print(d2.groupby("年").agg(笔数=("收益%", "size"), 均收益=("收益%", "mean"),
                              达标=("结果", lambda x: (x == "目标").mean() * 100)).round(2).to_string())
    print("\n(对照:v4 纯席位无增强 OOS 52 笔/32.7%/+1.96%;全样本筛选时该组合 42.9%)")

    # 买点低不低
    zz = pd.read_pickle(aulib.OUT / "au_zigzag10.pkl")
    ups = zz[zz["type"] == "上涨"][["from", "from_px"]].dropna()
    ds = []
    for _, e in oos_tr.iterrows():
        prev = ups[ups["from"] <= pd.Timestamp(e["信号日"])]
        if len(prev):
            ds.append((e["进场价"] / prev.iloc[-1]["from_px"] - 1) * 100)
    print(f"\n进场价高于最近波段谷:中位 {np.median(ds):.1f}%(v4 无增强为 ~12%)")
    oos_tr.to_pickle(aulib.OUT / "pure2_oos_trades.pkl")
    print("已写出 out/pure2_oos_trades.pkl")


if __name__ == "__main__":
    sys.exit(main())
