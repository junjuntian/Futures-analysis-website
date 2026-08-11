# -*- coding: utf-8 -*-
"""v4.3 P1：事件分位、阈值窗口、事件窗口的逐年 walk-forward 寻优。"""
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
)


# 默认锚点放在枚举首位；保守分完全相等时照 run_rulesearch.py 保留首个，
# 即无证据时回退严格固定基准参数，而不是让枚举顺序制造漂移。
QUANTILES = [0.80, 0.75, 0.85, 0.70, 0.90]
THRESHOLD_WINDOWS = [250, 180, 375, 120, 500]
EVENT_WINDOWS = [5, 3, 7, 10]
BASE_PARAMS = {"分位": 0.80, "阈值窗": 250, "事件窗": 5}


def param_key(quantile, threshold_window, event_window):
    return (round(float(quantile), 8), int(threshold_window), int(event_window))


def main() -> int:
    grid = list(product(QUANTILES, THRESHOLD_WINDOWS, EVENT_WINDOWS))
    if len(grid) > 2000:
        raise AssertionError(f"P1 网格 {len(grid)} 超过 2000 红线")

    ctx = load_context()
    # v4.3 严格口径统一使用最近 5 个交易日识别成本触发席位。
    trade_config = TradeConfig(exact_trigger_window=True)
    cache = []
    spec_lookup = {}
    full_rows = []
    baseline_trades = None
    baseline_missed = None
    baseline_model = None
    print(f"P1 受控网格：{len(grid)} 组合；逐组合缓存全期逐笔……", flush=True)
    for index, (quantile, threshold_window, event_window) in enumerate(grid, start=1):
        model = build_event_model(
            ctx,
            q=quantile,
            threshold_window=threshold_window,
            event_window=event_window,
        )
        # 年度选择的训练轨迹也按已拍板的“有效期到期即放弃”语义生成。
        trades, missed = run_trades_stateful(
            ctx, model, trade_config, pending_policy="abandon"
        )
        params = {"分位": quantile, "阈值窗": threshold_window, "事件窗": event_window}
        cache.append((params, trades, missed))
        spec_lookup[param_key(quantile, threshold_window, event_window)] = (
            model,
            trade_config,
            "abandon",
        )
        full_rows.append({**params, **summarize(trades, missed)})
        if params == BASE_PARAMS:
            baseline_trades, baseline_missed = trades.copy(), missed
            baseline_model = model
        if index % 20 == 0:
            print(f"  已完成 {index}/{len(grid)}", flush=True)

    if baseline_trades is None or baseline_model is None:
        raise AssertionError("P1 网格未包含严格固定基准参数")

    # walk_forward 只负责在每个年初用当时可见的已完结交易选参；其按年切片
    # 的返回轨迹不作绩效口径。真正 OOS 用一条逐日状态机跨年连续重放。
    _selection_blocks, picks = walk_forward(cache, final_year=ctx.dates[-1].year)
    selected = picks[picks["状态"] == "已选择"]
    selected_specs = {
        int(row["年"]): spec_lookup[
            param_key(row["分位"], row["阈值窗"], row["事件窗"])
        ]
        for _, row in selected.iterrows()
    }
    oos, oos_missed = run_trades_yearly(ctx, selected_specs)
    overlaps = overlap_count(oos)
    if overlaps:
        raise AssertionError(f"P1 连续 OOS 出现 {overlaps} 笔单仓位重叠")
    selected_year_set = set(selected["年"].astype(int))
    baseline_specs = {
        year: (baseline_model, trade_config, "abandon") for year in selected_year_set
    }
    baseline_same_years, baseline_oos_missed = run_trades_yearly(ctx, baseline_specs)
    baseline_overlaps = overlap_count(baseline_same_years)
    if baseline_overlaps:
        raise AssertionError(f"P1 同期固定基准连续回放出现 {baseline_overlaps} 笔重叠")

    actual_counts = (
        pd.to_datetime(oos["信号日"]).dt.year.value_counts()
        if not oos.empty else pd.Series(dtype=int)
    )
    picks["连续回放实际笔数"] = picks["年"].map(actual_counts).fillna(0).astype(int)

    oos_summary = summarize(oos, oos_missed)
    baseline_oos_summary = summarize(baseline_same_years, baseline_oos_missed)
    baseline_all_summary = summarize(baseline_trades, baseline_missed)

    selected_years = (
        f"{int(selected['年'].min())}-{int(selected['年'].max())}" if len(selected) else "无"
    )
    enough = len(completed(oos)) >= 20
    if not enough:
        verdict = "样本不足仅供参考：OOS 已完结样本少于20笔，不得作为推荐依据。"
    elif (
        oos_summary["均收益%"] > baseline_oos_summary["均收益%"]
        and oos_summary["总收益%"] > baseline_oos_summary["总收益%"]
    ):
        verdict = "OOS 样本达到20笔且均收益、总收益均超过同期基准，可进入最终推荐比较。"
    else:
        verdict = "OOS 样本达到20笔，但均收益或总收益跑输同期固定参数；P1 不改默认参数。"

    compare = pd.DataFrame(
        [
            {"口径": f"P1 WF 连续 OOS（{selected_years}）", **oos_summary},
            {
                "口径": f"同期 v4.3严格固定基准（P0时序，{selected_years}）",
                **baseline_oos_summary,
            },
            {
                "口径": "全样本：v4.3严格固定基准（P0时序，仅参考）",
                **baseline_all_summary,
            },
        ]
    )

    full_table = pd.DataFrame(full_rows)
    full_done = full_table[full_table["已完结"] >= 20].copy()
    if len(full_done):
        full_top = full_done.sort_values(
            ["均收益%", "已完结"], ascending=[False, False]
        ).head(10)
    else:
        full_top = full_table.head(0)

    report = io.StringIO()
    report.write("== P1 事件参数寻优：逐年 walk-forward，仅 OOS 作结论 ==\n")
    report.write(
        f"网格={len(grid)}（分位5 × 阈值窗5 × 事件窗4，<=2000）；"
        "训练评分照 run_rulesearch.py：仅用年初前已完结交易，均收益减一个标准误，最少12笔。\n"
    )
    report.write("阈值由 rolling(...).quantile(...).shift(1) 生成；权重逐年扩窗且只用已实现 fwd20。\n\n")
    report.write(
        "walk_forward 年度选择只使用年初前已完结交易；绩效不再按年切缓存交易。"
        "主 OOS 逐交易日跨年连续回放，信号生成时冻结当年模型/交易参数/到期放弃策略，"
        "已有挂单和持仓跨年延续。"
        f"实际错过信号={oos_missed}，持仓重叠={overlaps}；同期固定基准实际错过信号="
        f"{baseline_oos_missed}，持仓重叠={baseline_overlaps}。\n\n"
    )
    report.write("== OOS 对比 ==\n")
    report.write(compare.to_string(index=False))
    report.write(f"\n\n判定：{verdict}\n")
    report.write("\n== 每年选择（当年数字不参与当年选择） ==\n")
    report.write(picks.to_string(index=False))
    report.write("\n\n== 全样本 top10（仅参考，禁止作为结论） ==\n")
    report.write(full_top.to_string(index=False))
    report.write("\n")

    text = report.getvalue()
    print(text)
    oos.to_pickle(aulib.OUT / "v43_p1_oos_trades.pkl")
    picks.to_csv(aulib.OUT / "v43_p1_wf_picks.csv", index=False, encoding="utf-8-sig")
    compare.to_csv(aulib.OUT / "v43_p1_compare.csv", index=False, encoding="utf-8-sig")
    full_table.to_csv(aulib.OUT / "v43_p1_fullsample_grid.csv", index=False, encoding="utf-8-sig")
    (aulib.OUT / "v43_p1_report.txt").write_text(text, encoding="utf-8")
    print("已写出 out/v43_p1_report.txt、v43_p1_wf_picks.csv、v43_p1_oos_trades.pkl。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
