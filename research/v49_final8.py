# -*- coding: utf-8 -*-
"""最终八家双强组(运营者 2026-08-11 拍板 11→8:剔银河/永安/申万)。

组:中财、中信、海通、国泰君安、高盛、东证、华泰、国投。
门槛统一化:θ(品种,年) = 1.2 × 当年组内最大权重
  —— 黄金 2026 = 1.2×5.0 = 6.0(与现行规则一致,零改动);白银自动校准(≈4)。
其余规则 = 方案A:80分位/250窗/5日窗、距60日低<12%、组净仓<60分位、
  国泰/东证仅贴低点计分、T+1 开盘市价、止损4%、消退10日T+1。
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_pure import features
from run_v42 import COND_SEATS
from run_optimize import build_events, expanding_weights
import v46_silver

pd.set_option("display.width", 250)

GROUP8 = ["中财期货", "中信期货", "海通期货", "国泰君安", "高盛期货", "东证期货", "华泰期货", "国投期货"]
EXTRA_ALIAS = {"国投安信": "国投期货"}


def plan_a8(cont, md, label):
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
    theta = 1.2 * wmat.max(axis=1)  # 动态门槛
    act10 = (strong.notna() & (wmat > 0)).any(axis=1).rolling(10, min_periods=1).sum().to_numpy()
    pos = {d: i for i, d in enumerate(dates)}
    lo_a, hi_a, cl_a = cont["adj_low"].to_numpy(), cont["adj_high"].to_numpy(), cont["adj_close"].to_numpy()
    op_a = cont["adj_open"].to_numpy()
    op_r = cont["open"].to_numpy()
    fct = (cont["factor"].iloc[-1] / cont["factor"]).to_numpy()
    sig = (score >= theta) & (theta > 0) & (f["dist60"] < 0.12) & (f["netq"] < 0.6)
    sig_days = list(dates[sig])
    trades, busy = [], -1
    for d in sig_days:
        i = pos[d]
        if i + 1 >= len(dates) or i < busy:
            continue
        recent = ev_eff[(ev_eff["trade_date"] > d - pd.Timedelta(days=8)) & (ev_eff["trade_date"] <= d)]
        seats = "、".join(sorted(set(f"{r.member}({r.strength:.1f})" for r in recent.itertuples())))
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
                reason, exit_i = "消退", j
                break
            if lo_a[j] <= stop_a:
                exit_a, reason, exit_i = stop_a, "止损", j
                break
            if fade is None and j > i0 + 2 and act10[j] == 0:
                fade = j
        if exit_a is None:
            exit_i = len(dates) - 1
            exit_a, reason = cl_a[exit_i], "持有中"
        trades.append({"信号日": d.date(), "θ": round(float(theta[d]), 1),
                       "触发席位": seats, "进场日": dates[i0].date(), "进场价": round(p0r, 1),
                       "出场日": dates[exit_i].date(), "出场价": round(exit_a / fct[exit_i], 1),
                       "结果": reason, "收益%": round((exit_a / p0a - 1 - 0.001) * 100, 2)})
        busy = exit_i
    tr = pd.DataFrame(trades)
    done = tr[tr["结果"] != "持有中"] if len(tr) else tr
    print(f"\n===== {label} =====")
    if len(done):
        print(f"全期 {len(tr)} 笔(完结 {len(done)}):胜率 {(done['收益%']>0).mean()*100:.1f}%,"
              f"均 {done['收益%'].mean():+.2f}%,总 {done['收益%'].sum():+.1f}%,"
              f"止损 {int((done['结果']=='止损').sum())} 笔")
        t2 = tr.copy()
        t2["年"] = pd.to_datetime(t2["信号日"]).dt.year
        print(t2.groupby("年").agg(笔数=("收益%", "size"), 均收益=("收益%", "mean"),
                                  合计=("收益%", "sum")).round(2).to_string())
        t26 = tr[pd.to_datetime(tr["信号日"]) >= "2026-01-01"]
        if len(t26):
            print("\n2026 逐笔:")
            print(t26.to_string(index=False))
    return tr


def main():
    # AU
    price_au = aulib.load_price("au")
    seat_au = aulib.load_seat("au")
    seat_au["member"] = seat_au["member"].replace(EXTRA_ALIAS)
    sub = seat_au[(~seat_au["is_variety_total"]) & seat_au["rank_type"].isin(["long", "short"])]
    g = sub.pivot_table(index=["member", "trade_date"], columns="rank_type",
                        values=["quantity", "change"], aggfunc="sum")
    md_au = pd.DataFrame(index=g.index)
    for kind, name, col in [("quantity", "long", "long_q"), ("quantity", "short", "short_q"),
                            ("change", "long", "dlong"), ("change", "short", "dshort")]:
        md_au[col] = g[kind][name] if name in g[kind].columns else np.nan
    md_au["net"] = md_au["long_q"].fillna(0) - md_au["short_q"].fillna(0)
    md_au["dnet"] = md_au["dlong"].fillna(0) - md_au["dshort"].fillna(0)
    md_au = md_au.reset_index()
    cont_au = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    opn = price_au.set_index(["contract", "trade_date"])["open_price"].unstack(0)
    cont_au = cont_au.copy()
    cont_au["open"] = [opn.at[d, m] if m in opn.columns else np.nan
                       for d, m in zip(mc["trade_date"], mc["main"])]
    cont_au["adj_open"] = cont_au["open"] / cont_au["factor"] * cont_au["factor"].iloc[-1]

    tr_au = plan_a8(cont_au, md_au, "黄金 AU(八家双强组,θ动态=1.2×最大权重)")

    # AG
    _, seat_ag, cont_ag, md_ag, _ = v46_silver.prep("ag")
    md_ag["member"] = md_ag["member"].replace(EXTRA_ALIAS)
    tr_ag = plan_a8(cont_ag, md_ag, "白银 AG(八家双强组,θ动态)")

    tr_au.to_pickle(aulib.OUT / "final8_au_trades.pkl")
    tr_ag.to_pickle(aulib.OUT / "final8_ag_trades.pkl")


if __name__ == "__main__":
    sys.exit(main())
