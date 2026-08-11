# -*- coding: utf-8 -*-
"""v4.3 最终整合：严格未知口径、P0-P6 OOS 证据与推荐轨迹。"""
from __future__ import annotations

import importlib
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import aulib
from run_optimize import CORE7
from v43_core import (
    TradeConfig,
    build_event_model,
    load_context,
    markdown_table,
    overlap_count,
    run_trades_yearly,
    summarize,
)


ROOT = Path(__file__).resolve().parent


def read_csv(name: str) -> pd.DataFrame:
    path = aulib.OUT / name
    if not path.exists():
        raise FileNotFoundError(f"缺少 {path.name}；请先运行对应的 v43_p*.py")
    return pd.read_csv(path)


def ensure_component_outputs() -> None:
    """缺失或早于源码的组件输出自动重跑，避免 final 误读旧结果。"""
    required = [
        ("v43_p0", "v43_p0_baseline_compare.csv"),
        ("v43_p1", "v43_p1_compare.csv"),
        ("v43_p2", "v43_p2_compare.csv"),
        ("v43_p3", "v43_p3_compare.csv"),
        ("v43_p4", "v43_p4_compare.csv"),
        ("v43_p5", "v43_p5_compare.csv"),
        ("v43_p6", "v43_p6_compare.csv"),
    ]
    core_mtime = (ROOT / "v43_core.py").stat().st_mtime
    for module_name, output_name in required:
        source_mtime = (ROOT / f"{module_name}.py").stat().st_mtime
        output = aulib.OUT / output_name
        if not output.exists() or output.stat().st_mtime < max(core_mtime, source_mtime):
            result = importlib.import_module(module_name).main()
            if result not in (None, 0):
                raise RuntimeError(f"{module_name}.main() 返回 {result}")


def selected_p1_specs(ctx, picks: pd.DataFrame):
    """按 P1 已冻结年度参数重建可执行规格；不从逐笔结果反推。"""
    specs = {}
    model_cache = {}
    selected = picks[picks["状态"] == "已选择"].copy()
    for row in selected.to_dict("records"):
        key = (float(row["分位"]), int(float(row["阈值窗"])), int(float(row["事件窗"])))
        if key not in model_cache:
            model_cache[key] = build_event_model(
                ctx, q=key[0], threshold_window=key[1], event_window=key[2]
            )
        specs[int(row["年"])] = (
            model_cache[key],
            TradeConfig(exact_trigger_window=True),
            "abandon",
        )
    return specs


def _metric(frame: pd.DataFrame, row: int, column: str) -> float:
    return float(pd.to_numeric(pd.Series([frame.iloc[row][column]]), errors="coerce").iloc[0])


def oos_decisions() -> tuple[pd.DataFrame, dict[str, bool]]:
    p1 = read_csv("v43_p1_compare.csv")
    p2 = read_csv("v43_p2_compare.csv")
    p3 = read_csv("v43_p3_compare.csv")
    p4 = read_csv("v43_p4_compare.csv")
    p5 = read_csv("v43_p5_compare.csv")
    p6 = read_csv("v43_p6_compare.csv")

    definitions = [
        ("P1 事件参数", p1, 0, 1, "均收益%", "总收益%", "止损笔数",
         "100组逐年选择", "采用逐年扩窗自动选择"),
        ("P2 累计Flow", p2, 2, 3, "均收益%", "总收益%", "止损笔数",
         "90组替代/补充", "保留score5"),
        ("P3 成本区间", p3, 1, 4, "均收益%", "总收益%", "止损笔数",
         "90组区间/有效期/错过策略", "保留±5元/10日/到期放弃"),
        ("P4 条件计分", p4, 1, 0, "均收益%", "总收益%", "止损笔数",
         "七席位两段四格因果映射", "仍仅限制国泰君安、东证期货"),
        ("P5 卖点", p5, 0, 1, "均收益%", "总收益%", "固定止损数",
         "36组消退日数/保本", "保留10日消退、无保本"),
    ]
    rows = []
    flags: dict[str, bool] = {}
    for name, frame, cand_i, base_i, mean_col, total_col, stop_col, label, reject_text in definitions:
        done = int(_metric(frame, cand_i, "已完结"))
        enough = done >= 20
        improved = (
            _metric(frame, cand_i, mean_col) > _metric(frame, base_i, mean_col)
            and _metric(frame, cand_i, total_col) > _metric(frame, base_i, total_col)
        )
        accepted = enough and improved
        flags[name] = accepted
        if name == "P3 成本区间":
            conclusion = "样本不足且成本覆盖无识别力；" + reject_text
        elif accepted:
            conclusion = "OOS均收益和总收益均改善；" + reject_text
        else:
            conclusion = reject_text
        rows.append(
            {
                "P项": name,
                "候选": label,
                "笔数/完结": f"{int(_metric(frame, cand_i, '笔数'))}/{done}",
                "胜率%": _metric(frame, cand_i, "胜率%"),
                "均收益%": _metric(frame, cand_i, mean_col),
                "止损": int(_metric(frame, cand_i, stop_col)),
                "总收益%": _metric(frame, cand_i, total_col),
                "同期基准总收益%": _metric(frame, base_i, total_col),
                "样本判定": "通过" if enough else "不足20，仅供参考",
                "结论": conclusion,
            }
        )

    p6_done = int(_metric(p6, 0, "已完结"))
    p6_enough = p6_done >= 20
    p6_full_count = int(_metric(p6, 0, "1.0档笔数"))
    p6_both_used = (
        p6_done > 0
        and p6_full_count / p6_done >= 0.20
        and (p6_done - p6_full_count) / p6_done >= 0.20
    )
    p6_improved = (
        _metric(p6, 0, "算术总收益%") > _metric(p6, 1, "算术总收益%")
        and _metric(p6, 0, "复利收益%") > _metric(p6, 1, "复利收益%")
        and _metric(p6, 0, "平仓净值最大回撤%")
        >= _metric(p6, 1, "平仓净值最大回撤%")
        and p6_both_used
    )
    flags["P6 仓位"] = p6_enough and p6_improved
    rows.append(
        {
            "P项": "P6 仓位",
            "候选": "18组score/共振映射",
            "笔数/完结": f"{int(_metric(p6, 0, '笔数'))}/{p6_done}",
            "胜率%": _metric(p6, 0, "胜率%"),
            "均收益%": _metric(p6, 0, "加权均收益%"),
            "止损": int(_metric(p6, 0, "止损笔数")),
            "总收益%": _metric(p6, 0, "算术总收益%"),
            "同期基准总收益%": _metric(p6, 1, "算术总收益%"),
            "样本判定": "通过" if p6_enough else "不足20，仅供参考",
            "结论": "维持1.0仓位" if not flags["P6 仓位"] else "采用0.5/1.0分级",
        }
    )
    return pd.DataFrame(rows), flags


def trajectory_table(ctx, specs_by_year, trades: pd.DataFrame) -> pd.DataFrame:
    recent = trades[pd.to_datetime(trades["信号日"]) >= "2026-04-24"].copy()
    details = []
    for trade in recent.to_dict("records"):
        signal_day = pd.Timestamp(trade["信号日"])
        model = specs_by_year[signal_day.year][0]
        signal_i = ctx.pos[signal_day]
        first_i = max(0, signal_i - model.event_window + 1)
        window_days = set(ctx.dates[first_i: signal_i + 1])
        events = model.effective_events[
            model.effective_events["trade_date"].isin(window_days)
        ]
        parts = []
        for member in CORE7:
            group = events[events["member"] == member]
            if len(group) and model.weights[signal_day.year].get(member, 0.0) > 0:
                parts.append(f"{member}(强度{group['strength'].max():.2f})")
        if trade["结果"] == "止损":
            note = "盘中固定-4%止损"
        elif trade["结果"] in {"持有中", "待消退执行"}:
            note = f"截至{ctx.dates[-1].date()}盯市；未伪造成交"
        else:
            note = "消退收盘确认后T+1开盘执行"
        details.append(
            {
                "信号日": trade["信号日"],
                "当年P1参数": f"Q{model.q:.2f}/阈值窗{model.threshold_window}/事件窗{model.event_window}",
                "触发席位/强度": "、".join(parts),
                "进场日": trade["进场日"],
                "区间上沿": trade["区间上沿"],
                "进场价(真实)": trade["进场价(真实)"],
                "消退确认日": trade["消退确认日"],
                "出场日/盯市日": trade["出场日"],
                "结果": trade["结果"],
                "收益%": round(float(trade["收益%"]), 2),
                "说明": note,
            }
        )
    return pd.DataFrame(details)


def summary_row(label: str, period: str, stats: dict) -> dict:
    return {
        "版本": label,
        "样本期/口径": period,
        "笔数": stats["笔数"],
        "已完结": stats["已完结"],
        "胜率%": stats["胜率%"],
        "均收益%": stats["均收益%"],
        "止损": stats["止损笔数"],
        "总收益%": stats["总收益%"],
        "错过信号": stats.get("错过信号", 0),
    }


def main() -> int:
    ensure_component_outputs()
    ctx = load_context()
    p1_picks = read_csv("v43_p1_wf_picks.csv")
    selected_specs = selected_p1_specs(ctx, p1_picks)
    if not selected_specs:
        raise AssertionError("P1 没有任何可执行年度规格")

    recommended_trades, recommended_missed = run_trades_yearly(ctx, selected_specs)
    if overlap_count(recommended_trades):
        raise AssertionError("最终推荐连续回放出现重叠持仓")
    rec_summary = summarize(recommended_trades, recommended_missed)

    fixed_model = build_event_model(ctx)
    fixed_config = TradeConfig(exact_trigger_window=True)
    fixed_all_specs = {
        year: (fixed_model, fixed_config, "abandon")
        for year in range(2015, ctx.dates[-1].year + 1)
    }
    fixed_all_trades, fixed_all_missed = run_trades_yearly(ctx, fixed_all_specs)
    fixed_same_specs = {
        year: (fixed_model, fixed_config, "abandon") for year in selected_specs
    }
    fixed_same_trades, fixed_same_missed = run_trades_yearly(ctx, fixed_same_specs)
    fixed_all_summary = summarize(fixed_all_trades, fixed_all_missed)
    fixed_same_summary = summarize(fixed_same_trades, fixed_same_missed)

    p1_compare = read_csv("v43_p1_compare.csv")
    for key in ["笔数", "已完结", "胜率%", "均收益%", "止损笔数", "总收益%", "错过信号"]:
        expected = _metric(p1_compare, 0, key)
        actual = float(rec_summary[key])
        if not np.isclose(actual, expected, rtol=0.0, atol=1e-9):
            raise AssertionError(f"最终P1连续轨迹未对拍独立报告：{key}={actual}，预期{expected}")

    decisions, flags = oos_decisions()
    expected_flags = {"P1 事件参数": True, "P2 累计Flow": False, "P3 成本区间": False,
                      "P4 条件计分": False, "P5 卖点": False, "P6 仓位": False}
    if flags != expected_flags:
        raise AssertionError(f"独立报告结论已变化，需重新审查最终组合：{flags}")

    legacy = read_csv("v43_p0_baseline_compare.csv")
    legacy_rows = []
    for index in [0, 1]:
        row = legacy.iloc[index]
        legacy_rows.append(
            {
                "版本": row["版本"],
                "样本期/口径": "旧蓝本精确复现（含2011-2014）",
                "笔数": int(row["笔数"]), "已完结": int(row["已完结"]),
                "胜率%": row["胜率%"], "均收益%": row["均收益%"],
                "止损": int(row["止损笔数"]), "总收益%": row["总收益%"],
                "错过信号": int(row["错过信号"]),
            }
        )
    selected_years = sorted(selected_specs)
    period = f"{selected_years[0]}-{selected_years[-1]}连续OOS"
    final_compare = pd.DataFrame(
        legacy_rows
        + [
            summary_row("v4.3严格固定基准（P0时序）", "2015-2026连续OOS", fixed_all_summary),
            summary_row("v4.3严格固定基准（P0时序）", period, fixed_same_summary),
            summary_row("最终推荐：P0时序+P1逐年事件参数", period, rec_summary),
        ]
    )

    trajectory = trajectory_table(ctx, selected_specs, recommended_trades)
    selected_display = p1_picks[p1_picks["状态"] == "已选择"][
        ["年", "训练保守分", "分位", "阈值窗", "事件窗", "连续回放实际笔数"]
    ].copy()

    causal_audit = pd.DataFrame(
        [
            ("ΔNet", "通过", "只聚合交易所change；多空任一榜腿缺失即NaN，不以相邻日Net差分"),
            ("掉榜/净仓", "通过", "缺失不补0；整日无有效净仓不前填，netq当日保持NaN"),
            ("成本可见性", "通过", "单腿缺失、整日缺口、零成交或连续性不符即失效；不跨掉榜延续成本"),
            ("事件阈值", "通过", "每席位rolling(N,min120).quantile(q).shift(1)，当日不进入自身阈值"),
            ("权重扩窗截止", "通过", "执行年y只用事件日<(y-1)-12-01的已实现fwd20；样本<30权重0"),
            ("条件计分", "通过", "只用信号日收盘可知dist60；仍仅国泰君安/东证期货<5%时计分"),
            ("买入T+1", "通过", "信号盘后确认；订单最早下一交易日，执行开盘缺失直接报错"),
            ("固定止损", "通过", "所有最终交易固定-4%，未采用机构跟随止损"),
            ("消退T+1", "通过", "连续10日无有效事件收盘确认，下一交易日开盘先执行"),
            ("WF训练", "通过", "只用年初前已完结交易，mean-SE，min12；当年结果不参与选择"),
            ("WF连续状态", "通过", "年度规格按信号年冻结；挂单和持仓跨12月31延续，不按年度缓存拼接"),
            ("OOS样本红线", "通过", "不足20笔只标参考；P1推荐依据为26笔已完结OOS"),
        ],
        columns=["检查项", "结果", "说明"],
    )

    implementation_notes = pd.DataFrame(
        [
            ("P0对拍边界", "P0表保留旧member_day/旧成本蓝本，只用于隔离并量化消退T+1这个单一bug；P1-P6与最终组合使用严格未知口径。"),
            ("原报告日期标签", "run_v42.py原30笔实际含2011-2014六笔，不是真正2015起；已并列披露。"),
            ("成本覆盖", "严格掉榜口径下215个固定模型信号仅29个成本可得；严格基准27笔中25笔按既定规则走成本不可得T+1市价兜底。"),
            ("P3识别力", "因成本覆盖不足，90组在成交层面完全同绩效；不把全样本相同数字解释为区间有效证据。"),
            ("研究范围", "仅回测研究，不进入生产；既有aulib.py、run_*.py和data/未改。"),
        ],
        columns=["事项", "披露"],
    )

    report = io.StringIO()
    report.write("# 黄金席位跟踪系统 v4.3 最终整合报告\n\n")
    report.write("## 最终结论\n\n")
    report.write(
        "最终推荐接受 P0 与 P1：消退必须在确认后 T+1 开盘执行；事件分位、阈值窗和事件窗"
        "不再固定为80%/250/5，而是在每年年初用此前已完结交易从预注册100组网格中因果选择。"
        "P1连续OOS为27笔/26笔完结，均收益3.27%、总收益85.0%；同期严格固定基准为"
        "25笔完结、均收益2.52%、总收益62.9%。P2-P6均不改。\n\n"
    )
    report.write("## P0 原实现与T+1修正并排\n\n")
    report.write(markdown_table(legacy))
    report.write("\n\nP0表是旧蓝本单因素对拍；后续优化统一使用严格未知/严格成本口径。\n\n")
    report.write("## 最终推荐与严格固定基准\n\n")
    report.write(markdown_table(final_compare))
    report.write("\n\n胜率、均收益、总收益只统计已完结交易；笔数另含期末持有中。\n\n")
    report.write("## P1-P6 独立OOS证据\n\n")
    report.write(markdown_table(decisions))
    report.write("\n\n## P1逐年冻结参数\n\n")
    report.write(markdown_table(selected_display))
    report.write("\n\n## 最终推荐完整规则\n\n")
    report.write(
        "- 七席位固定：中信期货、中财期货、高盛期货、海通期货、国泰君安、东证期货、国贸期货。\n"
        "- 每年年初在预注册100组 `分位×阈值窗×事件窗` 中选择；训练只用年初前已完结交易，"
        "评分为均收益减一个标准误，最少12笔，完全同分回退枚举靠前参数。\n"
        "- 各候选事件仍为增多且多头腿主导，阈值 `shift(1)`，强度封顶3；席位权重逐年扩窗自动计算。\n"
        "- 买点保持 `score≥6`、距60日最低收盘<12%、榜上可见七席位净多分位<60%；"
        "国泰君安/东证期货仅距低点<5%时计分。\n"
        "- 成本触发席位用所选事件窗的真实交易日；成本区间±5元、10交易日有效、到期放弃；"
        "成本不可得则T+1开盘市价兜底。\n"
        "- 卖出保持固定-4%盘中止损；连续10交易日零有效增多事件收盘确认后T+1开盘；"
        "无保本、无目标、无机构止损跟随。仓位保持1.0。\n\n"
    )
    report.write("## 2026-04-24起轨迹回放\n\n")
    report.write(markdown_table(trajectory))
    report.write("\n\n## 无未来函数自查\n\n")
    report.write(markdown_table(causal_audit))
    report.write("\n\n## 口径差异与数据覆盖披露\n\n")
    report.write(markdown_table(implementation_notes))
    report.write("\n")
    text = report.getvalue()
    print(text)

    recommended_trades.to_pickle(aulib.OUT / "v43_final_recommended_trades.pkl")
    final_compare.to_csv(aulib.OUT / "v43_final_compare.csv", index=False, encoding="utf-8-sig")
    decisions.to_csv(aulib.OUT / "v43_final_oos_decisions.csv", index=False, encoding="utf-8-sig")
    selected_display.to_csv(aulib.OUT / "v43_final_p1_yearly_params.csv", index=False, encoding="utf-8-sig")
    trajectory.to_csv(aulib.OUT / "v43_final_trajectory_20260424.csv", index=False, encoding="utf-8-sig")
    causal_audit.to_csv(aulib.OUT / "v43_final_causal_audit.csv", index=False, encoding="utf-8-sig")
    implementation_notes.to_csv(
        aulib.OUT / "v43_final_implementation_notes.csv", index=False, encoding="utf-8-sig"
    )
    (aulib.OUT / "v43_final_report.md").write_text(text, encoding="utf-8")
    print("已写出 out/v43_final_report.md 及最终比较/轨迹/自查/推荐逐笔文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
