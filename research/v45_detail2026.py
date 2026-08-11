# -*- coding: utf-8 -*-
"""方案A + E2金字塔:2026-01-01~08-01 买卖点逐腿明细。"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_pure import features
from run_v42 import COND_SEATS, seat_cost_series
from run_optimize import CORE7, build_events, expanding_weights

pd.set_option("display.width", 250)


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")
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

    f = features(cont, md)
    c = cont["adj_close"]
    f["dist60"] = c / c.rolling(60).min() - 1
    f["netq"] = f["netsum"].rolling(250, min_periods=120).rank(pct=True)

    ev = build_events(md, cont, CORE7)
    weights = expanding_weights(ev, range(cont.index[0].year, cont.index[-1].year + 1))
    w = weights[2026]
    ev["dist"] = f["dist60"].reindex(ev["trade_date"]).to_numpy()
    ev_eff = ev[~(ev["member"].isin(COND_SEATS) & (ev["dist"] >= 0.05))]
    costs = {m: seat_cost_series(md, m, main_settle) for m in CORE7}

    dates = cont.index
    pos = {d: i for i, d in enumerate(dates)}
    wser = pd.Series(w)
    strong = ev_eff.pivot_table(index="trade_date", columns="member", values="strength",
                                aggfunc="max").reindex(dates).reindex(columns=CORE7)
    score = (strong.fillna(0) * wser).rolling(5, min_periods=1).max().sum(axis=1)
    ev_day_members = {d: g for d, g in ev_eff.groupby("trade_date")}
    act10 = (strong.notna() & (wser > 0)).any(axis=1).rolling(10, min_periods=1).sum().to_numpy()

    lo_r, op_r = cont["low"].to_numpy(), cont["open"].to_numpy()
    lo_a, hi_a, cl_a = cont["adj_low"].to_numpy(), cont["adj_high"].to_numpy(), cont["adj_close"].to_numpy()
    op_a = cont["adj_open"].to_numpy()
    fct = (cont["factor"].iloc[-1] / cont["factor"]).to_numpy()

    sig_days = list(dates[(score >= 6) & (f["dist60"] < 0.12) & (f["netq"] < 0.6)
                          & (dates >= pd.Timestamp("2025-12-01")) & (dates <= pd.Timestamp("2026-08-01"))])

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

    busy = -1
    for d in sig_days:
        i = pos[d]
        if i + 1 >= len(dates) or i < busy:
            continue
        recent = ev_eff[(ev_eff["trade_date"] > d - pd.Timedelta(days=8)) & (ev_eff["trade_date"] <= d)]
        seats = "、".join(sorted(set(f"{r.member}({r.strength:.1f})" for r in recent.itertuples())))
        wc = wcost_at(d)
        i0 = p0r = None
        if wc == wc:
            zh = wc + 5
            zone = f"{wc-5:.0f}~{wc+5:.0f}"
            for j in range(i + 1, min(i + 11, len(dates))):
                if j <= busy:
                    break
                if not np.isnan(lo_r[j]) and lo_r[j] <= zh:
                    p0r = min(op_r[j], zh) if not np.isnan(op_r[j]) else zh
                    i0 = j
                    break
            if i0 is None:
                if d >= pd.Timestamp("2026-01-01"):
                    print(f"信号 {d.date()}(score {score[d]:.1f})触发:{seats} 区间{zone} → 10日未回踩,放弃\n")
                continue
        else:
            i0 = i + 1
            p0r = op_r[i0]
            zone = "市价"
        p0a = p0r * fct[i0]
        legs = [{"tag": "主仓", "i": i0, "pa": p0a, "pr": p0r, "u": 1.0, "src": seats}]
        adds = 0
        fade_from = None
        exit_i = None
        events_log = []
        j = i0
        for j in range(i0, len(dates)):
            if np.isnan(lo_a[j]) or np.isnan(hi_a[j]):
                continue
            still = []
            for leg in legs:
                if leg.get("closed"):
                    still.append(leg)
                    continue
                if lo_a[j] <= leg["pa"] * 0.96:
                    leg["closed"] = ("止损-4%", dates[j], leg["pa"] * 0.96 / fct[j], -4.1)
                still.append(leg)
            legs = still
            if all(l.get("closed") for l in legs):
                exit_i = j
                break
            if fade_from is not None and j > fade_from:
                pxa = op_a[j] if not np.isnan(op_a[j]) else cl_a[j]
                for leg in legs:
                    if not leg.get("closed"):
                        leg["closed"] = ("消退T+1", dates[j], pxa / fct[j],
                                         (pxa / leg["pa"] - 1 - 0.001) * 100)
                exit_i = j
                break
            if fade_from is None and j > i0 + 2 and act10[j] == 0:
                fade_from = j
            if adds < 2 and j > i0 and dates[j] in ev_day_members:
                g = ev_day_members[dates[j]]
                names = "、".join(f"{r.member}({r.strength:.1f})" for r in g.itertuples())
                if cl_a[j] / legs[0]["pa"] - 1 > 0.02 and j + 1 < len(dates):
                    pa = op_a[j + 1] if not np.isnan(op_a[j + 1]) else cl_a[j]
                    legs.append({"tag": f"加仓{adds+1}", "i": j + 1, "pa": pa, "pr": pa / fct[min(j+1, len(dates)-1)],
                                 "u": 0.5, "src": names})
                    adds += 1
        open_legs = [l for l in legs if not l.get("closed")]
        if open_legs:
            exit_i = len(dates) - 1
            for leg in open_legs:
                leg["closed"] = ("持有中", dates[exit_i], cl_a[exit_i] / fct[exit_i],
                                 (cl_a[exit_i] / leg["pa"] - 1) * 100)
        busy = exit_i
        if dates[i0] < pd.Timestamp("2026-01-01"):
            continue
        print(f"■ 信号日 {d.date()}(score {score[d]:.1f})  买入区间 {zone}")
        tot_u = tot_p = 0.0
        for leg in legs:
            rs, xd, xp, ret = leg["closed"]
            print(f"   {leg['tag']}({leg['u']}单位) {dates[leg['i']].date()} 买 @ {leg['pr']:.2f}"
                  f"  ← {leg['src']}")
            print(f"      → {xd.date()} {rs} @ {xp:.2f}  收益 {ret:+.2f}%")
            tot_u += leg["u"]
            tot_p += leg["u"] * ret
        print(f"   本轮合计(按{tot_u:.1f}单位加权): {tot_p/tot_u:+.2f}%\n")


if __name__ == "__main__":
    sys.exit(main())
