# -*- coding: utf-8 -*-
"""v4.4:成本口径放宽(掉榜期间成本冻结延续而非报废,运营者 2026-08-11 拍板)
+ 两方案 2026-01-01~08-01 买卖点逐笔对比。

方案A 固定参数(68%版):事件 80分位/250日阈值窗/5日事件窗(v4.2 原参数)
方案B P1 逐年选参(2026 冻结):70分位/120日阈值窗/3日事件窗
两方案共用:P0 修正(消退确认后 T+1 开盘卖出)、止损4%盘中、
  买点=score≥6+距60日低<12%+净仓<60分位、国泰/东证仅贴低点(<5%)计分、
  成本区间 [加权成本±5元] 10日有效(成本=掉榜冻结延续口径),不可得走 T+1 市价。
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_pure import features
from run_v42 import COND_SEATS, seat_cost_series
from run_optimize import CORE7

pd.set_option("display.width", 250)


def build_events_param(md, cont, members, q, window, min_hist=120):
    oi = cont["oi_total"]
    out = []
    for m in members:
        s = md[md["member"] == m].set_index("trade_date").sort_index()
        if len(s) < min_hist:
            continue
        flow = (s["dnet"] / oi.reindex(s.index)).dropna()
        thr = flow.abs().rolling(window, min_periods=min(min_hist, window)).quantile(q).shift(1)
        hit = flow[(flow.abs() >= thr) & thr.notna() & (flow > 0)]
        sub = s.loc[hit.index]
        long_dom = sub["dlong"].abs().fillna(0) >= sub["dshort"].abs().fillna(0)
        idx = sub.index[long_dom]
        if not len(idx):
            continue
        out.append(pd.DataFrame({"member": m, "trade_date": idx,
                                 "strength": (flow.loc[idx].abs() / thr.loc[idx]).clip(upper=3).to_numpy()}))
    return pd.concat(out, ignore_index=True)


def weights_2026(ev, cont, cutoff="2025-12-01"):
    """2026 年扩窗权重:截至 2025-12-01 的事件 fwd20 t 值,截断[0,5],N<30 记 0。"""
    from run_profile import forward_returns
    fwd = forward_returns(cont)
    w = {}
    for m in CORE7:
        dr = fwd.reindex(ev[(ev["member"] == m) & (ev["trade_date"] < pd.Timestamp(cutoff))]
                         ["trade_date"])[20].dropna()
        if len(dr) < 30 or dr.std(ddof=1) == 0:
            w[m] = 0.0
        else:
            w[m] = float(np.clip(dr.mean() / dr.std(ddof=1) * np.sqrt(len(dr)), 0, 5))
    return w


def replay(cont, md, f, ev, w, ewin, label):
    dates = cont.index
    pos = {d: i for i, d in enumerate(dates)}
    c = cont["adj_close"]
    dist = f["dist60"]
    ev = ev.copy()
    ev["dist"] = dist.reindex(ev["trade_date"]).to_numpy()
    ev_eff = ev[~(ev["member"].isin(COND_SEATS) & (ev["dist"] >= 0.05))]
    strong = ev_eff.pivot_table(index="trade_date", columns="member", values="strength",
                                aggfunc="max").reindex(dates).reindex(columns=CORE7)
    wser = pd.Series(w)
    score = (strong.fillna(0) * wser).rolling(ewin, min_periods=1).max().sum(axis=1)
    active10 = (strong.notna() & (wser > 0)).any(axis=1).rolling(10, min_periods=1).sum().to_numpy()

    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    px = price.set_index(["contract", "trade_date"]).sort_index()
    opn = px["open_price"].unstack(0)
    cont = cont.copy()
    cont["open"] = [opn.at[d, m] if m in opn.columns else np.nan
                    for d, m in zip(mc["trade_date"], mc["main"])]
    setl = px["settlement_price"].unstack(0)
    main_settle = pd.Series([setl.at[d, m] if m in setl.columns else np.nan
                             for d, m in zip(mc["trade_date"], mc["main"])], index=dates)
    costs = {m: seat_cost_series(md, m, main_settle) for m in CORE7}

    lo_r, op_r = cont["low"].to_numpy(), cont["open"].to_numpy()
    lo_a, hi_a, cl_a = cont["adj_low"].to_numpy(), cont["adj_high"].to_numpy(), cont["adj_close"].to_numpy()
    op_a = cont["adj_open"].to_numpy() if "adj_open" in cont else (cont["open"] / cont["factor"] * cont["factor"].iloc[-1]).to_numpy()
    fct = (cont["factor"].iloc[-1] / cont["factor"]).to_numpy()

    sig_days = list(dates[(score >= 6) & (f["dist60"] < 0.12) & (f["netq"] < 0.6)
                          & (dates >= pd.Timestamp("2025-12-01")) & (dates <= pd.Timestamp("2026-08-01"))])

    def wcost_at(d):
        recent = ev_eff[(ev_eff["trade_date"] > d - pd.Timedelta(days=ewin + 3)) & (ev_eff["trade_date"] <= d)]
        num = den = 0.0
        for m in recent["member"].unique():
            wm = w.get(m, 0)
            cv = costs[m].asof(d) if len(costs[m]) else np.nan  # 掉榜冻结延续:asof 取最后可见成本
            if wm > 0 and cv == cv:
                num += wm * cv
                den += wm
        return num / den if den else np.nan

    rows, busy = [], -1
    for d in sig_days:
        i = pos[d]
        if i + 1 >= len(dates) or i < busy:
            continue
        recent = ev_eff[(ev_eff["trade_date"] > d - pd.Timedelta(days=ewin + 3)) & (ev_eff["trade_date"] <= d)]
        seats = "、".join(f"{r.member}({r.strength:.1f})" for r in recent.itertuples())
        wc = wcost_at(d)
        # 区间进场
        i0 = p0r = None
        zone = f"{wc - 5:.0f}~{wc + 5:.0f}" if wc == wc else "市价"
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
                rows.append({"信号日": d.date(), "触发席位": seats, "买入区间": zone, "进场日": "未回踩,放弃",
                             "进场价": None, "出场日": None, "出场价": None, "结果": "放弃", "收益%": None})
                continue
        else:
            i0 = i + 1
            p0r = op_r[i0]
        p0a = p0r * fct[i0]
        stop_a = p0a * 0.96
        fade_from = None
        exit_i = exit_a = reason = None
        for j in range(i0, len(dates)):
            if np.isnan(lo_a[j]) or np.isnan(hi_a[j]):
                continue
            if fade_from is not None and j > fade_from:
                exit_a = op_a[j] if not np.isnan(op_a[j]) else cl_a[j]
                reason, exit_i = "消退T+1", j
                break
            if lo_a[j] <= stop_a:
                exit_a, reason, exit_i = stop_a, "止损-4%", j
                break
            if fade_from is None and j > i0 + 2 and active10[j] == 0:
                fade_from = j
        if exit_a is None:
            exit_i = len(dates) - 1
            exit_a, reason = cl_a[exit_i], "持有中"
        rows.append({"信号日": d.date(), "触发席位": seats, "买入区间": zone, "进场日": dates[i0].date(),
                     "进场价": round(p0r, 2), "出场日": dates[exit_i].date(),
                     "出场价": round(exit_a / fct[exit_i], 2), "结果": reason,
                     "收益%": round((exit_a / p0a - 1 - 0.001) * 100, 2)})
        busy = exit_i
    df = pd.DataFrame(rows)
    df = df[pd.to_datetime(df["进场日"], errors="coerce") >= "2026-01-01"] if len(df) else df
    print(f"\n===== {label} =====")
    print(df.to_string(index=False))
    return df


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")
    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    opn = price.set_index(["contract", "trade_date"])["open_price"].unstack(0)
    cont = cont.copy()
    cont["open"] = [opn.at[d, m] if m in opn.columns else np.nan
                    for d, m in zip(mc["trade_date"], mc["main"])]
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]

    f = features(cont, md)
    c = cont["adj_close"]
    f["dist60"] = c / c.rolling(60).min() - 1
    f["netq"] = f["netsum"].rolling(250, min_periods=120).rank(pct=True)

    evA = build_events_param(md, cont, CORE7, q=0.80, window=250)
    wA = weights_2026(evA, cont)
    replay(cont, md, f, evA, wA, ewin=5, label="方案A 固定参数(80分位/250窗/5日窗)")

    evB = build_events_param(md, cont, CORE7, q=0.70, window=120)
    wB = weights_2026(evB, cont)
    replay(cont, md, f, evB, wB, ewin=3, label="方案B P1参数(70分位/120窗/3日窗,2026冻结)")


if __name__ == "__main__":
    sys.exit(main())
