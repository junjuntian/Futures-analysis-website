# -*- coding: utf-8 -*-
"""机构止损跟随检验(运营者 2026-08-11 提出:机构跑了我再跑,机构也会止损)。

场景区分:此前证明的是「机构盈利中减仓≠卖出信号」;本检验是「持仓中,当初触发
进场的席位自己砍仓(认错)」——前提消失,跟跑。

同一批 v4.2 进场(条件计分+成本区间),只换止损方式:
  E0 固定-4%(现行基准)+ 消退
  E1 触发席位「减多事件」→T+1开盘跑 + 消退 + 无固定止损(看最差单笔)
  E2 = E1 + 灾难兜底-8%
  E3 触发席位可见净仓 < 进场日的70%(砍了三成)→T+1开盘跑 + 消退 + 兜底-8%
  E4 核心七席位合计净多 < 进场日的80% →跑 + 消退 + 兜底-8%
掉榜=不可知:净仓比较只在该席位上榜可见日进行,不外推。
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_pure import features
from run_optimize import CORE7, build_events, expanding_weights
from run_v42 import COND_SEATS

pd.set_option("display.width", 220)


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")
    tr42 = pd.read_pickle(aulib.OUT / "v42_trades.pkl")

    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    opn = price.set_index(["contract", "trade_date"])["open_price"].unstack(0)
    cont = cont.copy()
    cont["open"] = [opn.at[d, m] if m in opn.columns else np.nan
                    for d, m in zip(mc["trade_date"], mc["main"])]
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]

    dates = cont.index
    pos = {d: i for i, d in enumerate(dates)}
    lo, hi, cl, op = (cont[c].to_numpy() for c in ["adj_low", "adj_high", "adj_close", "adj_open"])

    f = features(cont, md)
    c = cont["adj_close"]
    dist = c / c.rolling(60).min() - 1

    ev = build_events(md, cont, CORE7)
    weights = expanding_weights(ev, range(dates[0].year, dates[-1].year + 1))
    ev["dist"] = dist.reindex(ev["trade_date"]).to_numpy()
    ev_eff = ev[~(ev["member"].isin(COND_SEATS) & (ev["dist"] >= 0.05))]
    strong = ev_eff.pivot_table(index="trade_date", columns="member", values="strength", aggfunc="max")
    strong = strong.reindex(dates).reindex(columns=CORE7)
    wmat = pd.DataFrame({m: [weights[d.year].get(m, 0.0) for d in dates] for m in CORE7}, index=dates)
    active10 = (strong.notna() & (wmat > 0)).any(axis=1).rolling(10, min_periods=1).sum().to_numpy()

    # 每席位:净仓可见序列 + 减多事件日集合
    net_vis = {m: md[md["member"] == m].set_index("trade_date")["net"] for m in CORE7}
    oi = cont["oi_total"]
    exit_ev = {}
    for m in CORE7:
        s = md[md["member"] == m].set_index("trade_date").sort_index()
        flow = (s["dnet"] / oi.reindex(s.index)).dropna()
        thr = flow.abs().rolling(250, min_periods=120).quantile(0.80).shift(1)
        hit = flow[(flow.abs() >= thr) & thr.notna() & (flow < 0)]
        sub = s.loc[hit.index]
        long_dom = sub["dlong"].abs().fillna(0) > sub["dshort"].abs().fillna(0)
        exit_ev[m] = set(sub.index[long_dom])  # 减多事件(long 腿主导的净减)

    def trigger_seats(sig_d):
        recent = ev_eff[(ev_eff["trade_date"] > sig_d - pd.Timedelta(days=8)) & (ev_eff["trade_date"] <= sig_d)]
        return [m for m in recent["member"].unique() if weights[sig_d.year].get(m, 0) > 0]

    def replay(mode, hard=None):
        out = []
        for _, e in tr42.iterrows():
            d_sig, d_in = pd.Timestamp(e["信号日"]), pd.Timestamp(e["进场日"])
            i0 = pos.get(d_in)
            if i0 is None:
                continue
            p0 = e["进场价(真实)"]
            if p0 is None or p0 != p0:
                p0 = op[i0] / (cont["factor"].iloc[-1] / cont["factor"].iloc[i0])
            p0a = p0 * float(cont["factor"].iloc[-1] / cont["factor"].iloc[i0])
            seats = trigger_seats(d_sig)
            base_net = {m: net_vis[m].asof(d_in) for m in seats}
            follow_from = None  # 机构止损信号确认日 → 次日开盘执行
            exit_px = reason = None
            exit_i = None
            for i in range(i0, len(dates)):
                dd = dates[i]
                if np.isnan(lo[i]) or np.isnan(hi[i]):
                    continue
                if follow_from is not None and i > follow_from:
                    exit_px = op[i] if not np.isnan(op[i]) else cl[i]
                    reason, exit_i = "跟随机构跑", i
                    break
                if hard and lo[i] <= p0a * (1 - hard):
                    exit_px, reason, exit_i = p0a * (1 - hard), f"兜底-{hard*100:.0f}%", i
                    break
                if mode == "fixed" and lo[i] <= p0a * 0.96:
                    exit_px, reason, exit_i = p0a * 0.96, "止损-4%", i
                    break
                if i > i0 + 2 and active10[i] == 0:
                    exit_px, reason, exit_i = cl[i], "消退", i
                    break
                if follow_from is None and i > i0:
                    if mode == "event":
                        if any(dd in exit_ev[m] for m in seats):
                            follow_from = i
                    elif mode == "level":
                        for m in seats:
                            nv = net_vis[m].get(dd, np.nan)
                            if nv == nv and base_net.get(m, np.nan) == base_net.get(m, np.nan) \
                                    and base_net[m] > 0 and nv < base_net[m] * 0.70:
                                follow_from = i
                                break
                    elif mode == "sumlevel":
                        tot0 = sum(v for v in base_net.values() if v == v and v > 0)
                        totn = f["netsum"].get(dd, np.nan)
                        base_sum = f["netsum"].asof(d_in)
                        if totn == totn and base_sum == base_sum and totn < base_sum * 0.80:
                            follow_from = i
            if exit_px is None:
                exit_i = len(dates) - 1
                exit_px, reason = cl[exit_i], "持有中"
            out.append({"信号日": e["信号日"], "进场日": e["进场日"], "结果": reason,
                        "收益%": (exit_px / p0a - 1 - 0.001) * 100, "持有日": exit_i - i0 + 1})
        return pd.DataFrame(out)

    print("== 机构止损跟随 vs 固定止损(同一批 v4.2 进场,30 笔) ==")
    rows = []
    variants = [("E0 固定-4%(基准)", "fixed", None),
                ("E1 触发席位减多事件跟跑(无兜底)", "event", None),
                ("E2 减多事件跟跑+兜底-8%", "event", 0.08),
                ("E3 触发席位净仓砍30%跟跑+兜底-8%", "level", 0.08),
                ("E4 七席位合计净多降20%跟跑+兜底-8%", "sumlevel", 0.08)]
    details = {}
    for name, mode, hard in variants:
        tr = replay(mode, hard)
        details[name] = tr
        done = tr[tr["结果"] != "持有中"]
        rows.append({"卖法": name, "笔数": len(tr),
                     "胜率%": round((done["收益%"] > 0).mean() * 100, 1),
                     "均收益%": round(done["收益%"].mean(), 2),
                     "总收益%": round(done["收益%"].sum(), 1),
                     "最差单笔%": round(done["收益%"].min(), 1),
                     "亏损>4%笔数": int((done["收益%"] < -4.2).sum())})
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n== 关键案例逐笔对照(2026 年) ==")
    for name in ["E0 固定-4%(基准)", "E2 减多事件跟跑+兜底-8%", "E3 触发席位净仓砍30%跟跑+兜底-8%"]:
        t = details[name]
        t26 = t[pd.to_datetime(t["信号日"]) >= "2026-04-01"]
        print(f"-- {name} --")
        print(t26.round(2).to_string(index=False))


if __name__ == "__main__":
    sys.exit(main())
