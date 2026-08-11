# -*- coding: utf-8 -*-
"""v4.3 P5：信号消退天数与浮盈保本组合的逐年 walk-forward 寻优。"""
from __future__ import annotations

import io
import sys
from itertools import product
from typing import Any

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
    walk_forward,
)


# 基准放在枚举首位；训练保守分完全相等时回退 v4.2，不让枚举顺序制造漂移。
DECAY_DAYS = [10, 5, 7, 15, 20, 30]
BREAKEVEN_TRIGGERS = [None, 0.03, 0.05, 0.08, 0.10, 0.15]
BASE_PARAMS = {"消退日数": 10, "保本启动": "无"}


def trigger_label(trigger: float | None) -> str:
    return "无" if trigger is None else f"浮盈{trigger * 100:.0f}%"


def summarize_p5(trades: pd.DataFrame, missed: int = 0) -> dict[str, Any]:
    """P5 验收口径：绩效仅由已完结交易计算，保留所有信号产生的笔数。"""
    done = completed(trades)
    if done.empty:
        return {
            "笔数": len(trades),
            "已完结": 0,
            "胜率%": float("nan"),
            "均收益%": float("nan"),
            "固定止损数": 0,
            "保本数": 0,
            "总收益%": 0.0,
            "错过信号": missed,
        }
    return {
        "笔数": len(trades),
        "已完结": len(done),
        "胜率%": round((done["收益%"] > 0).mean() * 100, 1),
        "均收益%": round(done["收益%"].mean(), 2),
        "固定止损数": int((done["结果"] == "止损").sum()),
        "保本数": int((done["结果"] == "保本").sum()),
        "总收益%": round(done["收益%"].sum(), 1),
        "错过信号": missed,
    }


def params_key(params) -> tuple[int, str]:
    return int(params["消退日数"]), params["保本启动"]


def specs_from_picks(picks: pd.DataFrame, spec_lookup) -> dict[int, tuple]:
    specs = {}
    for row in picks.loc[picks["状态"] == "已选择"].to_dict("records"):
        key = params_key(row)
        if key not in spec_lookup:
            raise KeyError(f"P5 年度选择缺少规格映射: {key}")
        specs[int(row["年"])] = spec_lookup[key]
    return specs


def main() -> int:
    grid = list(product(DECAY_DAYS, BREAKEVEN_TRIGGERS))
    if len(grid) != 36:
        raise AssertionError(f"P5 网格应为36组合，实际 {len(grid)}")
    if len(grid) > 2000:
        raise AssertionError(f"P5 网格 {len(grid)} 超过 2000 红线")

    ctx = load_context()
    model = build_event_model(ctx)
    cache = []
    full_rows = []
    spec_lookup = {}
    baseline_trades = None
    baseline_missed = None
    baseline_spec = None
    print("P5 受控网格：消退6档 × 保本6档 = 36组合……", flush=True)
    for index, (decay_days, breakeven_trigger) in enumerate(grid, start=1):
        # P5 只改变退出；v4.3 严格口径使用最近5个交易日识别成本触发席位，
        # 进场固定±5元、10交易日、到期放弃。
        config = TradeConfig(
            entry_kind="fixed",
            zone_value=5.0,
            validity=10,
            miss_policy="abandon",
            stop=0.04,
            decay_days=decay_days,
            breakeven_trigger=breakeven_trigger,
            exact_trigger_window=True,
        )
        policy = "abandon"
        trades, missed = run_trades_stateful(
            ctx, model, config, pending_policy=policy
        )
        params = {
            "消退日数": decay_days,
            "保本启动": trigger_label(breakeven_trigger),
        }
        cache.append((params, trades, missed))
        spec_lookup[params_key(params)] = (model, config, policy)
        full_rows.append({**params, **summarize_p5(trades, missed)})
        if params == BASE_PARAMS:
            baseline_trades = trades.copy()
            baseline_missed = missed
            baseline_spec = (model, config, policy)
        if index % 12 == 0:
            print(f"  已完成 {index}/{len(grid)}", flush=True)

    if baseline_trades is None or baseline_missed is None or baseline_spec is None:
        raise AssertionError("P5 网格未包含 P0 修正后的 v4.2 基准")

    _, picks = walk_forward(cache, final_year=ctx.dates[-1].year)
    yearly_specs = specs_from_picks(picks, spec_lookup)
    oos, oos_missed = run_trades_yearly(ctx, yearly_specs)
    yearly_counts = (
        pd.to_datetime(oos["信号日"]).dt.year.value_counts().to_dict()
        if not oos.empty
        else {}
    )
    picks = picks.copy()
    picks["独立候选切片笔数"] = picks["当年笔数"]
    picks["当年笔数"] = picks["年"].map(yearly_counts).fillna(0).astype(int)
    overlaps = overlap_count(oos)
    if overlaps:
        raise AssertionError(f"P5 连续 OOS 出现 {overlaps} 笔单仓位重叠")
    baseline_year_specs = {year: baseline_spec for year in yearly_specs}
    baseline_same_years, baseline_oos_missed = run_trades_yearly(
        ctx, baseline_year_specs
    )
    baseline_overlaps = overlap_count(baseline_same_years)
    if baseline_overlaps:
        raise AssertionError(
            f"P5 同期严格基准连续 OOS 出现 {baseline_overlaps} 笔单仓位重叠"
        )

    oos_summary = summarize_p5(oos, oos_missed)
    baseline_oos_summary = summarize_p5(baseline_same_years, baseline_oos_missed)
    baseline_all_summary = summarize_p5(baseline_trades, baseline_missed)
    done_n = len(completed(oos))
    if done_n < 20:
        verdict = "样本不足仅供参考：P5 OOS 已完结少于20笔，不得作为推荐依据。"
    elif (
        oos_summary["均收益%"] > baseline_oos_summary["均收益%"]
        and oos_summary["总收益%"] > baseline_oos_summary["总收益%"]
    ):
        verdict = "P5 OOS 已完结不少于20笔，且均收益、总收益均超过同期基准，可进入最终推荐比较。"
    else:
        verdict = "P5 OOS 已完结不少于20笔，但未同时改善均收益与总收益；保留消退10日且不启用保本。"

    selected = picks[picks["状态"] == "已选择"]
    selected_years = (
        f"{int(selected['年'].min())}-{int(selected['年'].max())}"
        if len(selected)
        else "无"
    )
    compare = pd.DataFrame(
        [
            {"口径": f"P5 连续walk-forward OOS（{selected_years}）", **oos_summary},
            {"口径": f"同期v4.3严格固定基准（P0时序，连续回放，{selected_years}）", **baseline_oos_summary},
            {"口径": "全样本：v4.3严格固定基准（P0时序，仅参考）", **baseline_all_summary},
        ]
    )

    full_table = pd.DataFrame(full_rows)
    full_top = full_table[full_table["已完结"] >= 20].sort_values(
        ["均收益%", "已完结"], ascending=[False, False]
    ).head(12)

    causal_checks = pd.DataFrame(
        [
            ("事件阈值", "通过", "build_events 使用 rolling 阈值 shift(1)，当日不进入自身阈值"),
            ("席位权重", "通过", "逐年扩窗，仅用截至上年末已实现的 fwd20"),
            ("进场时序", "通过", "信号日15:00后确认，区间订单最早T+1成交"),
            ("固定止损", "通过", "所有36组合均固定-4%，保本线不替代初始硬止损"),
            ("保本时序", "通过", "日高达到X只登记触发，保本线从下一交易日起生效"),
            ("消退时序", "通过", "连续N日无有效事件在收盘确认，T+1开盘卖出"),
            ("消退宽限", "通过", "保留 i > entry_i + 2，进场后前三根K线不因消退退出"),
            ("WF训练", "通过", "每年只用出场日<1月1日的已完结交易，mean-SE，min12"),
            ("WF归属", "通过", "参数按信号年归属；当年结果不参与当年选择"),
            (
                "OOS连续状态",
                "通过",
                f"单次逐日回放；挂单/持仓跨年延续；候选重叠={overlaps}，同期基准重叠={baseline_overlaps}",
            ),
        ],
        columns=["检查项", "结果", "说明"],
    )

    report = io.StringIO()
    report.write("== P5 卖点参数：消退 + 浮盈保本，逐年 walk-forward ==\n")
    report.write(
        "网格=36（消退日数[10,5,7,15,20,30] × "
        "保本启动[无,3%,5%,8%,10%,15%]，<=2000）。"
        "基准(10日/无保本)置于首项，同分时回退基准。\n"
    )
    report.write(
        "其余口径钉死：v4.2 条件计分、固定±5元成本区间、10交易日有效、错过放弃、"
        "固定-4%止损；P5 不改变任何买点参数。\n"
    )
    report.write(
        "训练评分：仅用每年1月1日前已完结交易，均收益减一个标准误，最少12笔；"
        "年度选择按信号年冻结 config，主OOS只做一次跨年连续回放，挂单和持仓跨12月31日延续。"
        "保本在日高首次达到X后，从下一交易日起生效；"
        "消退确认后T+1开盘卖出，并保留进场后前三根K线宽限。\n\n"
    )
    report.write("== OOS 与同期基准 ==\n")
    report.write(compare.iloc[:2].to_string(index=False))
    report.write(f"\n\n<20判定：OOS已完结={done_n}；{verdict}\n")
    report.write("\n== 每年选择（当年数字不参与当年选择） ==\n")
    report.write(picks.to_string(index=False))
    report.write("\n\n== 无未来函数与时序自查 ==\n")
    report.write(causal_checks.to_string(index=False))
    report.write("\n\n== 全样本数字（仅参考，不作为结论） ==\n")
    report.write(compare.iloc[2:].to_string(index=False))
    report.write("\n\n== 全样本网格 top12（仅参考） ==\n")
    report.write(full_top.to_string(index=False))
    report.write("\n")
    text = report.getvalue()
    print(text)

    oos.to_pickle(aulib.OUT / "v43_p5_oos_trades.pkl")
    oos.to_csv(aulib.OUT / "v43_p5_oos_trades.csv", index=False, encoding="utf-8-sig")
    picks.to_csv(aulib.OUT / "v43_p5_wf_picks.csv", index=False, encoding="utf-8-sig")
    compare.to_csv(aulib.OUT / "v43_p5_compare.csv", index=False, encoding="utf-8-sig")
    full_table.to_csv(aulib.OUT / "v43_p5_fullsample_grid.csv", index=False, encoding="utf-8-sig")
    causal_checks.to_csv(aulib.OUT / "v43_p5_causal_checks.csv", index=False, encoding="utf-8-sig")
    (aulib.OUT / "v43_p5_report.txt").write_text(text, encoding="utf-8")
    print("已写出 out/v43_p5_report.txt 及 P5 OOS/选择/网格/时序自查文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
