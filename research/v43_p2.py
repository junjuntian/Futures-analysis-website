# -*- coding: utf-8 -*-
"""v4.3 P2：用近 N 日累计加权 Flow 精细建模持续建仓。"""
from __future__ import annotations

import io
import sys
from dataclasses import replace
from itertools import product

import numpy as np
import pandas as pd

import aulib
from run_optimize import CORE7
from v43_core import (
    BASE_CONDITIONS,
    TradeConfig,
    build_event_model,
    completed,
    load_context,
    overlap_count,
    run_trades_stateful,
    run_trades_yearly,
    summarize,
    walk_forward,
)


FLOW_WINDOWS = [5, 3, 10, 15, 20]
FEATURE_QUANTILES = [0.80, 0.70, 0.90]
FEATURE_KINDS = ["标准化累计量", "原始累计量", "近期加权速率"]
MODES = ["替代score5", "补充score5"]


def param_key(model, kind, window, feature_q):
    return (str(model), str(kind), int(window), round(float(feature_q), 8))


def yearly_specs_from_picks(picks, spec_lookup):
    specs = {}
    for _, row in picks[picks["状态"] == "已选择"].iterrows():
        key = param_key(row["模型"], row["Flow种类"], row["N"], row["触发分位"])
        specs[int(row["年"])] = spec_lookup[key]
    return specs


def positive_flow_panels(ctx, model):
    """构造每席位正向、长腿主导的 Flow；掉榜保持 NaN，不伪造为零观测。"""
    raw = pd.DataFrame(index=ctx.dates, columns=CORE7, dtype=float)
    normalized = pd.DataFrame(index=ctx.dates, columns=CORE7, dtype=float)
    oi = ctx.cont["oi_total"]
    for member in CORE7:
        seat = ctx.md[ctx.md["member"] == member].set_index("trade_date").sort_index()
        flow = seat["dnet"] / oi.reindex(seat.index)
        threshold = (
            flow.abs().rolling(250, min_periods=120).quantile(0.80).shift(1)
        )
        known = flow.notna() & seat["dlong"].notna() & seat["dshort"].notna()
        long_dominant = known & (seat["dlong"].abs() >= seat["dshort"].abs())
        build = known & (seat["dnet"] > 0) & long_dominant
        # 已知但非正向建仓=0；单腿缺失/掉榜=NaN。二者不能混为一谈。
        values = pd.Series(np.nan, index=seat.index, dtype=float)
        values.loc[known] = 0.0
        values.loc[build] = flow.loc[build]
        norm = pd.Series(np.nan, index=seat.index, dtype=float)
        norm.loc[known] = 0.0
        norm_hit = build & threshold.notna() & (threshold > 0)
        norm.loc[norm_hit] = (flow.loc[norm_hit] / threshold.loc[norm_hit]).clip(upper=3.0)
        # 已拍板的国泰君安/东证期货位置条件同样约束连续 Flow 贡献。
        if member in BASE_CONDITIONS:
            valid_place = ctx.dist60.reindex(seat.index) < BASE_CONDITIONS[member]
            # 位置条件不满足是“已知但不贡献”，不是数据缺失。
            values.loc[known & ~valid_place.fillna(False)] = 0.0
            norm.loc[known & ~valid_place.fillna(False)] = 0.0
        raw[member] = values.reindex(ctx.dates)
        normalized[member] = norm.reindex(ctx.dates)
    positive_active = (raw > 0) & (model.weight_matrix > 0)
    # 权重为0的席位不应凭一个“已知0”让整日看似可观测；未知值始终保持NaN。
    raw_weighted = (raw * model.weight_matrix).where(model.weight_matrix > 0)
    normalized_weighted = (normalized * model.weight_matrix).where(model.weight_matrix > 0)
    return raw_weighted, normalized_weighted, positive_active


def rolling_feature(raw_daily, normalized_daily, kind, window):
    if kind == "标准化累计量":
        return normalized_daily.rolling(window, min_periods=1).sum().where(normalized_daily.notna())
    if kind == "原始累计量":
        return raw_daily.rolling(window, min_periods=1).sum().where(raw_daily.notna())
    if kind == "近期加权速率":
        weights = np.arange(1.0, window + 1.0)

        def weighted_rate(values):
            w = weights[-len(values):]
            known = np.isfinite(values)
            return float(np.dot(values[known], w[known]) / w[known].sum())

        return raw_daily.rolling(window, min_periods=1).apply(
            weighted_rate, raw=True
        ).where(raw_daily.notna())
    raise ValueError(kind)


def main() -> int:
    ctx = load_context()
    baseline_model = build_event_model(ctx)
    baseline_config = TradeConfig(exact_trigger_window=True)
    baseline_trades, baseline_missed = run_trades_stateful(
        ctx, baseline_model, baseline_config, pending_policy="abandon"
    )

    raw_weighted, norm_weighted, positive_active = positive_flow_panels(ctx, baseline_model)
    raw_daily = raw_weighted.sum(axis=1, min_count=1)
    norm_daily = norm_weighted.sum(axis=1, min_count=1)
    grid = list(product(FEATURE_KINDS, FLOW_WINDOWS, FEATURE_QUANTILES, MODES))
    if len(grid) > 2000:
        raise AssertionError(f"P2 网格 {len(grid)} 超过 2000 红线")

    baseline_params = {
        "模型": "v4.3严格score5基准",
        "Flow种类": "-",
        "N": 5,
        "触发分位": 0.80,
    }
    cache = [(baseline_params, baseline_trades, baseline_missed)]
    flow_cache = []
    spec_lookup = {
        param_key(
            baseline_params["模型"],
            baseline_params["Flow种类"],
            baseline_params["N"],
            baseline_params["触发分位"],
        ): (baseline_model, baseline_config, "abandon")
    }
    full_rows = [{**baseline_params, **summarize(baseline_trades, baseline_missed)}]
    print(f"P2 受控网格：{len(grid)} 个 Flow 变体 + 1 个基准锚……", flush=True)
    for index, (kind, window, feature_q, mode) in enumerate(grid, start=1):
        feature = rolling_feature(raw_daily, norm_daily, kind, window)
        threshold = (
            feature.rolling(250, min_periods=120).quantile(feature_q).shift(1)
        )
        flow_hit = (feature > 0) & threshold.notna() & (feature >= threshold)
        if mode == "替代score5":
            entry_hit = flow_hit
        else:
            entry_hit = flow_hit & (baseline_model.score >= 6)
        signal_mask = entry_hit & (ctx.dist60 < 0.12) & (ctx.netq < 0.60)
        signals = ctx.dates[signal_mask.fillna(False)]
        active_recent = (
            positive_active.rolling(window, min_periods=1).max().astype(bool)
        )
        ratio = (feature / threshold.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
        variant_model = replace(
            baseline_model,
            event_window=window,
            active_recent=active_recent,
            score=ratio.fillna(0),
            nseat=active_recent.sum(axis=1),
            signals=signals,
        )
        # Flow 信号的成本锚取真实最近 N 个交易日内贡献席位。
        variant_config = TradeConfig(exact_trigger_window=True)
        trades, missed = run_trades_stateful(
            ctx, variant_model, variant_config, pending_policy="abandon"
        )
        params = {"模型": mode, "Flow种类": kind, "N": window, "触发分位": feature_q}
        item = (params, trades, missed)
        cache.append(item)
        flow_cache.append(item)
        spec_lookup[param_key(mode, kind, window, feature_q)] = (
            variant_model,
            variant_config,
            "abandon",
        )
        full_rows.append({**params, **summarize(trades, missed)})
        if index % 30 == 0:
            print(f"  已完成 {index}/{len(grid)}", flush=True)

    # walk_forward 仅用于因果年度选参；按年切出的缓存交易不作 OOS 绩效。
    _adaptive_blocks, adaptive_picks = walk_forward(cache, final_year=ctx.dates[-1].year)
    _flow_blocks, flow_picks = walk_forward(flow_cache, final_year=ctx.dates[-1].year)
    adaptive_specs = yearly_specs_from_picks(adaptive_picks, spec_lookup)
    flow_specs = yearly_specs_from_picks(flow_picks, spec_lookup)
    adaptive_oos, adaptive_missed = run_trades_yearly(ctx, adaptive_specs)
    flow_oos, flow_missed = run_trades_yearly(ctx, flow_specs)

    adaptive_years = set(adaptive_specs)
    flow_years = set(flow_specs)
    adaptive_baseline, adaptive_baseline_missed = run_trades_yearly(
        ctx,
        {year: (baseline_model, baseline_config, "abandon") for year in adaptive_years},
    )
    flow_baseline, flow_baseline_missed = run_trades_yearly(
        ctx,
        {year: (baseline_model, baseline_config, "abandon") for year in flow_years},
    )
    overlap_stats = {
        "含基准锚": overlap_count(adaptive_oos),
        "仅Flow候选": overlap_count(flow_oos),
        "含基准锚同期固定基准": overlap_count(adaptive_baseline),
        "仅Flow同期固定基准": overlap_count(flow_baseline),
    }
    bad_overlaps = {key: value for key, value in overlap_stats.items() if value}
    if bad_overlaps:
        raise AssertionError(f"P2 连续 OOS 出现重叠持仓：{bad_overlaps}")

    for picks, trades in ((adaptive_picks, adaptive_oos), (flow_picks, flow_oos)):
        actual_counts = (
            pd.to_datetime(trades["信号日"]).dt.year.value_counts()
            if not trades.empty else pd.Series(dtype=int)
        )
        picks["连续回放实际笔数"] = picks["年"].map(actual_counts).fillna(0).astype(int)

    adaptive_summary = summarize(adaptive_oos, adaptive_missed)
    flow_summary = summarize(flow_oos, flow_missed)
    adaptive_baseline_summary = summarize(
        adaptive_baseline, adaptive_baseline_missed
    )
    flow_baseline_summary = summarize(flow_baseline, flow_baseline_missed)
    enough = len(completed(flow_oos)) >= 20
    if not enough:
        verdict = "样本不足仅供参考：仅 Flow 候选 OOS 已完结少于20笔，不作为推荐依据。"
    elif (
        flow_summary["均收益%"] > flow_baseline_summary["均收益%"]
        and flow_summary["总收益%"] > flow_baseline_summary["总收益%"]
    ):
        verdict = "累计 Flow 的 OOS 均收益与总收益均超过同期基准，可进入最终推荐比较。"
    else:
        verdict = "累计 Flow 的 OOS 未同时改善均收益与总收益；保留 score5，不推荐替换/补充。"

    compare = pd.DataFrame(
        [
            {"口径": "P2 WF 连续OOS（允许年度回退score5）", **adaptive_summary},
            {
                "口径": "其同期 v4.3严格固定基准（P0时序）",
                **adaptive_baseline_summary,
            },
            {"口径": "P2 WF 连续OOS（仅Flow候选）", **flow_summary},
            {
                "口径": "其同期 v4.3严格固定基准（P0时序）",
                **flow_baseline_summary,
            },
            {
                "口径": "全样本：v4.3严格固定基准（P0时序，仅参考）",
                **summarize(baseline_trades, baseline_missed),
            },
        ]
    )
    full_table = pd.DataFrame(full_rows)
    full_top = full_table[full_table["已完结"] >= 20].sort_values(
        ["均收益%", "已完结"], ascending=[False, False]
    ).head(10)

    report = io.StringIO()
    report.write("== P2 持续建仓精细建模：逐年 walk-forward ==\n")
    report.write(
        f"网格={len(grid)}：Flow口径3 × N窗口5 × 动态触发分位3 × 替代/补充2。"
        "动态特征阈值固定 rolling(250,min120).quantile(q).shift(1)。\n"
    )
    report.write(
        "原始累计量衡量建仓量；标准化累计量按席位自身Q80阈值缩放；近期加权速率给予越近交易日越高线性权重。"
        "国泰君安/东证期货仍仅在距60日低点<5%时贡献。已知但非增多记0；单腿缺失/掉榜保持NaN，"
        "聚合和动态阈值均不让不可知日伪装成零观测。\n\n"
    )
    report.write(
        "walk_forward 只负责用年初前已完结交易选当年模型；主 OOS 均以逐交易日状态机跨年连续重放，"
        "新信号冻结当年模型/参数/到期放弃策略，既有挂单和持仓不因跨年重置。"
        f"含基准锚实际错过={adaptive_missed}、重叠={overlap_stats['含基准锚']}；"
        f"仅Flow实际错过={flow_missed}、重叠={overlap_stats['仅Flow候选']}；"
        f"两套同期固定基准实际错过分别={adaptive_baseline_missed}/{flow_baseline_missed}、"
        f"重叠分别={overlap_stats['含基准锚同期固定基准']}/"
        f"{overlap_stats['仅Flow同期固定基准']}。\n\n"
    )
    report.write("== OOS 对比 ==\n")
    report.write(compare.to_string(index=False))
    report.write(f"\n\n判定：{verdict}\n")
    report.write("\n== 含基准锚的逐年选择 ==\n")
    report.write(adaptive_picks.to_string(index=False))
    report.write("\n\n== 仅 Flow 候选的逐年选择 ==\n")
    report.write(flow_picks.to_string(index=False))
    report.write("\n\n== 全样本 top10（仅参考） ==\n")
    report.write(full_top.to_string(index=False))
    report.write("\n")
    text = report.getvalue()
    print(text)

    adaptive_oos.to_pickle(aulib.OUT / "v43_p2_adaptive_oos_trades.pkl")
    flow_oos.to_pickle(aulib.OUT / "v43_p2_flow_oos_trades.pkl")
    adaptive_picks.to_csv(aulib.OUT / "v43_p2_adaptive_picks.csv", index=False, encoding="utf-8-sig")
    flow_picks.to_csv(aulib.OUT / "v43_p2_flow_picks.csv", index=False, encoding="utf-8-sig")
    compare.to_csv(aulib.OUT / "v43_p2_compare.csv", index=False, encoding="utf-8-sig")
    full_table.to_csv(aulib.OUT / "v43_p2_fullsample_grid.csv", index=False, encoding="utf-8-sig")
    (aulib.OUT / "v43_p2_report.txt").write_text(text, encoding="utf-8")
    print("已写出 out/v43_p2_report.txt 及 P2 OOS/选择/网格文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
