# -*- coding: utf-8 -*-
"""v4.5:目标=超过高盛的收益(运营者 2026-08-11)。

第一步把目标定量:高盛/中财 2023-08~2026-08 的收益率基准(合约价值口径,无杠杆:
累计盯市盈亏 ÷ 日均持仓手数 ÷ 期间平均合约价值)。
第二步测增强(基线=方案A:80分位/250窗/5日窗+成本区间冻结+止损4%+消退10日T+1):
  E1 盈利导向权重:高盛=中财=5.0 顶格(运营者先验),其余席位扩窗 t 值
  E2 金字塔加仓:持仓中浮盈>2% 且出现新的有效增多事件 → 加 0.5 单位,最多加 2 次;
     各批次独立 -4% 止损,共同消退卖出(跟随机构分批建仓)
  E3 消退放宽 10→15 日(在场更久)
  E4 = E1+E2+E3
两个窗口报数:全期 2015- 与 高盛可比窗口 2023-08-。收益按复利。
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_pure import features
from run_v42 import COND_SEATS, seat_cost_series
from run_optimize import CORE7, build_events, expanding_weights
from run_profile import mark_to_market_pnl

pd.set_option("display.width", 250)

GS_WIN = pd.Timestamp("2023-08-01")


def gs_benchmark(seat_all, price):
    m = mark_to_market_pnl(seat_all, price)
    m = m[m["trade_date"] >= GS_WIN]
    setl = price[price["trade_date"] >= GS_WIN].groupby("trade_date")["settlement_price"].mean()
    avg_val = setl.mean() * 1000  # 一手平均合约价值
    out = {}
    for name in ["高盛期货", "中财期货"]:
        d = m[m["member"] == name]
        pnl = d["pnl"].sum()
        hands = d.groupby("trade_date")["pos"].apply(lambda x: x.abs().sum()).mean()
        out[name] = pnl / hands / avg_val * 100
    return out, avg_val


def replay(cont, f, ev_eff, w, active10, costs, fade_days=10, pyramid=False,
           since=None):
    dates = cont.index
    pos = {d: i for i, d in enumerate(dates)}
    wser = pd.Series(w)
    strong = ev_eff.pivot_table(index="trade_date", columns="member", values="strength",
                                aggfunc="max").reindex(dates).reindex(columns=CORE7)
    score = (strong.fillna(0) * wser).rolling(5, min_periods=1).max().sum(axis=1)
    ev_day = (strong.notna() & (wser > 0)).any(axis=1).to_numpy()
    act = (strong.notna() & (wser > 0)).any(axis=1).rolling(fade_days, min_periods=1).sum().to_numpy()

    lo_r, op_r = cont["low"].to_numpy(), cont["open"].to_numpy()
    lo_a, hi_a, cl_a = cont["adj_low"].to_numpy(), cont["adj_high"].to_numpy(), cont["adj_close"].to_numpy()
    op_a = cont["adj_open"].to_numpy()
    fct = (cont["factor"].iloc[-1] / cont["factor"]).to_numpy()

    mask = (score >= 6) & (f["dist60"] < 0.12) & (f["netq"] < 0.6)
    if since is not None:
        mask &= dates >= since
    sig_days = list(dates[mask])

    def wcost_at(d):
        recent = ev_eff[(ev_eff["trade_date"] > d - pd.Timedelta(days=8)) & (ev_eff["trade_date"] <= d)]
        num = den = 0.0
        for mm in recent["member"].unique():
            wm = w.get(mm, 0)
            cv = costs[mm].asof(d) if len(costs[mm]) else np.nan
            if wm > 0 and cv == cv:
                num += wm * cv
                den += wm
        return num / den if den else np.nan

    trades, busy = [], -1
    for d in sig_days:
        i = pos[d]
        if i + 1 >= len(dates) or i < busy:
            continue
        wc = wcost_at(d)
        i0 = p0r = None
        if wc == wc:
            zh = wc + 5
            for j in range(i + 1, min(i + 11, len(dates))):
                if j <= busy:
                    break
                if not np.isnan(lo_r[j]) and lo_r[j] <= zh:
                    p0r = min(op_r[j], zh) if not np.isnan(op_r[j]) else zh
                    i0 = j
                    break
            if i0 is None:
                continue
        else:
            i0 = i + 1
            p0r = op_r[i0]
            if np.isnan(p0r):
                continue
        legs = [(i0, p0r * fct[i0], 1.0)]  # (进场idx, adj价, 单位)
        adds = 0
        fade_from = None
        exit_i = None
        pnl_units = 0.0
        closed_units = 0.0
        for j in range(i0, len(dates)):
            if np.isnan(lo_a[j]) or np.isnan(hi_a[j]):
                continue
            # 各批次独立止损
            still = []
            for (ei, ep, u) in legs:
                if lo_a[j] <= ep * 0.96:
                    pnl_units += u * (-0.04 - 0.001)
                    closed_units += u
                else:
                    still.append((ei, ep, u))
            legs = still
            if not legs:
                exit_i = j
                break
            if fade_from is not None and j > fade_from:
                px = op_a[j] if not np.isnan(op_a[j]) else cl_a[j]
                for (ei, ep, u) in legs:
                    pnl_units += u * (px / ep - 1 - 0.001)
                    closed_units += u
                legs = []
                exit_i = j
                break
            if fade_from is None and j > i0 + 2 and act[j] == 0:
                fade_from = j
            if pyramid and adds < 2 and j > i0 and ev_day[j]:
                base = legs[0][1]
                if cl_a[j] / base - 1 > 0.02:
                    legs.append((j + 1 if j + 1 < len(dates) else j,
                                 (op_a[j + 1] if j + 1 < len(dates) and not np.isnan(op_a[j + 1]) else cl_a[j]), 0.5))
                    adds += 1
        if legs:  # 期末持有
            exit_i = len(dates) - 1
            for (ei, ep, u) in legs:
                pnl_units += u * (cl_a[exit_i] / ep - 1)
                closed_units += u
        trades.append({"信号日": dates[pos[d]], "进场日": dates[i0], "单位": closed_units,
                       "收益%": pnl_units / closed_units * 100 if closed_units else 0,
                       "加权收益%": pnl_units * 100})
        busy = exit_i
    return pd.DataFrame(trades)


def summarize(tr, since=None):
    d = tr if since is None else tr[tr["进场日"] >= since]
    if d.empty:
        return {}
    compound = (1 + d["加权收益%"] / 100).prod() - 1  # 按1单位基准复利(加仓部分近似)
    return {"笔数": len(d), "胜率%": round((d["收益%"] > 0).mean() * 100, 1),
            "均收益%": round(d["收益%"].mean(), 2),
            "复利累计%": round(compound * 100, 1)}


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")
    seat_all = aulib.load_seat()
    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    px = price.set_index(["contract", "trade_date"]).sort_index()
    opn = px["open_price"].unstack(0)
    setl = px["settlement_price"].unstack(0)
    cont = cont.copy()
    cont["open"] = [opn.at[d, m] if m in opn.columns else np.nan for d, m in zip(mc["trade_date"], mc["main"])]
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]
    main_settle = pd.Series([setl.at[d, m] if m in setl.columns else np.nan
                             for d, m in zip(mc["trade_date"], mc["main"])], index=cont.index)

    bench, avg_val = gs_benchmark(seat_all, price)
    print(f"== 基准(2023-08~2026-08,合约价值口径,无杠杆;期间平均一手价值 {avg_val/1e4:.0f} 万) ==")
    for k, v in bench.items():
        print(f"  {k}: 每手累计收益率 ≈ {v:.0f}%")
    bh = cont.loc[cont.index >= GS_WIN, "adj_close"]
    print(f"  买入持有同期: {(bh.iloc[-1]/bh.iloc[0]-1)*100:.0f}%")

    f = features(cont, md)
    c = cont["adj_close"]
    f["dist60"] = c / c.rolling(60).min() - 1
    f["netq"] = f["netsum"].rolling(250, min_periods=120).rank(pct=True)

    ev = build_events(md, cont, CORE7)
    weights = expanding_weights(ev, range(cont.index[0].year, cont.index[-1].year + 1))
    w_t = weights[2026]
    w_profit = dict(w_t)
    w_profit["高盛期货"] = 5.0
    w_profit["中财期货"] = 5.0
    ev["dist"] = f["dist60"].reindex(ev["trade_date"]).to_numpy()
    ev_eff = ev[~(ev["member"].isin(COND_SEATS) & (ev["dist"] >= 0.05))]
    costs = {m: seat_cost_series(md, m, main_settle) for m in CORE7}
    dates = cont.index
    wser = pd.Series(w_t)

    variants = [
        ("基线 方案A", w_t, 10, False),
        ("E1 盈利权重(高盛=中财=5)", w_profit, 10, False),
        ("E2 金字塔加仓", w_t, 10, True),
        ("E3 消退15日", w_t, 15, False),
        ("E4 = E1+E2+E3", w_profit, 15, True),
    ]
    rows = []
    for name, w, fd, pyr in variants:
        tr = replay(cont, f, ev_eff, w, None, costs, fade_days=fd, pyramid=pyr)
        s_all = summarize(tr)
        s_gs = summarize(tr, since=GS_WIN)
        rows.append({"方案": name,
                     "全期笔数": s_all.get("笔数"), "全期胜率%": s_all.get("胜率%"),
                     "全期复利%": s_all.get("复利累计%"),
                     "23-08后笔数": s_gs.get("笔数"), "23-08后胜率%": s_gs.get("胜率%"),
                     "23-08后复利%": s_gs.get("复利累计%")})
    print("\n== 增强方案对比 ==")
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"\n目标线:高盛 {bench['高盛期货']:.0f}% / 中财 {bench['中财期货']:.0f}%(2023-08 后,同口径)")


if __name__ == "__main__":
    sys.exit(main())
