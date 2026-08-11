# -*- coding: utf-8 -*-
"""v4.3 P3：成本区间尺度、有效期与错过信号策略的 OOS 寻优。"""
from __future__ import annotations

import io
import sys
from itertools import product

import pandas as pd

import aulib
from v43_core import (
    TradeConfig,
    build_event_model,
    completed,
    load_context,
    overlap_count,
    run_trades_stateful,
    run_trades_yearly,
    summarize,
    walk_forward,
    weighted_cost_at,
)


ZONE_OPTIONS = [
    ("固定±5元", "fixed", 5.0),
    ("成本±0.50%", "percent", 0.0050),
    ("成本±0.75%", "percent", 0.0075),
    ("成本±1.00%", "percent", 0.0100),
    ("成本±1.50%", "percent", 0.0150),
    ("成本±2.00%", "percent", 0.0200),
]
VALIDITIES = [10, 3, 5, 15, 20]
POLICIES = [
    ("到期放弃", "abandon"),
    ("新信号重置", "refresh"),
    ("到期确认后T+1开盘追价", "expiry_market"),
]


def params_key(params):
    return (params["区间"], int(params["有效期"]), params["错过策略"])


def specs_from_picks(picks, spec_lookup):
    specs = {}
    for row in picks.loc[picks["状态"] == "已选择"].to_dict("records"):
        key = params_key(row)
        if key not in spec_lookup:
            raise KeyError(f"P3 年度选择缺少规格映射: {key}")
        specs[int(row["年"])] = spec_lookup[key]
    return specs


def run_wf(ctx, cache, spec_lookup, final_year, label):
    _, picks = walk_forward(cache, final_year=final_year)
    specs = specs_from_picks(picks, spec_lookup)
    trades, missed = run_trades_yearly(ctx, specs)
    yearly_counts = (
        pd.to_datetime(trades["信号日"]).dt.year.value_counts().to_dict()
        if not trades.empty
        else {}
    )
    picks = picks.copy()
    picks["独立候选切片笔数"] = picks["当年笔数"]
    picks["当年笔数"] = picks["年"].map(yearly_counts).fillna(0).astype(int)
    overlap = overlap_count(trades)
    if overlap:
        raise AssertionError(f"P3 {label} 连续 OOS 出现 {overlap} 笔重叠持仓")
    return trades, missed, picks


def main() -> int:
    ctx = load_context()
    model = build_event_model(ctx)
    # P3 严格基准与候选共用精确近5个交易日成本席位口径及逐日状态机。
    baseline_config = TradeConfig(
        entry_kind="fixed",
        zone_value=5.0,
        validity=10,
        miss_policy="abandon",
        exact_trigger_window=True,
    )
    baseline_policy = "abandon"
    baseline_trades, baseline_missed = run_trades_stateful(
        ctx, model, baseline_config, pending_policy=baseline_policy
    )
    finite_cost_trades = int(baseline_trades["机构加权成本"].notna().sum())
    market_fallback_trades = len(baseline_trades) - finite_cost_trades
    signal_costs = [
        weighted_cost_at(ctx, model, day, exact_trigger_window=True)[0]
        for day in model.signals
    ]
    finite_cost_signals = int(pd.Series(signal_costs).notna().sum())
    baseline_params = {"区间": "固定±5元", "有效期": 10, "错过策略": "到期放弃"}
    baseline_item = (baseline_params, baseline_trades, baseline_missed)
    baseline_spec = (model, baseline_config, baseline_policy)

    grid = list(product(ZONE_OPTIONS, VALIDITIES, POLICIES))
    if len(grid) > 2000:
        raise AssertionError(f"P3 网格 {len(grid)} 超过 2000 红线")
    cache = []
    fixed_cache = []
    percent_cache = []
    spec_lookup = {params_key(baseline_params): baseline_spec}
    full_rows = []
    print(f"P3 受控网格：{len(grid)} 组合……", flush=True)
    for index, ((zone_name, entry_kind, zone_value), validity, (policy_name, policy)) in enumerate(
        grid, start=1
    ):
        config = TradeConfig(
            entry_kind=entry_kind,
            zone_value=zone_value,
            validity=validity,
            miss_policy="abandon",
            exact_trigger_window=True,
        )
        trades, missed = run_trades_stateful(ctx, model, config, pending_policy=policy)
        params = {"区间": zone_name, "有效期": validity, "错过策略": policy_name}
        item = (params, trades, missed)
        spec_lookup[params_key(params)] = (model, config, policy)
        cache.append(item)
        (fixed_cache if entry_kind == "fixed" else percent_cache).append(item)
        full_rows.append({**params, **summarize(trades, missed)})
        if index % 30 == 0:
            print(f"  已完成 {index}/{len(grid)}", flush=True)

    adaptive_oos, adaptive_missed, adaptive_picks = run_wf(
        ctx, [baseline_item] + cache, spec_lookup, ctx.dates[-1].year, "含基准锚"
    )
    all_oos, all_missed, all_picks = run_wf(
        ctx, cache, spec_lookup, ctx.dates[-1].year, "全部P3候选"
    )
    fixed_oos, fixed_missed, fixed_picks = run_wf(
        ctx, fixed_cache, spec_lookup, ctx.dates[-1].year, "固定元候选"
    )
    percent_oos, percent_missed, percent_picks = run_wf(
        ctx, percent_cache, spec_lookup, ctx.dates[-1].year, "成本比例候选"
    )
    selected_years = set(all_picks.loc[all_picks["状态"] == "已选择", "年"].astype(int))
    baseline_year_specs = {year: baseline_spec for year in selected_years}
    baseline_same_years, baseline_oos_missed = run_trades_yearly(
        ctx, baseline_year_specs
    )
    if overlap_count(baseline_same_years):
        raise AssertionError("P3 同期严格基准连续 OOS 出现重叠持仓")

    all_summary = summarize(all_oos, all_missed)
    baseline_summary = summarize(baseline_same_years, baseline_oos_missed)
    adaptive_summary = summarize(adaptive_oos, adaptive_missed)
    fixed_summary = summarize(fixed_oos, fixed_missed)
    percent_summary = summarize(percent_oos, percent_missed)
    enough = len(completed(all_oos)) >= 20
    if not enough:
        verdict = "样本不足仅供参考：P3 OOS 已完结少于20笔，不得作为推荐依据。"
    elif (
        all_summary["均收益%"] > baseline_summary["均收益%"]
        and all_summary["总收益%"] > baseline_summary["总收益%"]
    ):
        verdict = "P3 OOS 的均收益与总收益均超过同期基准，可进入最终推荐比较。"
    else:
        verdict = "P3 OOS 未同时改善均收益与总收益，保留±5元/10日/到期放弃基准。"

    compare = pd.DataFrame(
        [
            {"口径": "P3 连续WF（允许年度回退v4.3严格固定基准）", **adaptive_summary},
            {"口径": "P3 连续WF（全部区间候选）", **all_summary},
            {"口径": "P3 连续WF（仅固定±5元）", **fixed_summary},
            {"口径": "P3 连续WF（仅成本百分比）", **percent_summary},
            {"口径": "同期v4.3严格固定基准（P0时序，连续回放）", **baseline_summary},
            {"口径": "全样本：v4.3严格固定基准（P0时序，仅参考）", **summarize(baseline_trades, baseline_missed)},
        ]
    )
    full_table = pd.DataFrame(full_rows)
    full_top = full_table[full_table["已完结"] >= 20].sort_values(
        ["均收益%", "已完结"], ascending=[False, False]
    ).head(12)

    report = io.StringIO()
    report.write("== P3 成本区间参数：逐年 walk-forward ==\n")
    report.write(
        f"网格={len(grid)}：固定±5元/成本百分比5档 × 有效期5档 × 错过策略3档。"
        "百分比区间按真实机构成本计算，成交后才转复权价。\n"
    )
    report.write(
        "P3 使用逐日单挂单状态机：T+1为第1个有效日；触及按min(开盘,上沿)；"
        "新信号重置只在当日收盘后生效；到期追价在到期确认后的T+1开盘。\n"
    )
    report.write(
        "训练仍按每年年初前已完结交易选择参数；主OOS把年度选择映射为信号年规格后只连续回放一次，"
        "挂单和持仓跨12月31日延续并冻结原模型、交易参数和挂单策略。v4.3严格固定基准（P0时序）"
        "为固定±5元、"
        "10交易日有效、到期放弃，且同样连续回放。\n\n"
    )
    report.write(
        f"成本覆盖审计：全样本严格基准 {len(baseline_trades)} 笔中仅 "
        f"{finite_cost_trades} 笔取得机构加权成本，{market_fallback_trades} 笔按既定兜底规则"
        f"转为T+1市价；{len(signal_costs)} 个信号日中仅 {finite_cost_signals} 个 weighted_cost 有限。"
        "本轮90组在实际成交层面没有形成差异。因此P3除样本不足外还存在"
        "成本覆盖不足，不能把相同成绩解释为区间参数有效证据。\n\n"
    )
    report.write("== OOS 对比 ==\n")
    report.write(compare.to_string(index=False))
    report.write(f"\n\n判定：{verdict}\n")
    report.write("\n== 全部 P3 候选逐年选择 ==\n")
    report.write(all_picks.to_string(index=False))
    report.write("\n\n== 固定±5元逐年选择 ==\n")
    report.write(fixed_picks.to_string(index=False))
    report.write("\n\n== 成本百分比逐年选择 ==\n")
    report.write(percent_picks.to_string(index=False))
    report.write("\n\n== 全样本 top12（仅参考） ==\n")
    report.write(full_top.to_string(index=False))
    report.write("\n")
    text = report.getvalue()
    print(text)

    all_oos.to_pickle(aulib.OUT / "v43_p3_oos_trades.pkl")
    adaptive_oos.to_pickle(aulib.OUT / "v43_p3_adaptive_oos_trades.pkl")
    all_picks.to_csv(aulib.OUT / "v43_p3_wf_picks.csv", index=False, encoding="utf-8-sig")
    fixed_picks.to_csv(aulib.OUT / "v43_p3_fixed_picks.csv", index=False, encoding="utf-8-sig")
    percent_picks.to_csv(aulib.OUT / "v43_p3_percent_picks.csv", index=False, encoding="utf-8-sig")
    compare.to_csv(aulib.OUT / "v43_p3_compare.csv", index=False, encoding="utf-8-sig")
    full_table.to_csv(aulib.OUT / "v43_p3_fullsample_grid.csv", index=False, encoding="utf-8-sig")
    (aulib.OUT / "v43_p3_report.txt").write_text(text, encoding="utf-8")
    print("已写出 out/v43_p3_report.txt 及 P3 OOS/选择/网格文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
