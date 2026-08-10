# -*- coding: utf-8 -*-
"""Task 4:买点回测。

进场:信号日 T+1 开盘(缺开盘用收盘);单仓位,持仓中忽略新信号。
出场:目标 +10% / 止损 -3%(盘中 low 触发,同日先止损后目标,保守)/ 超时 120 交易日。
另跑无止损版,回答"3% 止损是否砍掉好交易"。
成本:双边 0.05%(手续费+滑点+移仓,保守)。
收益一律用复权序列(移仓无缝);MAE/MFE 用复权高低价。
对照:全部交易日无条件进场跑同一套出场规则 = 无信息基线。
"""
import sys

import numpy as np
import pandas as pd

import aulib

pd.set_option("display.width", 220)

CORE = ["中信期货", "中财期货", "高盛期货", "海通期货", "国泰君安", "东证期货"]  # 增多类 t20>=3.5
TRIO = ["中信期货", "中财期货", "高盛期货"]
COST = 0.0005 * 2
TARGET = 0.10
STOP = 0.03
MAXHOLD = 120


def build_signals(ev: pd.DataFrame, cont: pd.DataFrame):
    """返回 {规则名: 信号日期列表}。"""
    buy = ev[ev["cls"] == "增多"]
    dates = cont.index
    sig = {}
    sig["R1 三核任一增多"] = sorted(buy[buy["member"].isin(TRIO)]["trade_date"].unique())
    # 共振:过去5日内 >=K 家核心席位出现增多
    m = buy[buy["member"].isin(CORE)].pivot_table(index="trade_date", columns="member",
                                                  values="flow", aggfunc="size").reindex(dates)
    cnt = (m.notna().rolling(5, min_periods=1).max().sum(axis=1))
    sig["R2 五日共振>=2家"] = list(dates[cnt >= 2])
    sig["R3 五日共振>=3家"] = list(dates[cnt >= 3])
    # 价格确认:收盘 >= 20日最高收盘(创新高)
    close = cont["adj_close"]
    hh20 = close.rolling(20).max()
    conf = close >= hh20 * 0.999
    sig["R4 共振2+创20日新高"] = list(dates[(cnt >= 2) & conf])
    # OI 确认:品种总持仓 5 日净增
    oi_up = cont["oi_total"].diff(5) > 0
    sig["R5 共振2+新高+OI增"] = list(dates[(cnt >= 2) & conf & oi_up])
    return sig


def run_rule(cont: pd.DataFrame, entries, use_stop=True):
    dates = cont.index.to_list()
    pos = {d: i for i, d in enumerate(dates)}
    o = cont["adj_high"] * np.nan  # 开盘复权:用 close/high/low 同因子换算
    adj_f = cont["factor"].iloc[-1] / cont["factor"]
    open_adj = (pd.read_pickle(aulib.OUT / "au_open.pkl")
                if (aulib.OUT / "au_open.pkl").exists() else None)
    hi, lo, cl = cont["adj_high"].to_numpy(), cont["adj_low"].to_numpy(), cont["adj_close"].to_numpy()
    op = cont["adj_open"].to_numpy() if "adj_open" in cont else cl  # 兜底
    trades = []
    busy_until = -1
    for d in entries:
        if d not in pos:
            continue
        i0 = pos[d] + 1  # T+1
        if i0 >= len(dates) or i0 <= busy_until:
            continue
        p0 = op[i0] if not np.isnan(op[i0]) else cl[i0]
        if np.isnan(p0):
            continue
        stop_px, tgt_px = p0 * (1 - STOP), p0 * (1 + TARGET)
        mae = mfe = 0.0
        exit_i, exit_px, reason = None, None, None
        for i in range(i0, min(i0 + MAXHOLD, len(dates))):
            l_, h_ = lo[i], hi[i]
            if np.isnan(l_) or np.isnan(h_):
                continue
            mae = min(mae, l_ / p0 - 1)
            mfe = max(mfe, h_ / p0 - 1)
            if use_stop and l_ <= stop_px:
                exit_i, exit_px, reason = i, stop_px, "止损"
                break
            if h_ >= tgt_px:
                exit_i, exit_px, reason = i, tgt_px, "目标"
                break
        if exit_i is None:
            exit_i = min(i0 + MAXHOLD, len(dates)) - 1
            exit_px, reason = cl[exit_i], "超时"
        ret = exit_px / p0 - 1 - COST
        trades.append({"信号日": dates[pos[d]].date(), "进场日": dates[i0].date(), "出场日": dates[exit_i].date(),
                       "结果": reason, "收益%": ret * 100, "MAE%": mae * 100, "MFE%": mfe * 100,
                       "持有日": exit_i - i0 + 1})
        busy_until = exit_i
    return pd.DataFrame(trades)


def summarize(tr: pd.DataFrame):
    if tr.empty:
        return {}
    return {"笔数": len(tr), "达标率%": (tr["结果"] == "目标").mean() * 100,
            "止损率%": (tr["结果"] == "止损").mean() * 100,
            "均收益%": tr["收益%"].mean(), "收益中位%": tr["收益%"].median(),
            "均持有日": tr["持有日"].mean(),
            "总收益%": tr["收益%"].sum()}


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    ev = pd.read_pickle(aulib.OUT / "events_classified.pkl")

    # 补开盘复权列
    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    px = price.set_index(["contract", "trade_date"]).sort_index()
    op = px["open_price"].unstack(0)
    vals = []
    for d, m in zip(mc["trade_date"], mc["main"]):
        vals.append(op.at[d, m] if m in op.columns else np.nan)
    cont = cont.copy()
    cont["open"] = vals
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]

    sig = build_signals(ev, cont)
    print("== 规则回测(止损3% / 目标10% / 超时120日 / 成本0.1%) ==")
    rows, all_trades = [], {}
    for name, entries in sig.items():
        tr = run_rule(cont, entries, use_stop=True)
        all_trades[name] = tr
        rows.append({"规则": name, **summarize(tr)})
    # 无信息基线:全部交易日进场
    base_tr = run_rule(cont, list(cont.index), use_stop=True)
    rows.append({"规则": "基线:任意日进场", **summarize(base_tr)})
    print(pd.DataFrame(rows).round(2).to_string(index=False))

    print("\n== 无止损版(看 MAE 分布,检验 3% 止损是否太紧) ==")
    rows2 = []
    for name, entries in sig.items():
        tr = run_rule(cont, entries, use_stop=False)
        win = tr[tr["结果"] == "目标"]
        rows2.append({"规则": name, "笔数": len(tr), "达标率%": (tr["结果"] == "目标").mean() * 100,
                      "均收益%": tr["收益%"].mean(),
                      "达标交易MAE中位%": win["MAE%"].median() if len(win) else np.nan,
                      "达标交易MAE 90分位%": win["MAE%"].quantile(0.10) if len(win) else np.nan})
    print(pd.DataFrame(rows2).round(2).to_string(index=False))

    best = "R4 共振2+创20日新高"
    print(f"\n== {best} 分年度稳定性(止损版) ==")
    tr = all_trades[best].copy()
    tr["年"] = pd.to_datetime(tr["进场日"]).dt.year
    g = tr.groupby("年").agg(笔数=("收益%", "size"), 均收益=("收益%", "mean"), 达标=("结果", lambda x: (x == "目标").mean() * 100))
    print(g.round(2).to_string())

    print(f"\n== {best} 全部逐笔 ==")
    print(all_trades[best].to_string(index=False))

    for k, v in all_trades.items():
        v.to_pickle(aulib.OUT / f"trades_{k[:2]}.pkl")
    print("\n已写出逐笔 out/trades_R*.pkl")


if __name__ == "__main__":
    sys.exit(main())
