# -*- coding: utf-8 -*-
"""买点算法 v2:席位加权 SmartMoney 评分 + 进场方式优化,止损硬约束 3%。

运营者拍板(2026-08-11):止损 3% 不放宽;信号组=中信/中财/高盛/海通/国泰/东证/国贸;
权重由数据定,逐年扩窗(每年只用当年以前的事件算 t 值),防前视。

信号分数: score(t) = Σ_i w_i(year) × strength_i(t)
  w_i = clamp(截至上年末该席位"增多"事件 h20 方向收益 t 值, 0, 5),样本<30 记 0
  strength_i(t) = 过去5日内该席位增多事件的 max(|flow|/触发阈值, 封顶3);无事件=0
买点候选日: score >= θ 且 收盘创20日新高 且 收盘>MA200
进场方式: A=T+1 开盘追突破;B=候选日后10个交易日内回调至 收盘×(1-回调%) 限价买入
出场: 目标+10% / 止损3%(盘中,同日先止损) / 超时120日
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_profile import forward_returns

pd.set_option("display.width", 220)

CORE7 = ["中信期货", "中财期货", "高盛期货", "海通期货", "国泰君安", "东证期货", "国贸期货"]
TARGET, MAXHOLD, COST = 0.10, 120, 0.001


def build_events(md, cont, members, q=0.80, window=250, min_hist=120):
    """增多事件表:member, trade_date, strength(=|flow|/阈值), fwd20。"""
    fwd = forward_returns(cont)
    oi = cont["oi_total"]
    out = []
    for m in members:
        s = md[md["member"] == m].set_index("trade_date").sort_index()
        if len(s) < min_hist:
            continue
        flow = (s["dnet"] / oi.reindex(s.index)).dropna()
        thr = flow.abs().rolling(window, min_periods=min_hist).quantile(q).shift(1)
        hit = flow[(flow.abs() >= thr) & thr.notna() & (flow != 0)]
        sub = s.loc[hit.index]
        long_dom = sub["dlong"].abs().fillna(0) >= sub["dshort"].abs().fillna(0)
        idx = sub.index[(sub["dnet"] > 0) & long_dom]
        if not len(idx):
            continue
        strength = (flow.loc[idx].abs() / thr.loc[idx]).clip(upper=3.0)
        out.append(pd.DataFrame({"member": m, "trade_date": idx, "strength": strength.to_numpy(),
                                 "fwd20": fwd.reindex(idx)[20].to_numpy()}))
    return pd.concat(out, ignore_index=True)


def expanding_weights(ev, years):
    """每年权重:用 < 当年-21交易日 的事件 dr20 算 t 值(h20 已实现,无前视)。"""
    w = {}
    for y in years:
        cutoff = pd.Timestamp(f"{y - 1}-12-01")  # 留出 h20 实现期
        row = {}
        for m in CORE7:
            dr = ev[(ev["member"] == m) & (ev["trade_date"] < cutoff)]["fwd20"].dropna()
            if len(dr) < 30 or dr.std(ddof=1) == 0:
                row[m] = 0.0
            else:
                t = dr.mean() / dr.std(ddof=1) * np.sqrt(len(dr))
                row[m] = float(np.clip(t, 0, 5))
        w[y] = row
    return w


def score_series(ev, cont, weights):
    dates = cont.index
    strong = ev.pivot_table(index="trade_date", columns="member", values="strength", aggfunc="max")
    strong = strong.reindex(dates)
    recent = strong.rolling(5, min_periods=1).max()  # 过去5日内最强事件
    score = pd.Series(0.0, index=dates)
    for m in CORE7:
        if m not in recent.columns:
            continue
        wm = pd.Series([weights[d.year].get(m, 0.0) for d in dates], index=dates)
        score = score.add(recent[m].fillna(0) * wm, fill_value=0)
    return score


def run_trades(cont, cand_days, entry="breakout", pullback=0.015, stop=0.03):
    dates = cont.index.to_list()
    pos = {d: i for i, d in enumerate(dates)}
    hi, lo, cl = cont["adj_high"].to_numpy(), cont["adj_low"].to_numpy(), cont["adj_close"].to_numpy()
    op = cont["adj_open"].to_numpy()
    trades = []
    busy_until = -1
    missed = 0
    for d in cand_days:
        i_sig = pos.get(d)
        if i_sig is None or i_sig + 1 >= len(dates) or i_sig < busy_until:
            continue
        # 进场
        if entry == "breakout":
            i0 = i_sig + 1
            p0 = op[i0] if not np.isnan(op[i0]) else cl[i0]
            if np.isnan(p0):
                continue
        else:  # pullback 限价
            limit = cl[i_sig] * (1 - pullback)
            i0, p0 = None, None
            for i in range(i_sig + 1, min(i_sig + 11, len(dates))):
                if i <= busy_until:
                    break
                if not np.isnan(lo[i]) and lo[i] <= limit:
                    # 若当日开盘已低于限价,按开盘成交(更优)
                    p0 = min(limit, op[i]) if not np.isnan(op[i]) else limit
                    i0 = i
                    break
            if i0 is None:
                missed += 1
                continue
        stop_px, tgt_px = p0 * (1 - stop), p0 * (1 + TARGET)
        mae = mfe = 0.0
        exit_i = exit_px = reason = None
        for i in range(i0, min(i0 + MAXHOLD, len(dates))):
            l_, h_ = lo[i], hi[i]
            if np.isnan(l_) or np.isnan(h_):
                continue
            mae, mfe = min(mae, l_ / p0 - 1), max(mfe, h_ / p0 - 1)
            if l_ <= stop_px:
                exit_i, exit_px, reason = i, stop_px, "止损"
                break
            if h_ >= tgt_px:
                exit_i, exit_px, reason = i, tgt_px, "目标"
                break
        if exit_i is None:
            exit_i = min(i0 + MAXHOLD, len(dates)) - 1
            exit_px, reason = cl[exit_i], "超时"
        trades.append({"信号日": dates[i_sig].date(), "进场日": dates[i0].date(), "出场日": dates[exit_i].date(),
                       "进场价": p0, "结果": reason, "收益%": (exit_px / p0 - 1 - COST) * 100,
                       "MAE%": mae * 100, "持有日": exit_i - i0 + 1})
        busy_until = exit_i
    return pd.DataFrame(trades), missed


def summarize(tr, missed=0):
    if tr.empty:
        return {"笔数": 0}
    return {"笔数": len(tr), "错过": missed, "达标率%": round((tr["结果"] == "目标").mean() * 100, 1),
            "止损率%": round((tr["结果"] == "止损").mean() * 100, 1),
            "均收益%": round(tr["收益%"].mean(), 2), "总收益%": round(tr["收益%"].sum(), 1),
            "均持有日": round(tr["持有日"].mean(), 1)}


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")

    # 补开盘复权
    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    op = price.set_index(["contract", "trade_date"])["open_price"].unstack(0)
    cont = cont.copy()
    cont["open"] = [op.at[d, m] if m in op.columns else np.nan for d, m in zip(mc["trade_date"], mc["main"])]
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]

    ev = build_events(md, cont, CORE7)
    years = range(cont.index[0].year, cont.index[-1].year + 1)
    weights = expanding_weights(ev, years)
    wdf = pd.DataFrame(weights).T
    print("== 逐年扩窗权重(节选 2012/2016/2020/2024/2026) ==")
    print(wdf.loc[[2012, 2016, 2020, 2024, 2026]].round(2).to_string())

    score = score_series(ev, cont, weights)
    close = cont["adj_close"]
    conf = (close >= close.rolling(20).max() * 0.999) & (close > close.rolling(200).mean())

    print("\n== v2 变体网格(止损<=3% 硬约束) ==")
    rows = []
    for theta in (2.0, 3.0, 4.0, 5.0):
        cand = list(cont.index[(score >= theta) & conf])
        for entry, pb in [("breakout", None), ("pullback", 0.01), ("pullback", 0.015), ("pullback", 0.02)]:
            for stop in (0.02, 0.025, 0.03):
                tr, miss = run_trades(cont, cand, entry=entry, pullback=pb or 0, stop=stop)
                tag = "追突破" if entry == "breakout" else f"回调{pb * 100:.1f}%"
                rows.append({"θ": theta, "进场": tag, "止损%": stop * 100, **summarize(tr, miss)})
    res = pd.DataFrame(rows)
    print(res.to_string(index=False))

    res.to_pickle(aulib.OUT / "optimize_grid.pkl")
    # 保存 v2 信号基础设施供最终规则用
    score.to_pickle(aulib.OUT / "smartmoney_score.pkl")
    wdf.to_pickle(aulib.OUT / "weights_by_year.pkl")
    ev.to_pickle(aulib.OUT / "events_core7.pkl")
    print("\n已写出 out/optimize_grid.pkl, smartmoney_score.pkl, weights_by_year.pkl, events_core7.pkl")


if __name__ == "__main__":
    sys.exit(main())
