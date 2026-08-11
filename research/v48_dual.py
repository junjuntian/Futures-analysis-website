# -*- coding: utf-8 -*-
"""金银双强席位筛选(运营者 2026-08-11:两个品种都猛赚才是真机构,单品种全部剔除)。

四维交叉:AU 盯市盈亏 / AU 增多事件 t / AG 盯市盈亏 / AG 增多事件 t。
双强标准:两品种盈亏均为正且进各自前30 + 两品种事件 t 均 >=1.5(N>=30)。
然后:新信号组分别在 AU、AG 重跑方案A(权重按各品种自身扩窗),与旧组对比。
别名补充:国投安信(~2023)→ 国投期货。
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_pure import features
from run_v42 import COND_SEATS, seat_cost_series
from run_optimize import build_events, expanding_weights
from run_profile import forward_returns
import run_optimize
import v46_silver

pd.set_option("display.width", 250)
EXTRA_ALIAS = {"国投安信": "国投期货"}


def member_day(seat):
    sub = seat[(~seat["is_variety_total"]) & seat["rank_type"].isin(["long", "short"])]
    g = sub.pivot_table(index=["member", "trade_date"], columns="rank_type",
                        values=["quantity", "change"], aggfunc="sum")
    md = pd.DataFrame(index=g.index)
    for kind, name, col in [("quantity", "long", "long_q"), ("quantity", "short", "short_q"),
                            ("change", "long", "dlong"), ("change", "short", "dshort")]:
        md[col] = g[kind][name] if name in g[kind].columns else np.nan
    md["net"] = md["long_q"].fillna(0) - md["short_q"].fillna(0)
    md["dnet"] = md["dlong"].fillna(0) - md["dshort"].fillna(0)
    return md.reset_index()


def pnl_rank(seat, price, mult):
    setl = price.set_index(["contract", "trade_date"])["settlement_price"].sort_index()
    fd = setl.groupby(level=0).diff().groupby(level=0).shift(-1).rename("fd").reset_index()
    sub = seat[(~seat["is_variety_total"]) & seat["rank_type"].isin(["long", "short"])]
    nc = sub.pivot_table(index=["member", "contract", "trade_date"], columns="rank_type",
                         values="quantity", aggfunc="sum")
    nc = pd.DataFrame({"pos": nc.get("long", pd.Series(dtype=float)).fillna(0)
                       - nc.get("short", pd.Series(dtype=float)).fillna(0)}).reset_index()
    m = nc.merge(fd, on=["contract", "trade_date"], how="left")
    m["pnl"] = m["pos"] * m["fd"] * mult
    return m.groupby("member")["pnl"].sum() / 1e8


def event_t(md, cont):
    fwd = forward_returns(cont)
    oi = cont["oi_total"]
    rows = {}
    for m, s in md.groupby("member"):
        s = s.set_index("trade_date").sort_index()
        if len(s) < 120:
            continue
        flow = (s["dnet"] / oi.reindex(s.index)).dropna()
        thr = flow.abs().rolling(250, min_periods=120).quantile(0.80).shift(1)
        hit = flow[(flow.abs() >= thr) & thr.notna() & (flow > 0)]
        ss = s.loc[hit.index]
        idx = ss.index[ss["dlong"].abs().fillna(0) >= ss["dshort"].abs().fillna(0)]
        dr = fwd.reindex(idx)[20].dropna()
        if len(dr) < 30 or dr.std(ddof=1) == 0:
            continue
        rows[m] = {"N": len(dr), "mean": dr.mean() * 100,
                   "t": dr.mean() / dr.std(ddof=1) * np.sqrt(len(dr))}
    return pd.DataFrame(rows).T


def plan_a(cont, md, ev, group, label, since2026=False):
    """方案A 回测(通用品种,信号组=group)。"""
    dates = cont.index
    f = features(cont, md)
    c = cont["adj_close"]
    f["dist60"] = c / c.rolling(60).min() - 1
    sub = md[md["member"].isin(group)]
    f2net = sub.groupby("trade_date")["net"].sum().reindex(dates).ffill(limit=10)
    f["netq"] = f2net.rolling(250, min_periods=120).rank(pct=True)
    ev = ev[ev["member"].isin(group)].copy()
    weights = {y: {m: w for m, w in ws.items() if m in group}
               for y, ws in expanding_weights(ev, range(dates[0].year, dates[-1].year + 1)).items()}
    ev["dist"] = f["dist60"].reindex(ev["trade_date"]).to_numpy()
    ev_eff = ev[~(ev["member"].isin(COND_SEATS) & (ev["dist"] >= 0.05))]
    strong = ev_eff.pivot_table(index="trade_date", columns="member", values="strength",
                                aggfunc="max").reindex(dates).reindex(columns=group)
    wmat = pd.DataFrame({m: [weights[d.year].get(m, 0.0) for d in dates] for m in group}, index=dates)
    score = (strong.fillna(0) * wmat).rolling(5, min_periods=1).max().sum(axis=1)
    act10 = (strong.notna() & (wmat > 0)).any(axis=1).rolling(10, min_periods=1).sum().to_numpy()
    pos = {d: i for i, d in enumerate(dates)}
    lo_r, op_r = cont["low"].to_numpy(), cont["open"].to_numpy()
    lo_a, hi_a, cl_a = cont["adj_low"].to_numpy(), cont["adj_high"].to_numpy(), cont["adj_close"].to_numpy()
    op_a = cont["adj_open"].to_numpy()
    fct = (cont["factor"].iloc[-1] / cont["factor"]).to_numpy()
    sig_days = list(dates[(score >= 6) & (f["dist60"] < 0.12) & (f["netq"] < 0.6)])
    trades, busy = [], -1
    for d in sig_days:
        i = pos[d]
        if i + 1 >= len(dates) or i < busy:
            continue
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
        trades.append({"信号日": d.date(), "进场价": round(p0r, 1), "出场日": dates[exit_i].date(),
                       "结果": reason, "收益%": round((exit_a / p0a - 1 - 0.001) * 100, 2)})
        busy = exit_i
    tr = pd.DataFrame(trades)
    done = tr[tr["结果"] != "持有中"] if len(tr) else tr
    if len(done):
        print(f"[{label}] {len(tr)}笔(完结{len(done)}):胜率 {(done['收益%']>0).mean()*100:.1f}%,"
              f"均 {done['收益%'].mean():+.2f}%,总 {done['收益%'].sum():+.1f}%,"
              f"止损 {int((done['结果']=='止损').sum())} 笔")
    else:
        print(f"[{label}] 无完结交易({len(tr)}笔)")
    if since2026 and len(tr):
        t26 = tr[pd.to_datetime(tr["信号日"]) >= "2026-01-01"]
        if len(t26):
            print(t26.to_string(index=False))
    return tr


def main():
    # AU
    price_au = aulib.load_price("au")
    seat_au = aulib.load_seat("au")
    seat_au["member"] = seat_au["member"].replace(EXTRA_ALIAS)
    cont_au = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    price_tmp = aulib.load_price("au")
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    opn = price_tmp.set_index(["contract", "trade_date"])["open_price"].unstack(0)
    cont_au = cont_au.copy()
    cont_au["open"] = [opn.at[d, m] if m in opn.columns else np.nan
                       for d, m in zip(mc["trade_date"], mc["main"])]
    cont_au["adj_open"] = cont_au["open"] / cont_au["factor"] * cont_au["factor"].iloc[-1]
    md_au = member_day(seat_au)

    # AG
    price_ag, seat_ag, cont_ag, md_ag, _ = v46_silver.prep("ag")
    seat_ag["member"] = seat_ag["member"].replace(EXTRA_ALIAS)
    md_ag["member"] = md_ag["member"].replace(EXTRA_ALIAS)

    pnl_au = pnl_rank(seat_au, price_au, 1000)
    pnl_ag = pnl_rank(seat_ag, price_ag, 15)
    t_au = event_t(md_au, cont_au)
    t_ag = event_t(md_ag, cont_ag)

    x = pd.DataFrame({"AU盈亏亿": pnl_au, "AG盈亏亿": pnl_ag,
                      "AU_t": t_au["t"], "AU_N": t_au["N"],
                      "AG_t": t_ag["t"], "AG_N": t_ag["N"]})
    x["AU盈亏名次"] = pnl_au.rank(ascending=False)
    x["AG盈亏名次"] = pnl_ag.rank(ascending=False)
    dual = x[(x["AU盈亏亿"] > 0) & (x["AG盈亏亿"] > 0)
             & (x["AU盈亏名次"] <= 30) & (x["AG盈亏名次"] <= 30)
             & (x["AU_t"] >= 1.5) & (x["AG_t"] >= 1.5)].sort_values("AU_t", ascending=False)
    print("== 金银双强名单(盈亏双正且双前30 + 事件t双>=1.5) ==")
    print(dual.round(2).to_string())

    print("\n== 边缘落选(供运营者裁定;差一项达标) ==")
    near = x[(x["AU盈亏名次"] <= 30) & (x["AG盈亏名次"] <= 30)
             & (x["AU盈亏亿"] > 0) & (x["AG盈亏亿"] > 0)
             & ~x.index.isin(dual.index)].sort_values("AU盈亏亿", ascending=False)
    print(near.head(8).round(2).to_string())

    group = list(dual.index)
    print(f"\n新信号组({len(group)}家): {'、'.join(group)}")

    ev_au = build_events(md_au, cont_au, group)
    ev_ag = build_events(md_ag, cont_ag, group)
    print("\n== 新组 方案A 回测 ==")
    tr_au = plan_a(cont_au, md_au, ev_au, group, "AU 双强组", since2026=True)
    tr_ag = plan_a(cont_ag, md_ag, ev_ag, group, "AG 双强组", since2026=True)
    tr_au.to_pickle(aulib.OUT / "dual_au_trades.pkl")
    tr_ag.to_pickle(aulib.OUT / "dual_ag_trades.pkl")
    x.to_pickle(aulib.OUT / "dual_cross_table.pkl")


if __name__ == "__main__":
    sys.exit(main())
