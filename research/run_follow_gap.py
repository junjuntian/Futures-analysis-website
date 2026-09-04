# -*- coding: utf-8 -*-
"""永安在玻纯上赚很多,为什么跟随它测出来这么差?

把它的真实盯市盈亏拆成三块,逐块看跟随卡能不能抓到:
  A. 品种净方向(= 跟随卡唯一复制的东西)
  B. 品种内部的跨月结构(FG2701 多 / FG2611 空 那种)—— 卡片 2026-09-04 起有意丢掉
  C. 时点差:它按当日持仓算,跟随者 T+1 才进场
再看「对冲态」那道门挡掉了多少钱。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
MEM = ["永安期货", "东证期货"]
MULT = 20.0


def load(code, stem):
    H.use(code)
    price = H.clean_price(pd.read_csv(D / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{stem}_seat.csv.gz"))
    return price, seat, H.main_series(price)


PX, ST, MK = {}, {}, {}
for c, s in (("FG", "fg"), ("SA", "sa")):
    PX[c], ST[c], MK[c] = load(c, s)
IDX = MK["FG"].index.intersection(MK["SA"].index)
IDX = IDX[IDX >= pd.Timestamp("2020-06-01")]


def pieces(member):
    tot = pd.Series(0.0, index=IDX)      # A+B:真实逐合约盯市
    net_only = pd.Series(0.0, index=IDX)  # A:只按品种净方向 × 主力涨跌
    for k in ("FG", "SA"):
        s = ST[k][ST[k].member_key == member]
        w = s.pivot_table(index="trade_date", columns="contract",
                          values="net_off", aggfunc="sum")
        p = PX[k].pivot_table(index="trade_date", columns="contract",
                              values="settle", aggfunc="first")
        cols = w.columns.intersection(p.columns)
        w, p = w.reindex(IDX)[cols], p.reindex(IDX)[cols]
        dp = p.diff()
        # 真实:每个合约各自的持仓 × 该合约涨跌
        tot = tot.add((w.shift() * dp).sum(axis=1), fill_value=0.0)
        # 净方向:品种合计净持仓 × **主力合约**涨跌
        vnet = w.sum(axis=1)
        main = MK[k]["main"].reindex(IDX)
        mdp = pd.Series(
            [dp.at[d, main[d]] if (isinstance(main.get(d), str)
                                   and main[d] in dp.columns
                                   and np.isfinite(dp.at[d, main[d]])) else 0.0
             for d in IDX], index=IDX)
        net_only = net_only.add(vnet.shift() * mdp, fill_value=0.0)
    return tot * MULT, net_only * MULT


def vnets(member):
    out = {}
    for k in ("FG", "SA"):
        s = ST[k][ST[k].member_key == member]
        out[k] = s.groupby("trade_date")["net_off"].sum().reindex(IDX)
    return out


print(f"样本 {IDX[0].date()} ~ {IDX[-1].date()},{len(IDX)} 天;单位:亿元\n")
for m in MEM:
    tot, net_only = pieces(m)
    resid = tot - net_only
    v = vnets(m)
    hedge = (v["FG"] * v["SA"] < 0).reindex(IDX).fillna(False)
    print(f"=== {m} ===")
    print(f"  A+B 真实盯市盈亏合计      {tot.sum()/1e8:+8.2f}")
    print(f"  A   只算品种净方向×主力      {net_only.sum()/1e8:+8.2f}   ← 跟随卡唯一能复制的")
    print(f"  B   品种内部跨月结构(残差)  {resid.sum()/1e8:+8.2f}   ← 卡片有意丢掉的")
    print(f"      B 占真实盈亏             {resid.sum()/tot.sum()*100 if tot.sum() else float('nan'):7.0f}%")
    print(f"  —— 「对冲态」那道门 ——")
    print(f"  对冲态天数 {int(hedge.sum())}/{len(IDX)} = {hedge.mean()*100:.0f}%")
    print(f"    A 在对冲态日   {net_only[hedge].sum()/1e8:+8.2f}")
    print(f"    A 在同向日     {net_only[~hedge].sum()/1e8:+8.2f}   ← 卡片空仓,这部分放弃了")
    print()

print("=== 时点差:T+0 vs T+1(只看 A,即净方向那一块)===")
for m in MEM:
    _t, net_only = pieces(m)
    v = vnets(m)
    hedge = (v["FG"] * v["SA"] < 0).reindex(IDX).fillna(False)
    lag = pd.Series(0.0, index=IDX)
    for k in ("FG", "SA"):
        p = PX[k].pivot_table(index="trade_date", columns="contract",
                              values="settle", aggfunc="first").reindex(IDX)
        main = MK[k]["main"].reindex(IDX)
        dp = p.diff()
        mdp = pd.Series(
            [dp.at[d, main[d]] if (isinstance(main.get(d), str)
                                   and main[d] in dp.columns
                                   and np.isfinite(dp.at[d, main[d]])) else 0.0
             for d in IDX], index=IDX)
        lag = lag.add(v[k].shift(2) * mdp, fill_value=0.0)   # 多滞后一天
    lag *= MULT
    a0 = net_only[hedge].sum() / 1e8
    a1 = lag[hedge].sum() / 1e8
    print(f"  {m}  对冲态日  T+0 {a0:+7.2f}   T+1 {a1:+7.2f}   "
          f"差 {a1 - a0:+7.2f}({(a1-a0)/abs(a0)*100 if a0 else float('nan'):+.0f}%)")
