# -*- coding: utf-8 -*-
"""v4.3 P0：修正 v4.2「信号消退」卖出的 T+1 执行时序。

本脚本只修一个缺口：
- 原实现：第 10 个零有效增多事件交易日收盘成交；
- 修正后：该日 15:00 后确认，下一交易日开盘成交；开盘缺失即报数据错误。

其余口径逐项保持 run_v42.py 不变：七席位、条件计分、逐年扩窗权重、
score>=6、dist60<12%、netq<60%、成本区间 +/-5 元、10 日有效期、
固定 4% 止损、双边合计 0.1% 成本。
"""
from __future__ import annotations

import io
import sys

import numpy as np
import pandas as pd

import aulib
from run_optimize import CORE7, build_events, expanding_weights
from run_pure import features
from run_v42 import COND_SEATS, ZONE, seat_cost_series
from v43_core import ensure_research_inputs


STOP = 0.04
COST = 0.001
PENDING_RESULTS = {"持有中", "待消退执行"}


def prepare_inputs():
    """完全复刻 run_v42.py 的输入、特征、条件计分与成本引擎。"""
    ensure_research_inputs()
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")
    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")

    px = price.set_index(["contract", "trade_date"]).sort_index()
    opn = px["open_price"].unstack(0)
    setl = px["settlement_price"].unstack(0)
    cont = cont.copy()
    cont["open"] = [
        opn.at[d, m] if m in opn.columns else np.nan
        for d, m in zip(mc["trade_date"], mc["main"])
    ]
    cont["adj_open"] = cont["open"] / cont["factor"] * cont["factor"].iloc[-1]
    main_settle = pd.Series(
        [
            setl.at[d, m] if m in setl.columns else np.nan
            for d, m in zip(mc["trade_date"], mc["main"])
        ],
        index=cont.index,
    )

    feat = features(cont, md)
    close = cont["adj_close"]
    feat["dist60"] = close / close.rolling(60).min() - 1
    feat["netq"] = feat["netsum"].rolling(250, min_periods=120).rank(pct=True)

    dates = cont.index
    events = build_events(md, cont, CORE7)
    weights = expanding_weights(events, range(dates[0].year, dates[-1].year + 1))
    events["dist"] = feat["dist60"].reindex(events["trade_date"]).to_numpy()
    effective = events[
        ~(events["member"].isin(COND_SEATS) & (events["dist"] >= 0.05))
    ].copy()
    strong = effective.pivot_table(
        index="trade_date", columns="member", values="strength", aggfunc="max"
    )
    strong = strong.reindex(dates).reindex(columns=CORE7)
    wmat = pd.DataFrame(
        {m: [weights[d.year].get(m, 0.0) for d in dates] for m in CORE7},
        index=dates,
    )
    score = (strong.fillna(0) * wmat).rolling(5, min_periods=1).max().sum(axis=1)
    active10 = (
        (strong.notna() & (wmat > 0)).any(axis=1).rolling(10, min_periods=1).sum()
    )

    costs = {m: seat_cost_series(md, m, main_settle) for m in CORE7}
    signals = list(
        dates[(score >= 6) & (feat["dist60"] < 0.12) & (feat["netq"] < 0.6)]
    )
    return cont, effective, weights, costs, signals, active10


def run_zone_trades(decay_t1: bool, signal_start: str | None = None):
    """重放 v4.2 成本区间版；仅由 ``decay_t1`` 控制 P0 差异。"""
    cont, effective, weights, costs, signals, active10 = prepare_inputs()
    dates = cont.index
    pos = {d: i for i, d in enumerate(dates)}
    low_real = cont["low"].to_numpy()
    open_real = cont["open"].to_numpy()
    low_adj = cont["adj_low"].to_numpy()
    high_adj = cont["adj_high"].to_numpy()
    close_adj = cont["adj_close"].to_numpy()
    open_adj = cont["adj_open"].to_numpy()
    factors = (cont["factor"].iloc[-1] / cont["factor"]).to_numpy()
    event_count10 = active10.to_numpy()

    def weighted_cost_at(signal_day):
        # 刻意照抄 v4.2：用近 8 个自然日近似近 5 个交易日触发席位。
        recent = effective[
            (effective["trade_date"] > signal_day - pd.Timedelta(days=8))
            & (effective["trade_date"] <= signal_day)
        ]
        numerator = denominator = 0.0
        for member in recent["member"].unique():
            weight = weights[signal_day.year].get(member, 0.0)
            member_cost = costs[member].get(signal_day, np.nan)
            if weight > 0 and member_cost == member_cost:
                numerator += weight * member_cost
                denominator += weight
        return numerator / denominator if denominator else np.nan

    trades = []
    busy_until = -1
    missed = 0
    for signal_day in signals:
        if signal_start is not None and signal_day < pd.Timestamp(signal_start):
            continue
        signal_i = pos[signal_day]
        if signal_i + 1 >= len(dates) or signal_i < busy_until:
            continue

        weighted_cost = weighted_cost_at(signal_day)
        if np.isnan(weighted_cost):
            entry_i = signal_i + 1
            entry_adj = open_adj[entry_i] if not np.isnan(open_adj[entry_i]) else close_adj[entry_i]
            entry_real = np.nan
            zone_high = np.nan
        else:
            zone_high = weighted_cost + ZONE
            entry_i = entry_adj = entry_real = None
            for i in range(signal_i + 1, min(signal_i + 11, len(dates))):
                if i <= busy_until:
                    break
                if not np.isnan(low_real[i]) and low_real[i] <= zone_high:
                    entry_real = min(open_real[i], zone_high) if not np.isnan(open_real[i]) else zone_high
                    entry_adj = entry_real * factors[i]
                    entry_i = i
                    break
            if entry_i is None:
                missed += 1
                continue

        if entry_adj is None or np.isnan(entry_adj):
            continue

        stop_price = entry_adj * (1 - STOP)
        exit_i = None
        exit_price = None
        reason = None
        decay_confirm_day = pd.NaT
        for i in range(entry_i, len(dates)):
            if np.isnan(low_adj[i]) or np.isnan(high_adj[i]):
                continue

            # 硬止损在确认日盘中仍然有效；若确认日存活，收盘后才知道消退。
            if low_adj[i] <= stop_price:
                exit_i, exit_price, reason = i, stop_price, "止损"
                break

            if i > entry_i + 2 and event_count10[i] == 0:
                decay_confirm_day = dates[i]
                if not decay_t1:
                    exit_i, exit_price, reason = i, close_adj[i], "消退"
                elif i + 1 < len(dates):
                    exit_i = i + 1
                    if not np.isfinite(open_adj[exit_i]):
                        raise ValueError(
                            f"消退 T+1 要求开盘成交，但 {dates[exit_i].date()} 复权开盘价缺失"
                        )
                    exit_price = open_adj[exit_i]
                    reason = "消退T+1"
                else:
                    # 最后一个交易日确认但尚无下一日执行价，不虚构成交。
                    exit_i, exit_price, reason = i, close_adj[i], "待消退执行"
                break

        if exit_price is None:
            exit_i = len(dates) - 1
            exit_price, reason = close_adj[exit_i], "持有中"

        trades.append(
            {
                "信号日": dates[signal_i].date(),
                "进场日": dates[entry_i].date(),
                "区间上沿": round(zone_high, 1) if zone_high == zone_high else None,
                "进场价(真实)": round(entry_real, 2) if entry_real == entry_real else None,
                "消退确认日": decay_confirm_day.date() if pd.notna(decay_confirm_day) else None,
                "出场日": dates[exit_i].date(),
                "结果": reason,
                "收益%": (exit_price / entry_adj - 1 - COST) * 100,
                "持有日": exit_i - entry_i + 1,
            }
        )
        busy_until = exit_i
    return pd.DataFrame(trades), missed


def summarize(name, trades, missed):
    done = trades[~trades["结果"].isin(PENDING_RESULTS)]
    return {
        "版本": name,
        "笔数": len(trades),
        "已完结": len(done),
        "错过信号": missed,
        "胜率%": round((done["收益%"] > 0).mean() * 100, 1),
        "均收益%": round(done["收益%"].mean(), 2),
        "止损笔数": int((done["结果"] == "止损").sum()),
        "总收益%": round(done["收益%"].sum(), 1),
    }


def main():
    original, missed_original = run_zone_trades(decay_t1=False)
    fixed, missed_fixed = run_zone_trades(decay_t1=True)
    original_2015, missed_original_2015 = run_zone_trades(
        decay_t1=False, signal_start="2015-01-01"
    )
    fixed_2015, missed_fixed_2015 = run_zone_trades(
        decay_t1=True, signal_start="2015-01-01"
    )
    rows = pd.DataFrame(
        [
            summarize("原脚本实际全序列(2011-2026)", original, missed_original),
            summarize("P0修正实际全序列(2011-2026)", fixed, missed_fixed),
            summarize("原实现严格2015-2026", original_2015, missed_original_2015),
            summarize("P0修正严格2015-2026", fixed_2015, missed_fixed_2015),
        ]
    )

    # 防止在无意中改变 P0 之外的任何基线口径。
    baseline = rows.iloc[0]
    expected = {
        "笔数": 30,
        "错过信号": 37,
        "胜率%": 75.9,
        "均收益%": 3.54,
        "止损笔数": 4,
        "总收益%": 102.6,
    }
    for key, value in expected.items():
        if baseline[key] != value:
            raise AssertionError(
                f"P0 基线复现失败：{key}={baseline[key]!r}，预期 {value!r}"
            )

    changed = original.merge(
        fixed,
        on=["信号日", "进场日"],
        how="outer",
        suffixes=("_原", "_修正"),
        indicator=True,
    )
    changed = changed[
        (changed["_merge"] != "both")
        | (changed["出场日_原"] != changed["出场日_修正"])
        | (changed["结果_原"] != changed["结果_修正"])
        | (~np.isclose(changed["收益%_原"], changed["收益%_修正"], equal_nan=True))
    ].copy()

    report = io.StringIO()
    report.write("== P0：v4.2 消退卖出 T+1 修正，原数字并排 ==\n")
    report.write(rows.to_string(index=False))
    report.write(
        "\n注：run_v42.py/REPORT 把30笔标为2015-2026，但原脚本没有起始日过滤，"
        "实际含2011-2014的6笔；因此同时列出代码全序列复现与严格2015起口径。\n"
    )
    report.write("\n\n== 受时序修正影响的逐笔记录 ==\n")
    show_cols = [
        "信号日",
        "进场日",
        "消退确认日_修正",
        "出场日_原",
        "出场日_修正",
        "结果_原",
        "结果_修正",
        "收益%_原",
        "收益%_修正",
    ]
    report.write(changed[show_cols].round(3).to_string(index=False))
    report.write("\n\n== 2026-04-24 起轨迹（修正后） ==\n")
    replay = fixed[pd.to_datetime(fixed["信号日"]) >= "2026-04-24"]
    report.write(replay.round(3).to_string(index=False))
    report.write("\n")

    text = report.getvalue()
    print(text)
    original.to_pickle(aulib.OUT / "v43_p0_trades_original.pkl")
    fixed.to_pickle(aulib.OUT / "v43_p0_trades_fixed.pkl")
    original_2015.to_pickle(aulib.OUT / "v43_p0_trades_original_2015.pkl")
    fixed_2015.to_pickle(aulib.OUT / "v43_p0_trades_fixed_2015.pkl")
    rows.to_csv(aulib.OUT / "v43_p0_baseline_compare.csv", index=False, encoding="utf-8-sig")
    changed.to_csv(aulib.OUT / "v43_p0_changed_trades.csv", index=False, encoding="utf-8-sig")
    (aulib.OUT / "v43_p0_report.txt").write_text(text, encoding="utf-8")
    print("已写出 out/v43_p0_report.txt、v43_p0_baseline_compare.csv 及逐笔文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
