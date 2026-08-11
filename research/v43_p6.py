# -*- coding: utf-8 -*-
"""v4.3 P6：score 强度/共振家数映射 0.5/1.0 仓位的组合级 OOS。"""
from __future__ import annotations

import io
import sys
from itertools import product

import numpy as np
import pandas as pd

import aulib
from v43_core import (
    TradeConfig,
    build_event_model,
    completed,
    conservative_score,
    load_context,
    overlap_count,
    run_trades_yearly,
)


SCORE_LEVELS = [7.0, 8.0, 9.0]
NSEATS = [2, 3]


def mapping_space():
    yield {"映射": "全仓基准", "score阈值": "-", "共振": "-", "逻辑": "全仓"}
    for score_level in SCORE_LEVELS:
        yield {"映射": "仅强度", "score阈值": score_level, "共振": "-", "逻辑": "强度"}
    for nseat in NSEATS:
        yield {"映射": "仅共振", "score阈值": "-", "共振": nseat, "逻辑": "共振"}
    for score_level, nseat in product(SCORE_LEVELS, NSEATS):
        yield {"映射": "强度且共振", "score阈值": score_level, "共振": nseat, "逻辑": "AND"}
    for score_level, nseat in product(SCORE_LEVELS, NSEATS):
        yield {"映射": "强度或共振", "score阈值": score_level, "共振": nseat, "逻辑": "OR"}


def size_trades(trades, params):
    sized = trades.copy()
    score = pd.to_numeric(sized["信号score"], errors="coerce")
    nseat = pd.to_numeric(sized["信号共振家数"], errors="coerce")
    logic = params["逻辑"]
    if logic == "全仓":
        strong = pd.Series(True, index=sized.index)
    elif logic == "强度":
        strong = score >= float(params["score阈值"])
    elif logic == "共振":
        strong = nseat >= int(params["共振"])
    else:
        hit_ratio = score >= float(params["score阈值"])
        hit_nseat = nseat >= int(params["共振"])
        strong = hit_ratio & hit_nseat if logic == "AND" else hit_ratio | hit_nseat
    sized["仓位"] = np.where(strong, 1.0, 0.5)
    sized["原收益%"] = sized["收益%"]
    sized["收益%"] = sized["原收益%"] * sized["仓位"]
    return sized


def max_drawdown_trade_curve(returns):
    if len(returns) == 0:
        return np.nan
    equity = np.cumprod(1 + np.asarray(returns, dtype=float) / 100.0)
    equity = np.concatenate([[1.0], equity])
    peak = np.maximum.accumulate(equity)
    return float(np.min(equity / peak - 1.0) * 100)


def portfolio_summary(trades):
    done = completed(trades)
    if done.empty:
        return {
            "笔数": len(trades), "已完结": 0, "胜率%": np.nan, "加权均收益%": np.nan,
            "止损笔数": 0, "算术总收益%": 0.0, "复利收益%": 0.0, "平仓净值最大回撤%": np.nan,
            "平均仓位": np.nan, "1.0档笔数": 0,
        }
    returns = done["收益%"].to_numpy()
    return {
        "笔数": len(trades),
        "已完结": len(done),
        "胜率%": round((done["原收益%"] > 0).mean() * 100, 1),
        "加权均收益%": round(float(np.mean(returns)), 2),
        "止损笔数": int((done["结果"] == "止损").sum()),
        "算术总收益%": round(float(np.sum(returns)), 1),
        "复利收益%": round(float((np.prod(1 + returns / 100) - 1) * 100), 1),
        "平仓净值最大回撤%": round(max_drawdown_trade_curve(returns), 1),
        "平均仓位": round(float(done["仓位"].mean()), 3),
        "1.0档笔数": int((done["仓位"] == 1.0).sum()),
    }


def walk_forward_with_baseline_fallback(cache, final_year, start_year=2015):
    """训练不足12笔时因果回退首项全仓基准，不丢弃早年 OOS。"""
    oos = []
    picks = []
    for year in range(start_year, final_year + 1):
        cutoff = pd.Timestamp(f"{year}-01-01")
        scored = []
        for index, (params, trades, missed) in enumerate(cache):
            value = conservative_score(trades, cutoff, min_n=12)
            if np.isfinite(value):
                scored.append((value, -index, index, params, trades))
        if scored:
            value, _, _, params, trades = max(scored, key=lambda row: (row[0], row[1]))
            status = "已选择"
        else:
            params, trades, _ = cache[0]
            value = np.nan
            status = "训练不足回退全仓"
        year_trades = trades[pd.to_datetime(trades["信号日"]).dt.year == year].copy()
        year_trades["选择年"] = year
        for key, item in params.items():
            year_trades[f"参数_{key}"] = item
        oos.append(year_trades)
        picks.append(
            {"年": year, "状态": status, "训练保守分": round(value, 4) if np.isfinite(value) else np.nan,
             **params, "当年笔数": len(year_trades)}
        )
    return pd.concat(oos, ignore_index=True), pd.DataFrame(picks)


def main() -> int:
    ctx = load_context()
    model = build_event_model(ctx)
    base_config = TradeConfig(exact_trigger_window=True)
    base_trades, _ = run_trades_yearly(
        ctx,
        {
            year: (model, base_config, "abandon")
            for year in range(2015, ctx.dates[-1].year + 1)
        },
    )
    mappings = list(mapping_space())
    if len(mappings) > 2000:
        raise AssertionError(f"P6 网格 {len(mappings)} 超过 2000 红线")
    cache = []
    full_rows = []
    for params in mappings:
        sized = size_trades(base_trades, params)
        cache.append((params, sized, 0))
        full_rows.append({**params, **portfolio_summary(sized)})

    oos, picks = walk_forward_with_baseline_fallback(cache, final_year=ctx.dates[-1].year)
    if overlap_count(oos):
        raise AssertionError("P6 OOS 存在重叠持仓")
    full_position = size_trades(
        base_trades,
        {"映射": "全仓基准", "score阈值": "-", "共振": "-", "逻辑": "全仓"},
    )
    selected_years = set(picks["年"].astype(int))
    baseline_oos = full_position[
        pd.to_datetime(full_position["信号日"]).dt.year.isin(selected_years)
    ].copy()
    oos_summary = portfolio_summary(oos)
    baseline_summary = portfolio_summary(baseline_oos)
    enough = len(completed(oos)) >= 20
    half_share = float((completed(oos)["仓位"] == 0.5).mean()) if len(completed(oos)) else 0.0
    full_share = float((completed(oos)["仓位"] == 1.0).mean()) if len(completed(oos)) else 0.0
    both_used = half_share >= 0.20 and full_share >= 0.20
    if not enough:
        verdict = "样本不足仅供参考：P6 OOS 已完结少于20笔，不作为推荐依据。"
    elif (
        oos_summary["算术总收益%"] > baseline_summary["算术总收益%"]
        and
        oos_summary["复利收益%"] > baseline_summary["复利收益%"]
        and oos_summary["平仓净值最大回撤%"] >= baseline_summary["平仓净值最大回撤%"]
        and both_used
    ):
        verdict = "仓位分级提高 OOS 复利且未恶化回撤，可进入最终推荐比较。"
    else:
        verdict = "仓位分级未同时提高 OOS 复利并改善/保持回撤，维持1.0仓位基准。"
    if enough and not both_used:
        verdict += " 但0.5/1.0两档未各占至少20%，映射近似单一仓位，证据不足。"

    compare = pd.DataFrame(
        [
            {"口径": "P6 walk-forward OOS", **oos_summary},
            {"口径": "同期全部1.0仓位", **baseline_summary},
            {"口径": "全样本：全部1.0仓位（仅参考）", **portfolio_summary(full_position)},
        ]
    )
    full_table = pd.DataFrame(full_rows)
    full_top = full_table.sort_values(
        ["复利收益%", "平仓净值最大回撤%"], ascending=[False, False]
    ).head(12)

    report = io.StringIO()
    report.write("== P6 仓位分级：逐年 walk-forward ==\n")
    report.write(
        f"网格={len(mappings)}：全仓基准1 + score阈值3 + 共振2 + AND6 + OR6。"
        "信号日固定仓位；score阈值为7/8/9；满足强档为1.0，否则0.5，不测试空仓或杠杆。\n"
    )
    report.write(
        "训练只用年初前已完结的仓位加权净收益，评分仍为均值减标准误；"
        "不足12笔时因果回退1.0仓位，不删除早年OOS交易。"
        "最大回撤为逐笔平仓净值口径（非日内盯市），因此单独标名。\n\n"
    )
    report.write("== OOS 组合级对比 ==\n")
    report.write(compare.to_string(index=False))
    report.write(f"\n\n判定：{verdict}\n")
    report.write("\n== 逐年选择 ==\n")
    report.write(picks.to_string(index=False))
    report.write("\n\n== 全样本 top12（仅参考） ==\n")
    report.write(full_top.to_string(index=False))
    report.write("\n")
    text = report.getvalue()
    print(text)

    oos.to_pickle(aulib.OUT / "v43_p6_oos_trades.pkl")
    picks.to_csv(aulib.OUT / "v43_p6_wf_picks.csv", index=False, encoding="utf-8-sig")
    compare.to_csv(aulib.OUT / "v43_p6_compare.csv", index=False, encoding="utf-8-sig")
    full_table.to_csv(aulib.OUT / "v43_p6_fullsample_grid.csv", index=False, encoding="utf-8-sig")
    (aulib.OUT / "v43_p6_report.txt").write_text(text, encoding="utf-8")
    print("已写出 out/v43_p6_report.txt 及 P6 OOS/选择/网格文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
