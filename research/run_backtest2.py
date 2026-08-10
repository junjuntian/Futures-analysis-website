# -*- coding: utf-8 -*-
"""Task 4 变体:止损档位扫描 × MA200 牛市过滤。"""
import sys

import numpy as np
import pandas as pd

import aulib
import run_backtest as bt

pd.set_option("display.width", 220)


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    ev = pd.read_pickle(aulib.OUT / "events_classified.pkl")

    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    px = price.set_index(["contract", "trade_date"]).sort_index()
    op = px["open_price"].unstack(0)
    cont = cont.copy()
    cont["open"] = [op.at[d, m] if m in op.columns else np.nan for d, m in zip(mc["trade_date"], mc["main"])]
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]
    ma200 = cont["adj_close"].rolling(200).mean()
    bull = cont["adj_close"] > ma200

    sig = bt.build_signals(ev, cont)
    r4 = sig["R4 共振2+创20日新高"]
    r4_bull = [d for d in r4 if bool(bull.get(d, False))]
    r2 = sig["R2 五日共振>=2家"]
    r2_bull = [d for d in r2 if bool(bull.get(d, False))]

    rows = []
    for name, entries in [("R4", r4), ("R4+MA200", r4_bull), ("R2+MA200", r2_bull)]:
        for stop in (0.03, 0.04, 0.05):
            bt.STOP = stop
            tr = bt.run_rule(cont, entries, use_stop=True)
            s = bt.summarize(tr)
            rows.append({"规则": name, "止损%": stop * 100, **s})
    bt.STOP = 0.03
    print("== 止损档位 × 牛市过滤 ==")
    print(pd.DataFrame(rows).round(2).to_string(index=False))

    # R4+MA200, 止损4% 的分年度与逐笔
    bt.STOP = 0.04
    tr = bt.run_rule(cont, r4_bull, use_stop=True)
    tr["年"] = pd.to_datetime(tr["进场日"]).dt.year
    print("\n== R4+MA200(止损4%)分年度 ==")
    print(tr.groupby("年").agg(笔数=("收益%", "size"), 均收益=("收益%", "mean"),
                              达标=("结果", lambda x: (x == "目标").mean() * 100)).round(2).to_string())
    print("\n== 逐笔 ==")
    print(tr.to_string(index=False))
    tr.to_pickle(aulib.OUT / "trades_R4_ma200_s4.pkl")


if __name__ == "__main__":
    sys.exit(main())
