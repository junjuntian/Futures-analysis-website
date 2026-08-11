# -*- coding: utf-8 -*-
"""纯席位系统 v4(运营者初衷:只看核心席位持仓变化,不要任何价格规则)。

买点特征(全部来自持仓变化):
  score5   5日内加权事件强度合成
  nseat5   5日内共振家数(有事件且当年权重>0)
  pers10   近10日内事件日数(连续建仓,显式建模)
买点规则空间 36 组合,逐年 walk-forward 选阈值;进场=信号日 T+1 开盘直接买。
止损3%=资金管理约束(非信号);评估口径:达标=先+10%后-3%。

卖点候选(全部来自持仓变化,进场后逐日检查,触发收盘出,3%硬止损兜底):
  S1 反向共振>=2家(5日内)      S2 信号消退:近10日核心席位零事件
  S3 核心合计净多从持有期峰值回落>=10%   S4 反向加权强度>=买入时强度的一半
对照:固定持有40日、追踪6%(参照物)。
"""
import sys
from itertools import product

import numpy as np
import pandas as pd

import aulib
from run_optimize import CORE7, build_events, expanding_weights
from run_tops import negative_events

pd.set_option("display.width", 220)
pd.set_option("display.max_rows", 100)

STOP = 0.03
WF_START = 2015


def features(cont, md):
    dates = cont.index
    ev = build_events(md, cont, CORE7)
    weights = expanding_weights(ev, range(dates[0].year, dates[-1].year + 1))
    strong = ev.pivot_table(index="trade_date", columns="member", values="strength", aggfunc="max").reindex(dates)
    wmat = pd.DataFrame({m: [weights[d.year].get(m, 0.0) for d in dates] for m in CORE7}, index=dates)
    f = pd.DataFrame(index=dates)
    f["score5"] = (strong[CORE7].fillna(0) * wmat).rolling(5, min_periods=1).max().sum(axis=1)
    active = (strong[CORE7].notna() & (wmat > 0))
    f["nseat5"] = active.rolling(5, min_periods=1).max().sum(axis=1)
    f["pers10"] = active.any(axis=1).rolling(10, min_periods=1).sum()

    neg = negative_events(md, cont, CORE7)
    negp = neg.pivot_table(index="trade_date", columns="member", values="strength", aggfunc="max").reindex(dates)
    f["neg_nseat5"] = (negp.notna() & (wmat > 0)).rolling(5, min_periods=1).max().sum(axis=1)
    f["neg_score5"] = (negp[CORE7].fillna(0) * wmat).rolling(5, min_periods=1).max().sum(axis=1)
    f["any_ev10"] = active.any(axis=1).rolling(10, min_periods=1).sum()

    sub = md[md["member"].isin(CORE7)]
    f["netsum"] = sub.groupby("trade_date")["net"].sum().reindex(dates).ffill(limit=10)
    return f


def run_trades_pure(cont, cand_days, stop=STOP, target=0.10, maxhold=120):
    dates = cont.index.to_list()
    pos = {d: i for i, d in enumerate(dates)}
    hi, lo, cl, op = (cont[c].to_numpy() for c in ["adj_high", "adj_low", "adj_close", "adj_open"])
    trades, busy = [], -1
    for d in cand_days:
        i_sig = pos.get(d)
        if i_sig is None or i_sig + 1 >= len(dates) or i_sig < busy:
            continue
        i0 = i_sig + 1
        p0 = op[i0] if not np.isnan(op[i0]) else cl[i0]
        if np.isnan(p0):
            continue
        stop_px, tgt = p0 * (1 - stop), p0 * (1 + target)
        exit_i = exit_px = reason = None
        for i in range(i0, min(i0 + maxhold, len(dates))):
            if np.isnan(lo[i]) or np.isnan(hi[i]):
                continue
            if lo[i] <= stop_px:
                exit_i, exit_px, reason = i, stop_px, "止损"
                break
            if hi[i] >= tgt:
                exit_i, exit_px, reason = i, tgt, "目标"
                break
        if exit_i is None:
            exit_i = min(i0 + maxhold, len(dates)) - 1
            exit_px, reason = cl[exit_i], "超时"
        trades.append({"信号日": dates[i_sig].date(), "进场日": dates[i0].date(), "进场价": p0,
                       "出场日": dates[exit_i].date(), "结果": reason,
                       "收益%": (exit_px / p0 - 1 - 0.001) * 100, "持有日": exit_i - i0 + 1})
        busy = exit_i
    return pd.DataFrame(trades)


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")
    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    op = price.set_index(["contract", "trade_date"])["open_price"].unstack(0)
    cont = cont.copy()
    cont["open"] = [op.at[d, m] if m in op.columns else np.nan for d, m in zip(mc["trade_date"], mc["main"])]
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]

    f = features(cont, md)

    # ===== 买点:36 组合 walk-forward =====
    space = list(product([2.0, 4.0, 6.0, 8.0], [1, 2, 3], [1, 3, 5]))
    cache = []
    for theta, k, p in space:
        cand = list(f.index[(f["score5"] >= theta) & (f["nseat5"] >= k) & (f["pers10"] >= p)])
        tr = run_trades_pure(cont, cand) if len(cand) >= 15 else pd.DataFrame()
        cache.append(((theta, k, p), tr))

    def sc(tr, upto):
        d = tr[pd.to_datetime(tr["出场日"]) < upto] if len(tr) else tr
        if len(d) < 12:
            return -np.inf
        return d["收益%"].mean() - d["收益%"].std(ddof=1) / np.sqrt(len(d))

    oos, picks = [], []
    for y in range(WF_START, cont.index[-1].year + 1):
        cutoff = pd.Timestamp(f"{y}-01-01")
        best = max(range(len(cache)), key=lambda i: sc(cache[i][1], cutoff))
        (theta, k, p), tr = cache[best]
        yr = tr[pd.to_datetime(tr["进场日"]).dt.year == y] if len(tr) else pd.DataFrame()
        oos.append(yr)
        picks.append({"年": y, "θ": theta, "共振": k, "持续": p, "当年笔数": len(yr)})
    oos_tr = pd.concat(oos, ignore_index=True)
    print("== 纯席位买点:逐年所选阈值 ==")
    print(pd.DataFrame(picks).to_string(index=False))
    d = oos_tr
    print(f"\n== 纯席位买点 OOS({WF_START}-2026):笔数 {len(d)},达标率 {(d['结果']=='目标').mean()*100:.1f}%,"
          f"止损率 {(d['结果']=='止损').mean()*100:.1f}%,均收益 {d['收益%'].mean():.2f}%,总 {d['收益%'].sum():.1f}% ==")
    d2 = d.copy()
    d2["年"] = pd.to_datetime(d2["进场日"]).dt.year
    print(d2.groupby("年").agg(笔数=("收益%", "size"), 均收益=("收益%", "mean"),
                              达标=("结果", lambda x: (x == "目标").mean() * 100)).round(2).to_string())
    print("\n(对照:带价格确认版 OOS 33 笔,达标 42.4%,均 +2.84%;2021 起 56%/+3.9%)")

    # 买点提前度:纯席位信号日 vs 上涨波段谷
    zz = pd.read_pickle(aulib.OUT / "au_zigzag10.pkl")
    troughs = zz[zz["type"] == "上涨"]["from"].dropna()
    sig_days = pd.to_datetime(d["信号日"])
    ahead = []
    for t0 in troughs:
        after = sig_days[(sig_days >= t0) & (sig_days <= t0 + pd.Timedelta(days=60))]
        if len(after):
            ahead.append((after.min() - t0).days)
    if ahead:
        print(f"\n买点滞后波段谷的天数(60日内有信号的 {len(ahead)} 段):中位 {np.median(ahead):.0f} 天")

    # ===== 卖点:纯席位状态,OOS 进场重放 =====
    dates = cont.index.to_list()
    pos = {dd: i for i, dd in enumerate(dates)}
    hi, lo, cl = (cont[c].to_numpy() for c in ["adj_high", "adj_low", "adj_close"])
    fv = {c: f[c].to_numpy() for c in f.columns}

    def replay(mode):
        out = []
        for _, e in oos_tr.iterrows():
            i0 = pos.get(pd.Timestamp(e["进场日"]))
            if i0 is None:
                continue
            p0 = e["进场价"]
            stop_px = p0 * 0.97
            peak = p0
            net_peak = fv["netsum"][i0] if fv["netsum"][i0] == fv["netsum"][i0] else 0
            exit_px = reason = None
            exit_i = None
            for i in range(i0, min(i0 + 250, len(dates))):
                if np.isnan(lo[i]) or np.isnan(hi[i]):
                    continue
                if lo[i] <= stop_px:
                    exit_px, reason, exit_i = stop_px, "止损", i
                    break
                peak = max(peak, hi[i])
                if fv["netsum"][i] == fv["netsum"][i]:
                    net_peak = max(net_peak, fv["netsum"][i])
                trig = False
                if i > i0 + 2:
                    if mode == "S1":
                        trig = fv["neg_nseat5"][i] >= 2
                    elif mode == "S2":
                        trig = fv["any_ev10"][i] == 0
                    elif mode == "S3":
                        trig = net_peak > 0 and fv["netsum"][i] == fv["netsum"][i] and \
                            fv["netsum"][i] <= net_peak * 0.90
                    elif mode == "S4":
                        trig = fv["neg_score5"][i] >= max(2.0, fv["score5"][i0] * 0.5)
                    elif mode == "HOLD40":
                        trig = i - i0 >= 40
                    elif mode == "TRAIL":
                        if lo[i] <= peak * 0.94:
                            exit_px, reason, exit_i = peak * 0.94, "追踪", i
                            break
                if trig:
                    exit_px, reason, exit_i = cl[i], "信号", i
                    break
            if exit_px is None:
                exit_i = min(i0 + 250, len(dates)) - 1
                exit_px, reason = cl[exit_i], "超时"
            cap = (exit_px - p0) / (peak - p0) if peak > p0 * 1.001 else np.nan
            out.append({"收益%": (exit_px / p0 - 1 - 0.001) * 100, "持有日": exit_i - i0 + 1,
                        "捕获%": cap * 100 if cap == cap else np.nan, "出场": reason})
        return pd.DataFrame(out)

    print(f"\n== 纯席位卖点({len(oos_tr)} 笔同一批买点重放,只换卖法) ==")
    rows = []
    for mode, name in [("S1", "反向共振>=2"), ("S2", "信号消退(10日零事件)"), ("S3", "净多回落10%"),
                       ("S4", "反向强度过半"), ("HOLD40", "固定持有40日"), ("TRAIL", "追踪6%(参照)")]:
        tr = replay(mode)
        rows.append({"卖法": f"{mode} {name}", "均收益%": round(tr["收益%"].mean(), 2),
                     "中位%": round(tr["收益%"].median(), 2), "总收益%": round(tr["收益%"].sum(), 1),
                     "均持有日": round(tr["持有日"].mean(), 1), "捕获中位%": round(tr["捕获%"].median(), 1),
                     "赚>=10%笔": int((tr["收益%"] >= 10).sum())})
    print(pd.DataFrame(rows).to_string(index=False))

    oos_tr.to_pickle(aulib.OUT / "pure_oos_trades.pkl")
    print("\n已写出 out/pure_oos_trades.pkl")


if __name__ == "__main__":
    sys.exit(main())
