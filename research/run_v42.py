# -*- coding: utf-8 -*-
"""v4.2:运营者 2026-08-11 四条反馈落地。

1. 止损 3% → 4%(运营者从 6-11/7-02 实例拍板)
2. 国泰君安/东证 条件降权:其增多事件仅在价格贴 60 日低点(<5%)时计分
   (诊断依据:国泰高位事件 +0.85%≈基线;东证两层均弱;其余五家不受限)
3. 买点改价格区间:锚定触发席位的本轮建仓成本(结算价加权累积,减仓不改均价,
   翻向重置——与 SEAT_AND_SPREAD_REQUIREMENTS 2.2 同口径,品种级主力结算价近似),
   区间 = [加权成本-5, 加权成本+5](元,真实价);信号日后 10 个交易日内
   最低价触及区间上沿即按 min(开盘, 上沿) 成交;不触及则放弃该信号。
4. 执行时序不变:15:00 收盘后出席位数据 → 信号 → 次日起执行(买卖同)。
卖出:七席位连续 10 日零增多事件(收盘)或 -4% 止损。
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_pure import features
from run_optimize import CORE7, build_events, expanding_weights

pd.set_option("display.width", 220)

COND_SEATS = {"国泰君安", "东证期货"}  # 仅贴低点计分
STOP = 0.04
ZONE = 5.0  # 区间半宽(元)


def seat_cost_series(md, member, settle):
    """多头建仓成本(品种级近似):增仓按当日结算价加权,减仓不变,净空/掉出重置。"""
    s = md[md["member"] == member].set_index("trade_date").sort_index()
    cost, net_prev = np.nan, 0.0
    out = {}
    for d, row in s.iterrows():
        net, dnet = row["net"], row["dnet"]
        px = settle.get(d, np.nan)
        if not np.isnan(px):
            if net > 0 and net_prev <= 0:
                cost = px
            elif net > 0 and dnet > 0 and not np.isnan(cost):
                cost = (cost * (net - dnet) + px * dnet) / net
            if net <= 0:
                cost = np.nan
        out[d] = cost
        net_prev = net
    return pd.Series(out)


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
    dates = cont.index
    dist_np = f["dist60"]

    # ===== 条件计分的加权分数(国泰/东证仅贴低点计分) =====
    ev = build_events(md, cont, CORE7)
    weights = expanding_weights(ev, range(dates[0].year, dates[-1].year + 1))
    ev["dist"] = dist_np.reindex(ev["trade_date"]).to_numpy()
    ev_eff = ev[~(ev["member"].isin(COND_SEATS) & (ev["dist"] >= 0.05))]
    strong = ev_eff.pivot_table(index="trade_date", columns="member", values="strength", aggfunc="max")
    strong = strong.reindex(dates).reindex(columns=CORE7)
    wmat = pd.DataFrame({m: [weights[d.year].get(m, 0.0) for d in dates] for m in CORE7}, index=dates)
    score = (strong.fillna(0) * wmat).rolling(5, min_periods=1).max().sum(axis=1)
    active10 = (strong.notna() & (wmat > 0)).any(axis=1).rolling(10, min_periods=1).sum()

    # ===== 成本引擎 =====
    costs = {m: seat_cost_series(md, m, main_settle) for m in CORE7}
    cost_adj = {m: (costs[m].reindex(dates) / cont["factor"] * cont["factor"].iloc[-1]) for m in CORE7}

    sig_days = list(dates[(score >= 6) & (f["dist60"] < 0.12) & (f["netq"] < 0.6)])

    pos = {d: i for i, d in enumerate(dates)}
    lo_r, op_r = cont["low"].to_numpy(), cont["open"].to_numpy()
    lo_a, hi_a, cl_a = cont["adj_low"].to_numpy(), cont["adj_high"].to_numpy(), cont["adj_close"].to_numpy()
    op_a = cont["adj_open"].to_numpy()
    fct = (cont["factor"].iloc[-1] / cont["factor"]).to_numpy()
    ev10 = active10.to_numpy()

    def wcost_at(d):
        """触发席位(近5日有效事件)的权重加权建仓成本(真实价)。"""
        recent = ev_eff[(ev_eff["trade_date"] > d - pd.Timedelta(days=8)) & (ev_eff["trade_date"] <= d)]
        num = den = 0.0
        for m in recent["member"].unique():
            w = weights[d.year].get(m, 0)
            cv = costs[m].get(d, np.nan)
            if w > 0 and cv == cv:
                num += w * cv
                den += w
        return num / den if den else np.nan

    def run(entry_mode, stop):
        trades, busy = [], -1
        missed = 0
        for d in sig_days:
            i = pos[d]
            if i + 1 >= len(dates) or i < busy:
                continue
            if entry_mode == "market":
                i0 = i + 1
                p0a = op_a[i0] if not np.isnan(op_a[i0]) else cl_a[i0]
                p0r = op_r[i0] if not np.isnan(op_r[i0]) else np.nan
                zh = np.nan
            else:
                wc = wcost_at(d)
                if np.isnan(wc):
                    i0 = i + 1
                    p0a = op_a[i0] if not np.isnan(op_a[i0]) else cl_a[i0]
                    p0r, zh = np.nan, np.nan
                else:
                    zh = wc + ZONE
                    i0 = p0a = None
                    for j in range(i + 1, min(i + 11, len(dates))):
                        if j <= busy:
                            break
                        if not np.isnan(lo_r[j]) and lo_r[j] <= zh:
                            p0r = min(op_r[j], zh) if not np.isnan(op_r[j]) else zh
                            p0a = p0r * fct[j]
                            i0 = j
                            break
                    if i0 is None:
                        missed += 1
                        continue
            if p0a is None or np.isnan(p0a):
                continue
            stop_px = p0a * (1 - stop)
            exit_px = reason = None
            exit_i = None
            for j in range(i0, len(dates)):
                if np.isnan(lo_a[j]) or np.isnan(hi_a[j]):
                    continue
                if lo_a[j] <= stop_px:
                    exit_px, reason, exit_i = stop_px, "止损", j
                    break
                if j > i0 + 2 and ev10[j] == 0:
                    exit_px, reason, exit_i = cl_a[j], "消退", j
                    break
            if exit_px is None:
                exit_i = len(dates) - 1
                exit_px, reason = cl_a[exit_i], "持有中"
            trades.append({"信号日": dates[pos[d]].date(), "进场日": dates[i0].date(),
                           "区间上沿": round(zh, 1) if zh == zh else None,
                           "进场价(真实)": round(p0r, 2) if p0r == p0r else None,
                           "出场日": dates[exit_i].date(), "结果": reason,
                           "收益%": (exit_px / p0a - 1 - 0.001) * 100, "持有日": exit_i - i0 + 1})
            busy = exit_i
        return pd.DataFrame(trades), missed

    print("== v4.2 三版对比(条件计分已生效;全期 2015-2026 固定规则) ==")
    rows = []
    for name, mode, stop in [("A 市价+止损3%(旧执行)", "market", 0.03),
                             ("B 市价+止损4%", "market", 0.04),
                             ("C 成本区间+止损4%(v4.2)", "zone", 0.04)]:
        tr, miss = run(mode, stop)
        done = tr[tr["结果"] != "持有中"]
        rows.append({"版本": name, "笔数": len(tr), "错过": miss,
                     "胜率%": round((done["收益%"] > 0).mean() * 100, 1),
                     "均收益%": round(done["收益%"].mean(), 2),
                     "总收益%": round(done["收益%"].sum(), 1),
                     "赚>=10%笔": int((done["收益%"] >= 10).sum()),
                     "止损笔": int((done["结果"] == "止损").sum())})
        if mode == "zone":
            tr_zone = tr
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n== v4.2(成本区间+4%)2026-04-24 起轨迹 ==")
    t26 = tr_zone[pd.to_datetime(tr_zone["信号日"]) >= "2026-04-20"]
    print(t26.round(2).to_string(index=False))
    tr_zone.to_pickle(aulib.OUT / "v42_trades.pkl")
    print("\n已写出 out/v42_trades.pkl")


if __name__ == "__main__":
    sys.exit(main())
