# -*- coding: utf-8 -*-
"""卖点=顶部识别规则回测(运营者口径修正:+10%是买点质量门槛,不是止盈)。

一、顶部预测力检验(事件研究):每条候选规则的触发日,之后10/20日收益是否显著为负/低于基线。
    真顶标准 = zigzag ±10% 的峰(与运营者大波段表同构)。
二、轮次回测:OOS 买点进场,持有至各卖出规则触发(3%硬止损兜底),对比追踪6%基准。
    评"波段捕获":卖出价距该轮最高点让利多少。
候选规则:
  T1 共振撤退>=2家   T2 共振撤退>=3家(核心七席位5日内负向事件家数)
  T3 核心合计净多从60日峰回落>=15%
  T4 价格创20日新高后 OI 5日净降(资金背离)
  T5 跌破MA20        T6 追踪6%(被动基准)
  T7 = T1 且 价格从持仓最高回撤>=2%(席位撤退+价格确认)
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_profile import forward_returns
from run_optimize import CORE7, expanding_weights, build_events

pd.set_option("display.width", 220)


def negative_events(md, cont, members, q=0.80, window=250, min_hist=120):
    """负向事件(减多/增空)日表:member, trade_date, strength。"""
    oi = cont["oi_total"]
    out = []
    for m in members:
        s = md[md["member"] == m].set_index("trade_date").sort_index()
        if len(s) < min_hist:
            continue
        flow = (s["dnet"] / oi.reindex(s.index)).dropna()
        thr = flow.abs().rolling(window, min_periods=min_hist).quantile(q).shift(1)
        hit = flow[(flow.abs() >= thr) & thr.notna() & (flow < 0)]  # 负向
        if not len(hit):
            continue
        out.append(pd.DataFrame({"member": m, "trade_date": hit.index,
                                 "strength": (hit.abs() / thr.loc[hit.index]).clip(upper=3).to_numpy()}))
    return pd.concat(out, ignore_index=True)


def build_top_signals(cont, md):
    dates = cont.index
    neg = negative_events(md, cont, CORE7)
    negp = neg.pivot_table(index="trade_date", columns="member", values="strength", aggfunc="max").reindex(dates)
    nexit5 = negp.notna().rolling(5, min_periods=1).max().sum(axis=1)

    # 核心合计净多(可见口径)
    sub = md[md["member"].isin(CORE7)]
    netsum = sub.groupby("trade_date")["net"].sum().reindex(dates).ffill(limit=10)
    net_peak60 = netsum.rolling(60, min_periods=20).max()
    net_dd = netsum / net_peak60 - 1

    c = cont["adj_close"]
    ma20 = c.rolling(20).mean()
    hh20 = c >= c.rolling(20).max() * 0.999
    oi_dn = cont["oi_total"].diff(5) < 0

    sig = pd.DataFrame(index=dates)
    sig["T1"] = nexit5 >= 2
    sig["T2"] = nexit5 >= 3
    sig["T3"] = net_dd <= -0.15
    sig["T4"] = hh20 & oi_dn
    sig["T5"] = c < ma20
    return sig


def top_event_study(sig, cont, zz):
    fwd = forward_returns(cont)
    peaks = zz[zz["type"] == "下跌"]["from"].dropna()  # 每段下跌的起点=峰
    rows = []
    for t in ["T1", "T2", "T3", "T4"]:
        days = sig.index[sig[t] & ~sig[t].shift(1, fill_value=False)]  # 触发首日
        if len(days) < 10:
            continue
        r10 = fwd.reindex(days)[10].dropna()
        r20 = fwd.reindex(days)[20].dropna()
        near = [min(abs((t0 - p).days) for p in peaks) for t0 in days]
        rows.append({"规则": t, "触发次数": len(days),
                     "后10日均%": r10.mean() * 100, "后20日均%": r20.mean() * 100,
                     "20日为负占比%": (r20 < 0).mean() * 100,
                     "距最近真顶中位天数": float(np.median(near))})
    base = forward_returns(cont)[20].dropna()
    rows.append({"规则": "基线(全体日)", "触发次数": len(base),
                 "后10日均%": forward_returns(cont)[10].mean() * 100, "后20日均%": base.mean() * 100,
                 "20日为负占比%": (base < 0).mean() * 100, "距最近真顶中位天数": np.nan})
    return pd.DataFrame(rows)


def replay_hold(cont, entries, sig, mode, trail=0.06):
    """买点进场,持有至卖出规则触发(收盘出),3% 硬止损兜底;返回逐笔+波段捕获。"""
    dates = cont.index.to_list()
    pos = {d: i for i, d in enumerate(dates)}
    hi, lo, cl = cont["adj_high"].to_numpy(), cont["adj_low"].to_numpy(), cont["adj_close"].to_numpy()
    sv = {t: sig[t].to_numpy() for t in sig.columns}
    out = []
    for _, e in entries.iterrows():
        i0 = pos.get(pd.Timestamp(e["进场日"]))
        if i0 is None:
            continue
        p0 = e["进场价"]
        stop_px = p0 * 0.97
        peak = p0
        exit_px = reason = None
        exit_i = None
        for i in range(i0, min(i0 + 250, len(dates))):
            if np.isnan(lo[i]) or np.isnan(hi[i]):
                continue
            if lo[i] <= stop_px:
                exit_px, reason, exit_i = stop_px, "止损", i
                break
            peak = max(peak, hi[i])
            trig = False
            if mode == "TRAIL":
                trig = lo[i] <= peak * (1 - trail)
                if trig:
                    exit_px, reason, exit_i = peak * (1 - trail), "追踪", i
                    break
            else:
                if mode == "T7":
                    trig = sv["T1"][i] and cl[i] <= peak * 0.98
                else:
                    trig = sv[mode][i]
                if trig and i > i0:
                    exit_px, reason, exit_i = cl[i], "信号", i
                    break
        if exit_px is None:
            exit_i = min(i0 + 250, len(dates)) - 1
            exit_px, reason = cl[exit_i], "超时"
        capture = (exit_px - p0) / (peak - p0) if peak > p0 * 1.001 else np.nan
        out.append({"进场日": e["进场日"], "收益%": (exit_px / p0 - 1 - 0.001) * 100,
                    "出场": reason, "持有最高涨幅%": (peak / p0 - 1) * 100,
                    "波段捕获%": capture * 100 if capture == capture else np.nan,
                    "持有日": exit_i - i0 + 1})
    return pd.DataFrame(out)


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")
    zz = pd.read_pickle(aulib.OUT / "au_zigzag10.pkl")
    oos = pd.read_pickle(aulib.OUT / "oos_trades.pkl")

    sig = build_top_signals(cont, md)
    print("== 一、顶部预测力(触发后收益应显著低于基线;'距真顶'越小越准) ==")
    print(top_event_study(sig, cont, zz).round(2).to_string(index=False))

    print(f"\n== 二、轮次回测:同一批 {len(oos)} 笔 OOS 买点,持有至各卖出规则触发 ==")
    rows = []
    for mode, name in [("T1", "共振撤退>=2"), ("T2", "共振撤退>=3"), ("T3", "净多回落15%"),
                       ("T4", "新高OI背离"), ("T5", "破MA20"), ("T7", "撤退+价格确认"),
                       ("TRAIL", "追踪6%(基准)")]:
        tr = replay_hold(cont, oos, sig, mode)
        rows.append({"卖出规则": f"{mode} {name}", "均收益%": round(tr["收益%"].mean(), 2),
                     "中位%": round(tr["收益%"].median(), 2), "总收益%": round(tr["收益%"].sum(), 1),
                     "均持有日": round(tr["持有日"].mean(), 1),
                     "波段捕获中位%": round(tr["波段捕获%"].median(), 1),
                     "赚≥10%笔数": int((tr["收益%"] >= 10).sum())})
    print(pd.DataFrame(rows).to_string(index=False))
    sig.to_pickle(aulib.OUT / "top_signals.pkl")
    print("\n已写出 out/top_signals.pkl")


if __name__ == "__main__":
    sys.exit(main())
