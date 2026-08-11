# -*- coding: utf-8 -*-
"""白银 AG:方案A 参数原封不动迁移验证(运营者 2026-08-11 拍板纯方案A后要求)。

规则与黄金完全一致,零调参:
  事件 80分位/250日窗/5日事件窗;席位权重逐年扩窗(按 AG 自己的事件历史);
  买点 score>=6 + 距60日低<12% + 七席位净仓<60分位 + 国泰/东证仅贴低点计分;
  成本区间 [加权成本±5元](掉榜冻结延续),10日有效,不可得走 T+1 市价;
  止损4% 盘中;消退10日确认后 T+1 开盘卖。
产出:①七席位 AG 盯市盈亏与事件研究(有效性);②迁移回测全期+分年;③2026 轨迹;
     ④AG 专属海选 top(参考)。
注意:AG 价格乘数 15(千克/手,元/千克),仅影响盯市金额;白银日波动约为黄金 1.5-2 倍,
4% 止损的表现是迁移检验重点,如实报告不预调。
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_pure import features
from run_v42 import COND_SEATS, seat_cost_series
from run_optimize import CORE7, build_events, expanding_weights
from run_profile import forward_returns

pd.set_option("display.width", 250)
MULT_AG = 15


def prep(prefix):
    price = aulib.load_price(prefix)
    # 同日双来源去重:官方优先(2026-07-31 起 akshare_v1 与 shfe_official 并存)
    price["_pri"] = (price["source"] != "shfe_official").astype(int)
    price = (price.sort_values(["contract", "trade_date", "_pri"])
             .drop_duplicates(["contract", "trade_date"], keep="first")
             .drop(columns="_pri").reset_index(drop=True))
    seat = aulib.load_seat(prefix)
    seat = seat.drop_duplicates(["trade_date", "contract", "is_variety_total", "rank_type", "member"])
    mc = aulib.main_contract(price)
    cont = aulib.continuous_series(price, mc)
    px = price.set_index(["contract", "trade_date"]).sort_index()
    opn = px["open_price"].unstack(0)
    setl = px["settlement_price"].unstack(0)
    cont["open"] = [opn.at[d, m] if m in opn.columns else np.nan for d, m in zip(mc["trade_date"], mc["main"])]
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]
    main_settle = pd.Series([setl.at[d, m] if m in setl.columns else np.nan
                             for d, m in zip(mc["trade_date"], mc["main"])], index=cont.index)
    # member_day
    sub = seat[(~seat["is_variety_total"]) & seat["rank_type"].isin(["long", "short"])]
    g = sub.pivot_table(index=["member", "trade_date"], columns="rank_type",
                        values=["quantity", "change"], aggfunc="sum")
    md = pd.DataFrame(index=g.index)
    for kind, name, col in [("quantity", "long", "long_q"), ("quantity", "short", "short_q"),
                            ("change", "long", "dlong"), ("change", "short", "dshort")]:
        md[col] = g[kind][name] if name in g[kind].columns else np.nan
    md["net"] = md["long_q"].fillna(0) - md["short_q"].fillna(0)
    md["dnet"] = md["dlong"].fillna(0) - md["dshort"].fillna(0)
    return price, seat, cont, md.reset_index(), main_settle


def main():
    price, seat, cont, md, main_settle = prep("ag")
    dates = cont.index
    print(f"AG 数据:{dates[0].date()} ~ {dates[-1].date()},{len(dates)} 交易日,"
          f"席位 {md.member.nunique()} 家")

    # ① 七席位在 AG 上的有效性
    print("\n== 七席位 AG 盯市盈亏(全历史,榜上可见仓位,乘数15) ==")
    setl_m = price.set_index(["contract", "trade_date"])["settlement_price"].sort_index()
    fwd_diff = setl_m.groupby(level=0).diff().groupby(level=0).shift(-1).rename("fd").reset_index()
    sub = seat[(~seat["is_variety_total"]) & seat["rank_type"].isin(["long", "short"])]
    net_c = sub.pivot_table(index=["member", "contract", "trade_date"], columns="rank_type",
                            values="quantity", aggfunc="sum")
    pos_c = (net_c.get("long", 0) if "long" in net_c else 0)
    net_c = pd.DataFrame({"pos": (net_c["long"] if "long" in net_c else 0) if "short" not in net_c
                          else net_c["long"].fillna(0) - net_c["short"].fillna(0)}).reset_index()
    mm = net_c.merge(fwd_diff, on=["contract", "trade_date"], how="left")
    mm["pnl"] = mm["pos"] * mm["fd"] * MULT_AG
    rank = mm.groupby("member")["pnl"].sum().sort_values(ascending=False) / 1e8
    print("盈亏榜 top10(亿):")
    print(rank.head(10).round(1).to_string())
    print("\n七席位名次:")
    for m in CORE7:
        if m in rank.index:
            print(f"  {m}: {rank[m]:+.1f} 亿,第 {int((rank > rank[m]).sum() + 1)} 名 / {len(rank)}")

    ev = build_events(md, cont, CORE7)
    fwd = forward_returns(cont)
    print("\n== 七席位 AG 增多事件研究(h=20,全样本) ==")
    base20 = fwd[20].mean() * 100
    for m in CORE7:
        idx = ev[ev["member"] == m]["trade_date"]
        dr = fwd.reindex(idx)[20].dropna()
        if len(dr) < 20:
            print(f"  {m}: 样本 {len(dr)} 不足")
            continue
        t = dr.mean() / dr.std(ddof=1) * np.sqrt(len(dr))
        print(f"  {m}: N={len(dr)} 均值 {dr.mean()*100:+.2f}%(基线 {base20:+.2f}) 命中 {(dr>0).mean()*100:.0f}% t={t:.2f}")

    # ② 方案A 迁移回测
    f = features(cont, md)
    c = cont["adj_close"]
    f["dist60"] = c / c.rolling(60).min() - 1
    f["netq"] = f["netsum"].rolling(250, min_periods=120).rank(pct=True)
    weights = expanding_weights(ev, range(dates[0].year, dates[-1].year + 1))
    ev["dist"] = f["dist60"].reindex(ev["trade_date"]).to_numpy()
    ev_eff = ev[~(ev["member"].isin(COND_SEATS) & (ev["dist"] >= 0.05))]
    costs = {m: seat_cost_series(md, m, main_settle) for m in CORE7}

    pos = {d: i for i, d in enumerate(dates)}
    strongs = ev_eff.pivot_table(index="trade_date", columns="member", values="strength",
                                 aggfunc="max").reindex(dates).reindex(columns=CORE7)
    lo_r, op_r = cont["low"].to_numpy(), cont["open"].to_numpy()
    lo_a, hi_a, cl_a = cont["adj_low"].to_numpy(), cont["adj_high"].to_numpy(), cont["adj_close"].to_numpy()
    op_a = cont["adj_open"].to_numpy()
    fct = (cont["factor"].iloc[-1] / cont["factor"]).to_numpy()

    # 逐年权重的 score 与 active
    wmat = pd.DataFrame({m: [weights[d.year].get(m, 0.0) for d in dates] for m in CORE7}, index=dates)
    score = (strongs.fillna(0) * wmat).rolling(5, min_periods=1).max().sum(axis=1)
    act10 = (strongs.notna() & (wmat > 0)).any(axis=1).rolling(10, min_periods=1).sum().to_numpy()
    sig_days = list(dates[(score >= 6) & (f["dist60"] < 0.12) & (f["netq"] < 0.6)])

    def wcost_at(d):
        recent = ev_eff[(ev_eff["trade_date"] > d - pd.Timedelta(days=8)) & (ev_eff["trade_date"] <= d)]
        num = den = 0.0
        for m2 in recent["member"].unique():
            w2 = weights[d.year].get(m2, 0)
            cv = costs[m2].asof(d) if len(costs[m2]) else np.nan
            if w2 > 0 and cv == cv:
                num += w2 * cv
                den += w2
        return num / den if den else np.nan

    trades, busy = [], -1
    for d in sig_days:
        i = pos[d]
        if i + 1 >= len(dates) or i < busy:
            continue
        recent = ev_eff[(ev_eff["trade_date"] > d - pd.Timedelta(days=8)) & (ev_eff["trade_date"] <= d)]
        seats = "、".join(sorted(set(f"{r.member}({r.strength:.1f})" for r in recent.itertuples())))
        wc = wcost_at(d)
        i0 = p0r = None
        zone = "市价"
        if wc == wc:
            zh = wc + 5 / MULT_AG * 15  # 白银±5元/千克与黄金±5元/克同为一手±5000元?按价格±5元原样
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
                continue
        else:
            i0 = i + 1
            p0r = op_r[i0]
            if np.isnan(p0r):
                continue
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
            if fade_from is None and j > i0 + 2 and act10[j] == 0:
                fade_from = j
        if exit_a is None:
            exit_i = len(dates) - 1
            exit_a, reason = cl_a[exit_i], "持有中"
        trades.append({"信号日": d.date(), "触发席位": seats, "区间": zone,
                       "进场日": dates[i0].date(), "进场价": round(p0r, 0),
                       "出场日": dates[exit_i].date(), "出场价": round(exit_a / fct[exit_i], 0),
                       "结果": reason, "收益%": round((exit_a / p0a - 1 - 0.001) * 100, 2)})
        busy = exit_i
    tr = pd.DataFrame(trades)
    done = tr[tr["结果"] != "持有中"]
    print(f"\n== 方案A 迁移回测(AG,零调参) ==")
    print(f"全期 {len(tr)} 笔(完结 {len(done)}):胜率 {(done['收益%']>0).mean()*100:.1f}%,"
          f"均收益 {done['收益%'].mean():.2f}%,总 {done['收益%'].sum():.1f}%,"
          f"止损 {int((done['结果']=='止损-4%').sum())} 笔")
    tr2 = tr.copy()
    tr2["年"] = pd.to_datetime(tr2["信号日"]).dt.year
    print(tr2.groupby("年").agg(笔数=("收益%", "size"), 均收益=("收益%", "mean")).round(2).to_string())
    print("\n== 2026 年逐笔 ==")
    print(tr[pd.to_datetime(tr["信号日"]) >= "2026-01-01"].to_string(index=False))
    tr.to_pickle(aulib.OUT / "ag_planA_trades.pkl")

    # ④ AG 专属海选(参考)
    print("\n== AG 全席位增多事件 t 值 top12(全样本,参考) ==")
    rows = []
    for m, s in md.groupby("member"):
        s = s.set_index("trade_date").sort_index()
        if len(s) < 120:
            continue
        oi = cont["oi_total"]
        flow = (s["dnet"] / oi.reindex(s.index)).dropna()
        thr = flow.abs().rolling(250, min_periods=120).quantile(0.80).shift(1)
        hit = flow[(flow.abs() >= thr) & thr.notna() & (flow > 0)]
        ss = s.loc[hit.index]
        idx = ss.index[ss["dlong"].abs().fillna(0) >= ss["dshort"].abs().fillna(0)]
        dr = fwd.reindex(idx)[20].dropna()
        if len(dr) < 40:
            continue
        rows.append({"席位": m, "N": len(dr), "均值%": dr.mean() * 100,
                     "t": dr.mean() / dr.std(ddof=1) * np.sqrt(len(dr))})
    print(pd.DataFrame(rows).sort_values("t", ascending=False).head(12).round(2).to_string(index=False))


if __name__ == "__main__":
    sys.exit(main())
