# -*- coding: utf-8 -*-
"""v4.3 P4：七席位位置条件诊断与逐年因果条件映射。

本脚本只改变“增多事件是否计入分数”的位置条件，不改变七席位、事件阈值、
逐年扩窗权重、score 门槛、净仓分位、成本区间、有效期、4% 止损或消退天数。

预注册判据（看结果前固定）：
1. 每席位分别统计 <2020 / >=2020 两段，以及 dist60<5% / >=5% 两层；
2. 四格均 N>=20，且两段的“贴低点均值减离低点均值”严格同号，才称证据一致；
3. 原无条件席位仅在两段差值均为正时新增 dist60<5% 条件；
4. 国泰君安/东证期货仅在两段差值均为负时移除原条件，否则保留。

OOS 映射在每年年初重算，只使用该年以前且 fwd20 已完整实现的事件；若
[2020, year) 第二段任一层 N<20，则保持该席位 v4.2 原条件。交易由 v43_core
的 v4.3 严格引擎重放，信号消退确认后严格 T+1 开盘卖出。
"""
from __future__ import annotations

import io
import sys
from typing import Any, Mapping

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
    run_trades_stateful,
    summarize,
)


WF_START = 2015
SPLIT_DATE = pd.Timestamp("2020-01-01")
POSITION_LIMIT = 0.05
MIN_CELL_N = 20

PERIOD_PRE = "<2020"
PERIOD_POST = ">=2020"
POSITION_LOW = "贴60日低点(<5%)"
POSITION_AWAY = "离60日低点(>=5%)"

ORIGINAL_CONDITIONAL = set(BASE_CONDITIONS)
PENDING_LABEL = "样本不足仅供参考"
PASS_LABEL = "样本数通过"


def add_forward_available_date(
    events: pd.DataFrame, dates: pd.DatetimeIndex, horizon: int = 20
) -> pd.DataFrame:
    """标出 fwd20 在哪个交易日收盘后才完整可知。

    run_profile.forward_returns 的 fwd20 对事件日 t 使用 t+2..t+21 的日收益，
    因而在事件索引 + (horizon + 1) 的收盘后才可用于下一次年度决策。
    """
    out = events.copy()
    event_i = dates.get_indexer(pd.to_datetime(out["trade_date"]))
    available_i = event_i + horizon + 1
    available = np.full(len(out), np.datetime64("NaT"), dtype="datetime64[ns]")
    valid = (event_i >= 0) & (available_i < len(dates)) & out["fwd20"].notna().to_numpy()
    available[valid] = dates.to_numpy()[available_i[valid]]
    out["fwd20可得日"] = pd.to_datetime(available)
    return out


def diagnostic_cells(events: pd.DataFrame) -> pd.DataFrame:
    """输出七席位 × 两时期 × 两位置层的预注册统计。"""
    valid = events.dropna(subset=["fwd20", "dist"]).copy()
    valid["时期"] = np.where(valid["trade_date"] < SPLIT_DATE, PERIOD_PRE, PERIOD_POST)
    valid["位置层"] = np.where(
        valid["dist"] < POSITION_LIMIT, POSITION_LOW, POSITION_AWAY
    )

    rows: list[dict[str, Any]] = []
    for member in CORE7:
        for period in (PERIOD_PRE, PERIOD_POST):
            for layer in (POSITION_LOW, POSITION_AWAY):
                sample = valid[
                    (valid["member"] == member)
                    & (valid["时期"] == period)
                    & (valid["位置层"] == layer)
                ]["fwd20"]
                n = len(sample)
                mean = sample.mean() if n else np.nan
                median = sample.median() if n else np.nan
                hit = (sample > 0).mean() if n else np.nan
                std = sample.std(ddof=1) if n >= 2 else np.nan
                t_value = mean / std * np.sqrt(n) if n >= 2 and std > 0 else np.nan
                rows.append(
                    {
                        "席位": member,
                        "时期": period,
                        "位置层": layer,
                        "N": n,
                        "均值%": mean * 100,
                        "中位%": median * 100,
                        "命中%": hit * 100,
                        "t": t_value,
                    }
                )
    return pd.DataFrame(rows)


def seat_evidence(cells: pd.DataFrame, member: str) -> dict[str, Any]:
    """按四格 N 门槛与两段贴低点减离低点均值差同号判定一个席位。"""
    seat = cells[cells["席位"] == member]

    def cell(period: str, layer: str) -> pd.Series:
        hit = seat[(seat["时期"] == period) & (seat["位置层"] == layer)]
        if len(hit) != 1:
            raise AssertionError(f"{member} {period} {layer} 诊断格数量异常：{len(hit)}")
        return hit.iloc[0]

    pre_low = cell(PERIOD_PRE, POSITION_LOW)
    pre_away = cell(PERIOD_PRE, POSITION_AWAY)
    post_low = cell(PERIOD_POST, POSITION_LOW)
    post_away = cell(PERIOD_POST, POSITION_AWAY)
    counts = [pre_low["N"], pre_away["N"], post_low["N"], post_away["N"]]
    enough = all(int(value) >= MIN_CELL_N for value in counts)
    diff_pre = pre_low["均值%"] - pre_away["均值%"]
    diff_post = post_low["均值%"] - post_away["均值%"]

    if not enough:
        evidence = "样本不足"
    elif diff_pre > 0 and diff_post > 0:
        evidence = "双段均正"
    elif diff_pre < 0 and diff_post < 0:
        evidence = "双段均负"
    else:
        evidence = "两段异号或含零"

    return {
        "席位": member,
        "原v4.2条件": "贴60日低点<5%" if member in ORIGINAL_CONDITIONAL else "无条件",
        "训练前段贴低点N": int(pre_low["N"]),
        "训练前段离低点N": int(pre_away["N"]),
        "训练后段贴低点N": int(post_low["N"]),
        "训练后段离低点N": int(post_away["N"]),
        "前段贴低点减离低点差%": diff_pre,
        "后段贴低点减离低点差%": diff_post,
        "四格N均>=20": enough,
        "一致证据": evidence,
    }


def decide_condition(evidence: Mapping[str, Any]) -> tuple[float | None, str]:
    """只允许预注册的新增/移除动作；其余情况回到 v4.2 原条件。"""
    member = str(evidence["席位"])
    state = evidence["一致证据"]
    if member in ORIGINAL_CONDITIONAL:
        if state == "双段均负":
            return None, "移除原条件"
        return POSITION_LIMIT, "保留原条件"
    if state == "双段均正":
        return POSITION_LIMIT, "新增位置条件"
    return None, "保持无条件"


def evidence_table(cells: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for member in CORE7:
        row = seat_evidence(cells, member)
        limit, action = decide_condition(row)
        row["判定动作"] = action
        row["判定后条件"] = "贴60日低点<5%" if limit is not None else "无条件"
        rows.append(row)
    return pd.DataFrame(rows)


def yearly_condition_mapping(
    events: pd.DataFrame, dates: pd.DatetimeIndex
) -> tuple[dict[int, dict[str, float]], pd.DataFrame]:
    """逐年构造只依赖年初前已实现 fwd20 的条件映射。"""
    mapping: dict[int, dict[str, float]] = {}
    audit_rows: list[dict[str, Any]] = []
    first_year, final_year = dates[0].year, dates[-1].year
    for year in range(first_year, final_year + 1):
        cutoff = pd.Timestamp(f"{year}-01-01")
        train = events[
            (events["trade_date"] < cutoff)
            & (events["fwd20可得日"] < cutoff)
        ].copy()
        cells = diagnostic_cells(train)
        year_map: dict[str, float] = {}
        for member in CORE7:
            evidence = seat_evidence(cells, member)
            limit, action = decide_condition(evidence)
            if limit is not None:
                year_map[member] = limit
            audit_rows.append(
                {
                    "年": year,
                    "年初截止": cutoff.date(),
                    **evidence,
                    "当年动作": action,
                    "当年条件": "贴60日低点<5%" if limit is not None else "无条件",
                }
            )
        mapping[year] = year_map
    return mapping, pd.DataFrame(audit_rows)


def static_conditions_from_evidence(evidence: pd.DataFrame) -> dict[str, float]:
    """全样本诊断对应的静态条件，仅供 in-sample 参考。"""
    result: dict[str, float] = {}
    for row in evidence.to_dict("records"):
        limit, _ = decide_condition(row)
        if limit is not None:
            result[str(row["席位"])] = limit
    return result


def strict_signals(model, start_year: int = WF_START) -> list[pd.Timestamp]:
    """按信号决策年截取 OOS；不按成交年偷换年度参数。"""
    return [pd.Timestamp(day) for day in model.signals if pd.Timestamp(day).year >= start_year]


def summarize_version(
    version: str,
    trades: pd.DataFrame,
    missed: int,
    sample_type: str,
) -> dict[str, Any]:
    row = {"版本": version, "口径": sample_type, **summarize(trades, missed)}
    if "非OOS" in sample_type:
        row["样本红线"] = "不适用（全样本参考）"
    else:
        row["样本红线"] = PASS_LABEL if len(completed(trades)) >= 20 else PENDING_LABEL
    return row


def yearly_trade_summary(trades: pd.DataFrame, version: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    work = trades.copy()
    work["信号年"] = pd.to_datetime(work["信号日"]).dt.year
    rows = []
    for year, group in work.groupby("信号年", sort=True):
        done = completed(group)
        rows.append(
            {
                "版本": version,
                "信号年": int(year),
                "笔数": len(group),
                "已完结": len(done),
                "胜率%": (done["收益%"] > 0).mean() * 100 if len(done) else np.nan,
                "均收益%": done["收益%"].mean() if len(done) else np.nan,
                "止损笔数": int((done["结果"] == "止损").sum()) if len(done) else 0,
                "总收益%": done["收益%"].sum() if len(done) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def conditions_text(mapping: Mapping[str, float]) -> str:
    ordered = [member for member in CORE7 if member in mapping]
    return "、".join(ordered) if ordered else "无"


def main() -> int:
    ctx = load_context()
    # P4 的事件诊断必须从未经过位置过滤的原始增多事件开始。
    baseline_model = build_event_model(ctx, conditions=BASE_CONDITIONS)
    raw_events = add_forward_available_date(baseline_model.events, ctx.dates)

    full_cells = diagnostic_cells(raw_events)
    full_evidence = evidence_table(full_cells)
    full_conditions = static_conditions_from_evidence(full_evidence)

    annual_mapping, mapping_audit = yearly_condition_mapping(raw_events, ctx.dates)
    causal_model = build_event_model(
        ctx,
        conditions=None,
        conditions_by_year=annual_mapping,
    )
    full_sample_model = build_event_model(ctx, conditions=full_conditions)

    # v4.3 严格口径统一使用最近 5 个交易日识别成本触发席位。
    config = TradeConfig(
        entry_kind="fixed",
        zone_value=5.0,
        validity=10,
        miss_policy="abandon",
        stop=0.04,
        decay_days=10,
        exact_trigger_window=True,
    )
    baseline_trades, baseline_missed = run_trades_stateful(
        ctx, baseline_model, config, pending_policy="abandon",
        signals=strict_signals(baseline_model)
    )
    causal_trades, causal_missed = run_trades_stateful(
        ctx, causal_model, config, pending_policy="abandon",
        signals=strict_signals(causal_model)
    )
    full_sample_trades, full_sample_missed = run_trades_stateful(
        ctx, full_sample_model, config, pending_policy="abandon",
        signals=strict_signals(full_sample_model)
    )

    # P4 的同期基准由同一严格未知/成本口径直接生成，避免依赖旧缓存硬编码数字。
    baseline_summary = summarize(baseline_trades, baseline_missed)
    if baseline_summary["笔数"] == 0:
        raise AssertionError("P4 严格2015起同期基准没有交易，无法比较")

    compare = pd.DataFrame(
        [
            summarize_version(
                "v4.3严格基准（原条件）",
                baseline_trades,
                baseline_missed,
                "OOS/WF：严格2015起，按信号年",
            ),
            summarize_version(
                "P4逐年因果条件映射",
                causal_trades,
                causal_missed,
                "OOS/WF：严格2015起，按信号年",
            ),
            summarize_version(
                "P4终局条件静态回放",
                full_sample_trades,
                full_sample_missed,
                "全样本优化参考（非OOS）",
            ),
        ]
    )

    causal_trades = causal_trades.copy()
    causal_trades["信号年"] = pd.to_datetime(causal_trades["信号日"]).dt.year
    baseline_trades = baseline_trades.copy()
    baseline_trades["信号年"] = pd.to_datetime(baseline_trades["信号日"]).dt.year
    full_sample_trades = full_sample_trades.copy()
    full_sample_trades["信号年"] = pd.to_datetime(full_sample_trades["信号日"]).dt.year
    yearly = pd.concat(
        [
            yearly_trade_summary(baseline_trades, "v4.3严格基准（原条件）"),
            yearly_trade_summary(causal_trades, "P4逐年因果条件映射"),
        ],
        ignore_index=True,
    )

    # 年度映射压成一行，便于直接检查每个信号年用了哪些条件。
    map_rows = []
    for year in range(WF_START, ctx.dates[-1].year + 1):
        audit = mapping_audit[mapping_audit["年"] == year]
        changed = audit[audit["当年动作"].isin(["新增位置条件", "移除原条件"])]
        map_rows.append(
            {
                "年": year,
                "有位置条件席位": conditions_text(annual_mapping[year]),
                "相对v4.2发生变更": "；".join(
                    f"{row['席位']}:{row['当年动作']}" for row in changed.to_dict("records")
                )
                or "无",
            }
        )
    map_summary = pd.DataFrame(map_rows)

    report = io.StringIO()
    report.write("# v4.3 P4 条件计分推广独立报告\n\n")
    report.write("## 预注册判据\n\n")
    report.write(
        "每席位四格（<2020/≥2020 × dist60<5%/≥5%）均须 N≥20；"
        "两个时期的“贴低点减离低点”前向20日均值差严格同号才算一致。原无条件席位仅双段均正"
        "时新增条件；国泰君安/东证期货仅双段均负时移除，否则保持 v4.2 原条件。\n\n"
    )
    report.write("## 七席位分层诊断（全样本，仅作诊断参考）\n\n")
    report.write("```text\n")
    report.write(full_cells.round(3).to_string(index=False))
    report.write("\n```")
    report.write("\n\n## 全样本一致性判定（不用于 OOS 年度决策）\n\n")
    report.write("```text\n")
    report.write(full_evidence.round(3).to_string(index=False))
    report.write("\n```")
    report.write(
        f"\n\n终局静态条件席位（全样本优化参考，非OOS）：{conditions_text(full_conditions)}。\n"
    )
    report.write("\n## 逐年因果条件映射\n\n")
    report.write(
        "每年只使用年初前且 fwd20 已走完的事件；第二段 [2020, year) 任一位置层 N<20，"
        "或其余任一格 N<20，均保持原条件。\n\n"
    )
    report.write("```text\n")
    report.write(map_summary.to_string(index=False))
    report.write("\n```")
    report.write("\n\n## P4 OOS/WF 交易结果（v4.3 严格引擎）\n\n")
    report.write("```text\n")
    report.write(compare.round(3).to_string(index=False))
    report.write("\n```")
    report.write(
        "\n\nOOS 按信号年归属；消退在确认日收盘后才成立，次日开盘卖出。"
        "“全样本优化参考（非OOS）”不得作为推荐依据。\n"
    )
    report.write("\n## 分信号年结果\n\n")
    report.write("```text\n")
    report.write(yearly.round(3).to_string(index=False))
    report.write("\n```")
    report.write("\n\n## 无未来函数自查\n\n")
    report.write(
        "| 检查项 | P4 实现 |\n| --- | --- |\n"
        "| 事件阈值 | `rolling(window).quantile(q).shift(1)`，事件日不进入自身阈值 |\n"
        "| 席位权重 | `expanding_weights` 每年仅用上年 12-01 前事件的已实现 fwd20 |\n"
        "| 年度条件映射 | 年初前事件且 `fwd20可得日 < 年初`；不使用当年结果 |\n"
        "| 买入 | 信号日盘后确认，最早 T+1 执行；区间订单 10 个交易日 |\n"
        "| 卖出 | 盘中固定 -4% 止损；消退确认后 T+1 开盘 |\n"
        "| 年度归属 | 按信号年，不按可能跨年的进场年 |\n"
    )

    report_text = report.getvalue()
    print(report_text)

    out = aulib.OUT
    full_cells.to_csv(out / "v43_p4_diagnostic_cells.csv", index=False, encoding="utf-8-sig")
    full_evidence.to_csv(out / "v43_p4_evidence.csv", index=False, encoding="utf-8-sig")
    mapping_audit.to_csv(out / "v43_p4_year_mapping_audit.csv", index=False, encoding="utf-8-sig")
    map_summary.to_csv(out / "v43_p4_year_mapping.csv", index=False, encoding="utf-8-sig")
    compare.to_csv(out / "v43_p4_compare.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(out / "v43_p4_yearly.csv", index=False, encoding="utf-8-sig")
    baseline_trades.to_pickle(out / "v43_p4_baseline_trades.pkl")
    causal_trades.to_pickle(out / "v43_p4_oos_trades.pkl")
    full_sample_trades.to_pickle(out / "v43_p4_fullsample_trades.pkl")
    (out / "v43_p4_report.md").write_text(report_text, encoding="utf-8")
    print("已写出 out/v43_p4_report.md、诊断/年度映射/比较 CSV 与逐笔 pickle。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
