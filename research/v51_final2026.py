# -*- coding: utf-8 -*-
"""最终版规则引擎(2026-08-11 运营者拍板):金银统一硬规则 + 四条联动参考标记。

硬规则(决定买卖):
  八家组(中财/中信/海通/国泰/高盛/东证/华泰/国投);事件 80分位/250窗/5日窗;
  权重逐年扩窗;θ=1.2×当年组内最大权重;
  买点=score≥θ + 距60日低<12% + 组净仓<60分位 + 国泰/东证仅贴低点计分;
  进场=机构加权成本±5元区间限价(掉榜冻结延续),10日有效,不可得走T+1开盘市价;
  卖出=止损4%盘中 / 八家连续10日零增多事件确认后T+1开盘。
参考标记(只展示不改变交易):
  ①金银共振:信号日近5日另一品种是否有八家增多事件
  ②比价腿警示:触发席位含国泰/华泰/海通且其另一品种同期增空
  ③高盛金银组合★:高盛本品种增多+另一品种增空同窗(历史+5.00%/命中91%)
  ④金银比状态:250日z(z>2金极贵→若双信号优先银;z<-2银极贵)
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_pure import features
from run_v42 import COND_SEATS, seat_cost_series
from run_optimize import build_events, expanding_weights
import v46_silver
from v50_goldsilver import member_day_au, neg_events, EXTRA_ALIAS

pd.set_option("display.width", 250)
GROUP8 = ["中财期货", "中信期货", "海通期货", "国泰君安", "高盛期货", "东证期货", "华泰期货", "国投期货"]
SPREAD_SEATS = {"国泰君安", "华泰期货", "海通期货"}


def prep_market(kind):
    if kind == "au":
        price = aulib.load_price("au")
        seat = aulib.load_seat("au")
        seat["member"] = seat["member"].replace(EXTRA_ALIAS)
        md = member_day_au()
        cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
        mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
        px = price.set_index(["contract", "trade_date"]).sort_index()
        opn = px["open_price"].unstack(0)
        setl = px["settlement_price"].unstack(0)
        cont = cont.copy()
        cont["open"] = [opn.at[d, m] if m in opn.columns else np.nan
                        for d, m in zip(mc["trade_date"], mc["main"])]
        cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]
        ms = pd.Series([setl.at[d, m] if m in setl.columns else np.nan
                        for d, m in zip(mc["trade_date"], mc["main"])], index=cont.index)
    else:
        price, seat, cont, md, ms = v46_silver.prep("ag")
        md["member"] = md["member"].replace(EXTRA_ALIAS)
    return cont, md, ms


def engine(cont, md, ms, other_ev_long, other_ev_short, ratio_z, label):
    dates = cont.index
    ev = build_events(md, cont, GROUP8)
    weights = expanding_weights(ev, range(dates[0].year, dates[-1].year + 1))
    f = features(cont, md)
    c = cont["adj_close"]
    f["dist60"] = c / c.rolling(60).min() - 1
    sub = md[md["member"].isin(GROUP8)]
    f["netq"] = (sub.groupby("trade_date")["net"].sum().reindex(dates).ffill(limit=10)
                 .rolling(250, min_periods=120).rank(pct=True))
    ev["dist"] = f["dist60"].reindex(ev["trade_date"]).to_numpy()
    ev_eff = ev[~(ev["member"].isin(COND_SEATS) & (ev["dist"] >= 0.05))]
    strong = ev_eff.pivot_table(index="trade_date", columns="member", values="strength",
                                aggfunc="max").reindex(dates).reindex(columns=GROUP8)
    wmat = pd.DataFrame({m: [weights[d.year].get(m, 0.0) for d in dates] for m in GROUP8}, index=dates)
    score = (strong.fillna(0) * wmat).rolling(5, min_periods=1).max().sum(axis=1)
    theta = 1.2 * wmat.max(axis=1)
    act10 = (strong.notna() & (wmat > 0)).any(axis=1).rolling(10, min_periods=1).sum().to_numpy()
    costs = {m: seat_cost_series(md, m, ms) for m in GROUP8}
    # 本品种席位的另一品种增空(比价腿判定用)
    pos = {d: i for i, d in enumerate(dates)}
    lo_r, op_r = cont["low"].to_numpy(), cont["open"].to_numpy()
    lo_a, hi_a, cl_a = cont["adj_low"].to_numpy(), cont["adj_high"].to_numpy(), cont["adj_close"].to_numpy()
    op_a = cont["adj_open"].to_numpy()
    fct = (cont["factor"].iloc[-1] / cont["factor"]).to_numpy()
    sig = (score >= theta) & (theta > 0) & (f["dist60"] < 0.12) & (f["netq"] < 0.6)
    sig_days = list(dates[sig & (dates >= pd.Timestamp("2025-12-01"))])

    def marks(d, trig_members):
        w5 = pd.Timedelta(days=8)
        m1 = "✓" if len(other_ev_long[(other_ev_long["trade_date"] > d - w5)
                                      & (other_ev_long["trade_date"] <= d)]) else "—"
        spread = [m for m in trig_members if m in SPREAD_SEATS
                  and len(other_ev_short[(other_ev_short["member"] == m)
                                         & (other_ev_short["trade_date"] > d - w5)
                                         & (other_ev_short["trade_date"] <= d)])]
        m2 = "⚠" + "/".join(s[:2] for s in spread) if spread else "—"
        gs = ("★" if ("高盛期货" in trig_members
                      and len(other_ev_short[(other_ev_short["member"] == "高盛期货")
                                             & (other_ev_short["trade_date"] > d - w5)
                                             & (other_ev_short["trade_date"] <= d)])) else "—")
        zv = ratio_z.asof(d) if d >= ratio_z.index[0] else np.nan
        m4 = f"{zv:+.1f}" + ("(金极贵,优先银)" if zv > 2 else "(银极贵)" if zv < -2 else "")
        return m1, m2, gs, m4

    rows, busy = [], -1
    for d in sig_days:
        i = pos[d]
        if i + 1 >= len(dates) or i < busy:
            continue
        recent = ev_eff[(ev_eff["trade_date"] > d - pd.Timedelta(days=8)) & (ev_eff["trade_date"] <= d)]
        trig = sorted(set(recent["member"]))
        seats = "、".join(f"{r.member}({r.strength:.1f})" for r in recent.itertuples())
        num = den = 0.0
        for m2 in trig:
            w2 = weights[d.year].get(m2, 0)
            cv = costs[m2].asof(d) if len(costs[m2]) else np.nan
            if w2 > 0 and cv == cv:
                num += w2 * cv
                den += w2
        wc = num / den if den else np.nan
        i0 = p0r = None
        zone = "市价"
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
                m1, m2_, gs, m4 = marks(d, trig)
                rows.append({"信号日": d.date(), "触发": seats, "区间": zone, "进场": "未回踩放弃",
                             "银金共振": m1, "比价腿": m2_, "高盛组合": gs, "金银比z": m4})
                continue
        else:
            i0 = i + 1
            p0r = op_r[i0]
            if np.isnan(p0r):
                continue
        p0a = p0r * fct[i0]
        stop_a = p0a * 0.96
        fade = None
        exit_i = exit_a = reason = None
        for j in range(i0, len(dates)):
            if np.isnan(lo_a[j]) or np.isnan(hi_a[j]):
                continue
            if fade is not None and j > fade:
                exit_a = op_a[j] if not np.isnan(op_a[j]) else cl_a[j]
                reason, exit_i = "消退T+1", j
                break
            if lo_a[j] <= stop_a:
                exit_a, reason, exit_i = stop_a, "止损4%", j
                break
            if fade is None and j > i0 + 2 and act10[j] == 0:
                fade = j
        if exit_a is None:
            exit_i = len(dates) - 1
            exit_a, reason = cl_a[exit_i], "持有中"
        m1, m2_, gs, m4 = marks(d, trig)
        rows.append({"信号日": d.date(), "触发": seats, "区间": zone,
                     "进场": f"{dates[i0].date()} @ {p0r:.0f}",
                     "出场": f"{dates[exit_i].date()} @ {exit_a/fct[exit_i]:.0f}",
                     "结果": reason, "收益%": round((exit_a / p0a - 1 - 0.001) * 100, 2),
                     "银金共振": m1, "比价腿": m2_, "高盛组合": gs, "金银比z": m4})
        busy = exit_i
    df = pd.DataFrame(rows)
    df = df[pd.to_datetime(df["信号日"].astype(str)) >= "2025-12-15"]
    print(f"\n===== {label} 2026 买卖点(含参考标记) =====")
    print(df.to_string(index=False))
    return df


def main():
    cont_au, md_au, ms_au = prep_market("au")
    cont_ag, md_ag, ms_ag = prep_market("ag")
    common = cont_au.index.intersection(cont_ag.index)
    ratio = (cont_au.loc[common, "adj_close"] / cont_ag.loc[common, "adj_close"])
    ratio_z = ((ratio - ratio.rolling(250).mean()) / ratio.rolling(250).std()).rename("z")

    ev_au_long = build_events(md_au, cont_au, GROUP8)
    ev_ag_long = build_events(md_ag, cont_ag, GROUP8)
    ev_au_short = neg_events(md_au, cont_au, GROUP8)
    ev_ag_short = neg_events(md_ag, cont_ag, GROUP8)

    au = engine(cont_au, md_au, ms_au, ev_ag_long, ev_ag_short, ratio_z, "黄金 AU")
    ag = engine(cont_ag, md_ag, ms_ag, ev_au_long, ev_au_short, ratio_z, "白银 AG")
    au.to_pickle(aulib.OUT / "final_au_2026.pkl")
    ag.to_pickle(aulib.OUT / "final_ag_2026.pkl")


if __name__ == "__main__":
    sys.exit(main())
