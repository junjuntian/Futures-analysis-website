# -*- coding: utf-8 -*-
"""黄金席位系统 v4.3 研究公共内核（仅供 research/v43_*.py 使用）。

目标是让 P1-P6 共用同一套因果时序：滚动阈值 shift(1)、逐年扩窗权重、
信号日盘后确认、区间订单最早 T+1、生效消退最早 T+1 开盘。旧脚本保持只读。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

import aulib
from run_optimize import CORE7, build_events, expanding_weights


COST = 0.001
STOP = 0.04
WF_START = 2015
BASE_CONDITIONS = {"国泰君安": 0.05, "东证期货": 0.05}
PENDING_RESULTS = {"持有中", "待消退执行", "待到期追价"}


@dataclass
class Context:
    cont: pd.DataFrame
    md: pd.DataFrame
    dates: pd.DatetimeIndex
    pos: dict[pd.Timestamp, int]
    dist60: pd.Series
    netsum: pd.Series
    netq: pd.Series
    main_settle: pd.Series
    costs: dict[str, pd.Series]


@dataclass
class EventModel:
    q: float
    threshold_window: int
    event_window: int
    events: pd.DataFrame
    effective_events: pd.DataFrame
    weights: dict[int, dict[str, float]]
    weight_matrix: pd.DataFrame
    strong: pd.DataFrame
    active_day: pd.Series
    active_recent: pd.DataFrame
    score: pd.Series
    nseat: pd.Series
    pers10: pd.Series
    signals: pd.DatetimeIndex


@dataclass(frozen=True)
class TradeConfig:
    entry_kind: str = "fixed"  # fixed / percent / market
    zone_value: float = 5.0
    validity: int = 10
    miss_policy: str = "abandon"  # abandon / expiry_market
    stop: float = STOP
    decay_days: int = 10
    breakeven_trigger: float | None = None
    exact_trigger_window: bool = True


TradeSpec = tuple[EventModel, TradeConfig, str]


def ensure_research_inputs() -> None:
    """若既有研究缓存不在，直接从只读原始数据重建 v4.3 所需三项缓存。"""
    aulib.OUT.mkdir(parents=True, exist_ok=True)
    main_path = aulib.OUT / "au_main.pkl"
    cont_path = aulib.OUT / "au_continuous.pkl"
    member_path = aulib.OUT / "member_day.pkl"
    price = None
    if not main_path.exists():
        price = aulib.load_price()
        aulib.main_contract(price).to_pickle(main_path)
    if not cont_path.exists():
        if price is None:
            price = aulib.load_price()
        main_contract = pd.read_pickle(main_path)
        aulib.continuous_series(price, main_contract).to_pickle(cont_path)
    if not member_path.exists():
        from run_profile import member_day_table

        member_day_table(aulib.load_seat()).to_pickle(member_path)


def strict_member_day_table(seat: pd.DataFrame) -> pd.DataFrame:
    """v4.3 严格席位日表：任一持仓榜腿缺失时 Net/ΔNet 均保持不可知。

    只聚合交易所 ``quantity/change``，绝不以相邻日 Net 差分，也不把未上榜腿补 0。
    """
    sub = seat[(~seat["is_variety_total"]) & seat["rank_type"].isin(["long", "short"])]
    required_values = ["quantity", "change"]
    if sub[required_values].isna().any().any():
        missing = sub[required_values].isna().sum().to_dict()
        raise ValueError(f"席位原始 quantity/change 存在缺失，不能静默聚合：{missing}")
    pivot = sub.pivot_table(
        index=["member", "trade_date"],
        columns="rank_type",
        values=["quantity", "change"],
        aggfunc="sum",
    )
    result = pd.DataFrame(index=pivot.index)
    for source, side, target in [
        ("quantity", "long", "long_q"),
        ("quantity", "short", "short_q"),
        ("change", "long", "dlong"),
        ("change", "short", "dshort"),
    ]:
        if source in pivot.columns.get_level_values(0) and side in pivot[source].columns:
            result[target] = pivot[source][side]
        else:
            result[target] = np.nan
    both_quantity = result["long_q"].notna() & result["short_q"].notna()
    both_change = result["dlong"].notna() & result["dshort"].notna()
    result["net"] = (result["long_q"] - result["short_q"]).where(both_quantity)
    result["dnet"] = (result["dlong"] - result["dshort"]).where(both_change)
    return result.reset_index()


def strict_seat_cost_series(
    md: pd.DataFrame,
    member: str,
    settle: pd.Series,
    volume: pd.Series,
    dates: pd.DatetimeIndex,
) -> pd.Series:
    """严格成本：不可见/零成交即失效，重新净多前不跨缺口猜成本。"""
    visible = md[md["member"] == member].set_index("trade_date").sort_index()
    cost = np.nan
    previous_net = np.nan
    values: dict[pd.Timestamp, float] = {}
    for day in dates:
        if day not in visible.index:
            cost = np.nan
            previous_net = np.nan
            values[day] = np.nan
            continue
        row = visible.loc[day]
        net, dnet = row["net"], row["dnet"]
        price = settle.get(day, np.nan)
        traded = volume.get(day, np.nan)
        if not np.isfinite(net) or not np.isfinite(dnet) or not np.isfinite(price) \
                or not np.isfinite(traded) or traded <= 0:
            cost = np.nan
            previous_net = np.nan
            values[day] = np.nan
            continue
        implied_previous_net = net - dnet
        if net <= 0:
            cost = np.nan
        elif np.isfinite(previous_net):
            # 连续可见日必须能由交易所 change 还原上一日净仓；否则本轮成本失效。
            if not np.isclose(implied_previous_net, previous_net, rtol=0.0, atol=1e-9):
                cost = np.nan
            elif previous_net <= 0:
                cost = float(price)
            elif dnet > 0 and np.isfinite(cost):
                cost = (cost * previous_net + price * dnet) / net
        elif implied_previous_net <= 0:
            # 即使上一日掉榜，交易所 change 仍可证明当日从净空/零翻为净多。
            cost = float(price)
        # 首个可见/重现日若 implied_previous_net>0，存量多头成本未知，保持 NaN。
        values[day] = cost
        previous_net = net
    return pd.Series(values, dtype=float)


def load_context() -> Context:
    """读取既有只读中间件并补齐主力真实/复权 OHLC。"""
    ensure_research_inputs()
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl").copy()
    strict_member_path = aulib.OUT / "v43_member_day_strict.pkl"
    if not strict_member_path.exists():
        strict_member_day_table(aulib.load_seat()).to_pickle(strict_member_path)
    md = pd.read_pickle(strict_member_path)
    price = aulib.load_price()
    mc = pd.read_pickle(aulib.OUT / "au_main.pkl")
    px = price.set_index(["contract", "trade_date"]).sort_index()

    def main_values(column: str):
        wide = px[column].unstack(0)
        return [
            wide.at[d, m] if m in wide.columns else np.nan
            for d, m in zip(mc["trade_date"], mc["main"])
        ]

    cont["open"] = main_values("open_price")
    cont["settle"] = main_values("settlement_price")
    cont["volume"] = main_values("volume")
    end_factor = cont["factor"].iloc[-1]
    cont["adj_open"] = cont["open"] / cont["factor"] * end_factor

    dates = cont.index
    core = md[md["member"].isin(CORE7)]
    # 榜上可见净仓只在当日聚合；整日无有效值保持 NaN，绝不把旧值前填。
    netsum = core.groupby("trade_date")["net"].sum(min_count=1).reindex(dates)
    dist60 = cont["adj_close"] / cont["adj_close"].rolling(60).min() - 1
    netq = netsum.rolling(250, min_periods=120).rank(pct=True)
    main_settle = pd.Series(cont["settle"].to_numpy(), index=dates)
    main_volume = pd.Series(cont["volume"].to_numpy(), index=dates)
    costs = {
        member: strict_seat_cost_series(md, member, main_settle, main_volume, dates)
        for member in CORE7
    }
    return Context(
        cont=cont,
        md=md,
        dates=dates,
        pos={d: i for i, d in enumerate(dates)},
        dist60=dist60,
        netsum=netsum,
        netq=netq,
        main_settle=main_settle,
        costs=costs,
    )


def _condition_limit(
    member: str,
    year: int,
    fixed: Mapping[str, float] | None,
    by_year: Mapping[int, Mapping[str, float]] | None,
) -> float | None:
    if by_year is not None:
        return by_year.get(year, {}).get(member)
    return None if fixed is None else fixed.get(member)


def build_event_model(
    ctx: Context,
    q: float = 0.80,
    threshold_window: int = 250,
    event_window: int = 5,
    conditions: Mapping[str, float] | None = BASE_CONDITIONS,
    conditions_by_year: Mapping[int, Mapping[str, float]] | None = None,
    signal_score: float = 6.0,
    dist_limit: float = 0.12,
    netq_limit: float = 0.60,
) -> EventModel:
    """参数化构建事件、逐年权重、条件计分和 v4.2 固定买点。"""
    events = build_events(
        ctx.md,
        ctx.cont,
        CORE7,
        q=q,
        window=threshold_window,
        min_hist=120,
    ).copy()
    years = range(ctx.dates[0].year, ctx.dates[-1].year + 1)
    weights = expanding_weights(events, years)
    events["dist"] = ctx.dist60.reindex(events["trade_date"]).to_numpy()
    keep = []
    for row in events.itertuples(index=False):
        limit = _condition_limit(
            row.member,
            pd.Timestamp(row.trade_date).year,
            conditions,
            conditions_by_year,
        )
        keep.append(limit is None or row.dist < limit)
    effective = events[np.asarray(keep, dtype=bool)].copy()

    strong = effective.pivot_table(
        index="trade_date", columns="member", values="strength", aggfunc="max"
    )
    strong = strong.reindex(ctx.dates).reindex(columns=CORE7)
    wmat = pd.DataFrame(
        {
            member: [weights[d.year].get(member, 0.0) for d in ctx.dates]
            for member in CORE7
        },
        index=ctx.dates,
    )
    weighted = strong.fillna(0) * wmat
    score = weighted.rolling(event_window, min_periods=1).max().sum(axis=1)
    active = strong.notna() & (wmat > 0)
    active_recent = active.rolling(event_window, min_periods=1).max().astype(bool)
    nseat = active_recent.sum(axis=1)
    active_day = active.any(axis=1)
    pers10 = active_day.rolling(10, min_periods=1).sum()
    signal_mask = (
        (score >= signal_score)
        & (ctx.dist60 < dist_limit)
        & (ctx.netq < netq_limit)
    )
    signals = ctx.dates[signal_mask.fillna(False)]
    return EventModel(
        q=q,
        threshold_window=threshold_window,
        event_window=event_window,
        events=events,
        effective_events=effective,
        weights=weights,
        weight_matrix=wmat,
        strong=strong,
        active_day=active_day,
        active_recent=active_recent,
        score=score,
        nseat=nseat,
        pers10=pers10,
        signals=signals,
    )


def weighted_cost_at(
    ctx: Context,
    model: EventModel,
    signal_day: pd.Timestamp,
    exact_trigger_window: bool = True,
) -> tuple[float, list[str]]:
    """信号日触发席位的当日权重加权成本（真实价）。"""
    if exact_trigger_window:
        members = [
            member
            for member in CORE7
            if bool(model.active_recent.at[signal_day, member])
        ]
    else:
        # P0/旧 v4.2 对拍专用：原脚本用近 8 个自然日近似 5 个交易日。
        recent = model.effective_events[
            (model.effective_events["trade_date"] > signal_day - pd.Timedelta(days=8))
            & (model.effective_events["trade_date"] <= signal_day)
        ]
        members = list(recent["member"].unique())

    numerator = denominator = 0.0
    used = []
    for member in members:
        weight = model.weights[signal_day.year].get(member, 0.0)
        cost = ctx.costs[member].get(signal_day, np.nan)
        if weight > 0 and np.isfinite(cost):
            numerator += weight * cost
            denominator += weight
            used.append(member)
    return (numerator / denominator if denominator else np.nan), used


def _require_open(open_prices: np.ndarray, i: int, dates: pd.DatetimeIndex, action: str) -> float:
    value = open_prices[i]
    if not np.isfinite(value):
        raise ValueError(f"{action}要求开盘价，但 {dates[i].date()} 主力复权开盘价缺失")
    return float(value)


def run_trades(
    ctx: Context,
    model: EventModel,
    config: TradeConfig = TradeConfig(),
    signals: Iterable[pd.Timestamp] | None = None,
) -> tuple[pd.DataFrame, int]:
    """按 v4.2 固定规则重放，消退严格为确认后 T+1 开盘。"""
    dates = ctx.dates
    cont = ctx.cont
    low_real = cont["low"].to_numpy()
    open_real = cont["open"].to_numpy()
    low_adj = cont["adj_low"].to_numpy()
    high_adj = cont["adj_high"].to_numpy()
    close_adj = cont["adj_close"].to_numpy()
    open_adj = cont["adj_open"].to_numpy()
    factors = (cont["factor"].iloc[-1] / cont["factor"]).to_numpy()
    decay_count = (
        model.active_day.rolling(config.decay_days, min_periods=1).sum().to_numpy()
    )

    trades = []
    busy_until = -1
    missed = 0
    signal_days = model.signals if signals is None else list(signals)
    for signal_day0 in signal_days:
        signal_day = pd.Timestamp(signal_day0)
        signal_i = ctx.pos.get(signal_day)
        if signal_i is None or signal_i + 1 >= len(dates) or signal_i < busy_until:
            continue

        weighted_cost, trigger_members = weighted_cost_at(
            ctx,
            model,
            signal_day,
            exact_trigger_window=config.exact_trigger_window,
        )
        zone_low = zone_high = np.nan
        entry_i = None
        entry_real = np.nan

        if config.entry_kind == "market" or not np.isfinite(weighted_cost):
            entry_i = signal_i + 1
            entry_adj = _require_open(open_adj, entry_i, dates, "T+1 市价进场")
            if np.isfinite(open_real[entry_i]):
                entry_real = float(open_real[entry_i])
        else:
            if config.entry_kind == "fixed":
                half_width = config.zone_value
            elif config.entry_kind == "percent":
                half_width = weighted_cost * config.zone_value
            else:
                raise ValueError(f"未知 entry_kind: {config.entry_kind}")
            zone_low = weighted_cost - half_width
            zone_high = weighted_cost + half_width
            last_order_i = min(signal_i + config.validity, len(dates) - 1)
            for i in range(signal_i + 1, last_order_i + 1):
                if i <= busy_until:
                    break
                if np.isfinite(low_real[i]) and low_real[i] <= zone_high:
                    open_value = _require_open(open_real, i, dates, "区间进场")
                    entry_real = min(open_value, zone_high)
                    entry_adj = entry_real * factors[i]
                    entry_i = i
                    break
            if entry_i is None:
                missed += 1
                if config.miss_policy == "abandon":
                    continue
                if config.miss_policy != "expiry_market":
                    raise ValueError(f"未知 miss_policy: {config.miss_policy}")
                chase_i = last_order_i + 1
                if chase_i >= len(dates):
                    continue
                entry_i = chase_i
                entry_adj = _require_open(open_adj, entry_i, dates, "区间到期 T+1 追价")
                if np.isfinite(open_real[entry_i]):
                    entry_real = float(open_real[entry_i])

        if entry_i is None or not np.isfinite(entry_adj):
            continue

        base_stop = entry_adj * (1 - config.stop)
        current_stop = base_stop
        breakeven_from = None
        exit_i = None
        exit_price = None
        reason = None
        decay_confirm = pd.NaT
        for i in range(entry_i, len(dates)):
            if not np.isfinite(low_adj[i]) or not np.isfinite(high_adj[i]):
                continue

            if breakeven_from is not None and i >= breakeven_from:
                current_stop = max(current_stop, entry_adj)
            if low_adj[i] <= current_stop:
                exit_i, exit_price = i, current_stop
                reason = "保本" if current_stop >= entry_adj else "止损"
                break

            # 日线无法识别同日 high/low 顺序，保本线从下一交易日才生效。
            if (
                config.breakeven_trigger is not None
                and breakeven_from is None
                and high_adj[i] >= entry_adj * (1 + config.breakeven_trigger)
            ):
                breakeven_from = i + 1

            if i > entry_i + 2 and decay_count[i] == 0:
                decay_confirm = dates[i]
                if i + 1 < len(dates):
                    exit_i = i + 1
                    exit_price = _require_open(open_adj, exit_i, dates, "消退 T+1 卖出")
                    reason = "消退T+1"
                else:
                    exit_i, exit_price, reason = i, close_adj[i], "待消退执行"
                break

        if exit_price is None:
            exit_i = len(dates) - 1
            exit_price, reason = close_adj[exit_i], "持有中"

        trades.append(
            {
                "信号日": dates[signal_i].date(),
                "进场日": dates[entry_i].date(),
                "区间下沿": round(zone_low, 3) if np.isfinite(zone_low) else None,
                "区间上沿": round(zone_high, 3) if np.isfinite(zone_high) else None,
                "机构加权成本": round(weighted_cost, 3) if np.isfinite(weighted_cost) else None,
                "进场价(真实)": round(entry_real, 3) if np.isfinite(entry_real) else None,
                "触发席位": "、".join(trigger_members),
                "信号score": float(model.score.iloc[signal_i]),
                "信号共振家数": int(model.nseat.iloc[signal_i]),
                "消退确认日": decay_confirm.date() if pd.notna(decay_confirm) else None,
                "出场日": dates[exit_i].date(),
                "结果": reason,
                "收益%": (exit_price / entry_adj - 1 - COST) * 100,
                "持有日": exit_i - entry_i + 1,
            }
        )
        busy_until = exit_i
    return pd.DataFrame(trades), missed


def _validate_pending_policy(pending_policy: str) -> None:
    if pending_policy not in {"abandon", "refresh", "expiry_market"}:
        raise ValueError(f"未知 pending_policy: {pending_policy}")


def _run_trades_stateful_specs(
    ctx: Context,
    signal_specs: Mapping[pd.Timestamp, TradeSpec],
) -> tuple[pd.DataFrame, int]:
    """共享的逐日状态机；``signal_specs`` 只决定新信号采用哪套冻结规则。"""
    dates = ctx.dates
    cont = ctx.cont
    low_real = cont["low"].to_numpy()
    open_real = cont["open"].to_numpy()
    low_adj = cont["adj_low"].to_numpy()
    high_adj = cont["adj_high"].to_numpy()
    close_adj = cont["adj_close"].to_numpy()
    open_adj = cont["adj_open"].to_numpy()
    factors = (cont["factor"].iloc[-1] / cont["factor"]).to_numpy()
    decay_cache: dict[tuple[int, int], np.ndarray] = {}

    def decay_count_for(model: EventModel, config: TradeConfig) -> np.ndarray:
        key = (id(model), config.decay_days)
        if key not in decay_cache:
            decay_cache[key] = model.active_day.rolling(
                config.decay_days, min_periods=1
            ).sum().to_numpy()
        return decay_cache[key]

    state = "flat"
    pending: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    missed = 0

    def make_order(signal_i: int, spec: TradeSpec) -> dict[str, Any] | None:
        if signal_i + 1 >= len(dates):
            return None
        model, config, pending_policy = spec
        _validate_pending_policy(pending_policy)
        signal_day = dates[signal_i]
        weighted_cost, trigger_members = weighted_cost_at(
            ctx,
            model,
            signal_day,
            exact_trigger_window=config.exact_trigger_window,
        )
        base = {
            "signal_i": signal_i,
            "signal_day": signal_day,
            "weighted_cost": weighted_cost,
            "trigger_members": trigger_members,
            "score": float(model.score.iloc[signal_i]),
            "nseat": int(model.nseat.iloc[signal_i]),
            "zone_low": np.nan,
            "zone_high": np.nan,
            "start_i": signal_i + 1,
            # 订单、成交后的持仓都冻结信号日所选规则，不能被跨年参数接管。
            "model": model,
            "config": config,
            "pending_policy": pending_policy,
            "decay_count": decay_count_for(model, config),
        }
        if config.entry_kind == "market" or not np.isfinite(weighted_cost):
            return {**base, "kind": "market", "end_i": signal_i + 1}
        if config.entry_kind == "fixed":
            half_width = config.zone_value
        elif config.entry_kind == "percent":
            half_width = weighted_cost * config.zone_value
        else:
            raise ValueError(f"未知 entry_kind: {config.entry_kind}")
        return {
            **base,
            "kind": "zone",
            "zone_low": weighted_cost - half_width,
            "zone_high": weighted_cost + half_width,
            "end_i": min(signal_i + config.validity, len(dates) - 1),
        }

    def enter(order: dict[str, Any], entry_i: int, entry_real: float | None):
        nonlocal state, pending, current
        config: TradeConfig = order["config"]
        if entry_real is None:
            entry_adj = _require_open(open_adj, entry_i, dates, "T+1 市价进场")
            entry_real_value = float(open_real[entry_i]) if np.isfinite(open_real[entry_i]) else np.nan
        else:
            entry_real_value = float(entry_real)
            entry_adj = entry_real_value * factors[entry_i]
        current = {
            **order,
            "entry_i": entry_i,
            "entry_adj": entry_adj,
            "entry_real": entry_real_value,
            "base_stop": entry_adj * (1 - config.stop),
            "current_stop": entry_adj * (1 - config.stop),
            "breakeven_from": None,
            "decay_confirm": pd.NaT,
            "decay_exit_i": None,
        }
        pending = None
        state = "long"

    def close_trade(exit_i: int, exit_price: float, reason: str):
        nonlocal state, current
        assert current is not None
        trades.append(
            {
                "信号日": dates[current["signal_i"]].date(),
                "进场日": dates[current["entry_i"]].date(),
                "区间下沿": round(current["zone_low"], 3)
                if np.isfinite(current["zone_low"])
                else None,
                "区间上沿": round(current["zone_high"], 3)
                if np.isfinite(current["zone_high"])
                else None,
                "机构加权成本": round(current["weighted_cost"], 3)
                if np.isfinite(current["weighted_cost"])
                else None,
                "进场价(真实)": round(current["entry_real"], 3)
                if np.isfinite(current["entry_real"])
                else None,
                "触发席位": "、".join(current["trigger_members"]),
                "信号score": current["score"],
                "信号共振家数": current["nseat"],
                "消退确认日": current["decay_confirm"].date()
                if pd.notna(current["decay_confirm"])
                else None,
                "出场日": dates[exit_i].date(),
                "结果": reason,
                "收益%": (exit_price / current["entry_adj"] - 1 - COST) * 100,
                "持有日": exit_i - current["entry_i"] + 1,
            }
        )
        current = None
        state = "flat"

    for i, day in enumerate(dates):
        # 1) 开盘时点：消退卖单优先于当日盘中止损；市价进场也在开盘执行。
        if state == "long" and current is not None and current["decay_exit_i"] == i:
            close_trade(i, _require_open(open_adj, i, dates, "消退 T+1 卖出"), "消退T+1")
        if state == "pending" and pending is not None and pending["kind"] == "market" \
                and pending["start_i"] == i:
            enter(pending, i, None)

        # 2) 盘中：区间成交后同一根 K 线即受硬止损约束（保守顺序）。
        if state == "pending" and pending is not None and pending["kind"] == "zone" \
                and pending["start_i"] <= i <= pending["end_i"]:
            if np.isfinite(low_real[i]) and low_real[i] <= pending["zone_high"]:
                open_value = _require_open(open_real, i, dates, "区间进场")
                enter(pending, i, min(open_value, pending["zone_high"]))

        if state == "long" and current is not None and np.isfinite(low_adj[i]) \
                and np.isfinite(high_adj[i]):
            config: TradeConfig = current["config"]
            decay_count: np.ndarray = current["decay_count"]
            if current["breakeven_from"] is not None and i >= current["breakeven_from"]:
                current["current_stop"] = max(current["current_stop"], current["entry_adj"])
            if low_adj[i] <= current["current_stop"]:
                reason = "保本" if current["current_stop"] >= current["entry_adj"] else "止损"
                close_trade(i, current["current_stop"], reason)
            elif current is not None:
                if config.breakeven_trigger is not None and current["breakeven_from"] is None \
                        and high_adj[i] >= current["entry_adj"] * (1 + config.breakeven_trigger):
                    current["breakeven_from"] = i + 1
                if i > current["entry_i"] + 2 and decay_count[i] == 0:
                    current["decay_confirm"] = day
                    if i + 1 < len(dates):
                        current["decay_exit_i"] = i + 1
                    else:
                        close_trade(i, close_adj[i], "待消退执行")

        # 3) 收盘时点：处理新信号与挂单到期。持仓中信号一律忽略。
        signal_spec = signal_specs.get(day)
        has_signal = signal_spec is not None
        if state == "pending" and pending is not None and has_signal \
                and pending["pending_policy"] == "refresh":
            assert signal_spec is not None
            replacement = make_order(i, signal_spec)
            if replacement is not None:
                pending = replacement

        if state == "pending" and pending is not None and pending["kind"] == "zone" \
                and pending["end_i"] == i:
            missed += 1
            if pending["pending_policy"] == "expiry_market" and i + 1 < len(dates):
                pending = {**pending, "kind": "market", "start_i": i + 1, "end_i": i + 1}
            else:
                pending = None
                state = "flat"

        if state == "flat" and has_signal:
            assert signal_spec is not None
            order = make_order(i, signal_spec)
            if order is not None:
                pending = order
                state = "pending"

    if state == "long" and current is not None:
        # 期末仅盯市，不能伪造成交；结果被 completed() 排除。
        reason = "待消退执行" if pd.notna(current["decay_confirm"]) else "持有中"
        close_trade(len(dates) - 1, close_adj[-1], reason)
    return pd.DataFrame(trades), missed


def run_trades_stateful(
    ctx: Context,
    model: EventModel,
    config: TradeConfig = TradeConfig(),
    pending_policy: str = "abandon",
    signals: Iterable[pd.Timestamp] | None = None,
) -> tuple[pd.DataFrame, int]:
    """逐交易日单挂单/单仓位状态机，供 P3 及最终组合使用。

    ``pending_policy``：
    - ``abandon``：挂单有效期内忽略新信号，到期放弃；
    - ``refresh``：新信号收盘后按新成本重置挂单与有效期；
    - ``expiry_market``：到期确认后下一交易日开盘追价。
    """
    _validate_pending_policy(pending_policy)
    signal_days = model.signals if signals is None else signals
    spec = (model, config, pending_policy)
    signal_specs = {pd.Timestamp(day): spec for day in signal_days}
    return _run_trades_stateful_specs(ctx, signal_specs)


def run_trades_yearly(
    ctx: Context,
    specs_by_year: Mapping[int, TradeSpec],
    start_year: int = WF_START,
) -> tuple[pd.DataFrame, int]:
    """把逐年选出的规则做成一条跨年连续 OOS 回放。

    ``specs_by_year`` 的值为 ``(EventModel, TradeConfig, pending_policy)``。仅在
    新信号的收盘时点按信号年取规格；既有挂单（包括到期追价）和持仓继续使用
    创建它们时冻结的模型、交易参数及挂单策略，因此 12 月 31 日不会强平、撤单
    或被下一年参数接管。缺少规格的年份不接受新信号，但此前状态仍继续运行。
    """
    normalised: dict[int, TradeSpec] = {}
    for year, raw_spec in specs_by_year.items():
        try:
            model, config, pending_policy = raw_spec
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{year} 年规格必须是 (EventModel, TradeConfig, pending_policy)"
            ) from exc
        _validate_pending_policy(pending_policy)
        normalised[int(year)] = (model, config, pending_policy)

    signal_specs: dict[pd.Timestamp, TradeSpec] = {}
    for year, spec in normalised.items():
        if year < start_year:
            continue
        model = spec[0]
        for signal_day0 in model.signals:
            signal_day = pd.Timestamp(signal_day0)
            if signal_day.year == year:
                signal_specs[signal_day] = spec
    return _run_trades_stateful_specs(ctx, signal_specs)


def completed(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    return trades[~trades["结果"].isin(PENDING_RESULTS)]


def summarize(trades: pd.DataFrame, missed: int = 0) -> dict[str, Any]:
    done = completed(trades)
    if trades.empty or done.empty:
        return {
            "笔数": len(trades),
            "已完结": len(done),
            "胜率%": np.nan,
            "均收益%": np.nan,
            "止损笔数": 0,
            "总收益%": 0.0,
            "错过信号": missed,
        }
    return {
        "笔数": len(trades),
        "已完结": len(done),
        "胜率%": round((done["收益%"] > 0).mean() * 100, 1),
        "均收益%": round(done["收益%"].mean(), 2),
        "止损笔数": int((done["结果"] == "止损").sum()),
        "总收益%": round(done["收益%"].sum(), 1),
        "错过信号": missed,
    }


def conservative_score(trades: pd.DataFrame, cutoff: pd.Timestamp, min_n: int = 12) -> float:
    """照抄 run_rulesearch.py：完结日早于 cutoff，均值减一个标准误。"""
    if trades.empty:
        return -np.inf
    done = completed(trades)
    train = done[pd.to_datetime(done["出场日"]) < cutoff]
    if len(train) < min_n:
        return -np.inf
    std = train["收益%"].std(ddof=1)
    if not np.isfinite(std):
        return -np.inf
    return float(train["收益%"].mean() - std / np.sqrt(len(train)))


def walk_forward(
    cache: list[tuple[Mapping[str, Any], pd.DataFrame, int]],
    final_year: int,
    start_year: int = WF_START,
    min_train_trades: int = 12,
    decision_date_col: str = "信号日",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """逐年只用年初前已完结交易选规则，再拼当年决策产生的交易。

    旧 ``run_rulesearch.py`` 按进场年切块，会让上一年信号的未成交订单被下一年
    才选出的参数接管。v4.3 默认按信号年（真正的参数决策时点）归属；训练评分、
    最少样本与平局行为仍逐字照旧。
    """
    oos = []
    picks = []
    for year in range(start_year, final_year + 1):
        cutoff = pd.Timestamp(f"{year}-01-01")
        candidates = []
        for i, (params, trades, missed) in enumerate(cache):
            score = conservative_score(trades, cutoff, min_n=min_train_trades)
            if np.isfinite(score):
                candidates.append((score, -i, i, params, trades, missed))
        if not candidates:
            picks.append({"年": year, "状态": "训练已完结交易不足", "当年笔数": 0})
            continue
        score, _, _, params, trades, missed = max(candidates, key=lambda x: (x[0], x[1]))
        year_trades = trades[
            pd.to_datetime(trades[decision_date_col]).dt.year == year
        ].copy()
        year_trades["选择年"] = year
        for key, value in params.items():
            year_trades[f"参数_{key}"] = value
        oos.append(year_trades)
        picks.append(
            {
                "年": year,
                "状态": "已选择",
                "训练保守分": round(score, 4),
                **params,
                "当年笔数": len(year_trades),
            }
        )
    oos_trades = pd.concat(oos, ignore_index=True) if oos else pd.DataFrame()
    return oos_trades, pd.DataFrame(picks)


def selected_year_baseline(
    baseline_trades: pd.DataFrame, picks: pd.DataFrame, decision_date_col: str = "信号日"
) -> pd.DataFrame:
    years = set(picks.loc[picks["状态"] == "已选择", "年"].astype(int))
    if baseline_trades.empty or not years:
        return pd.DataFrame(columns=baseline_trades.columns)
    return baseline_trades[
        pd.to_datetime(baseline_trades[decision_date_col]).dt.year.isin(years)
    ].copy()


def overlap_count(trades: pd.DataFrame) -> int:
    """检查一条 OOS 轨迹是否存在重叠持仓。"""
    if len(trades) < 2:
        return 0
    ordered = trades.sort_values(["进场日", "出场日"]).reset_index(drop=True)
    entries = pd.to_datetime(ordered["进场日"])
    exits = pd.to_datetime(ordered["出场日"])
    return int((entries.iloc[1:].to_numpy() <= exits.iloc[:-1].to_numpy()).sum())


def markdown_table(frame: pd.DataFrame) -> str:
    """无第三方 ``tabulate`` 依赖的紧凑 Markdown 表格。"""
    df = frame.copy()

    def render(value: Any) -> str:
        if pd.isna(value):
            return ""
        if isinstance(value, (float, np.floating)):
            text = f"{float(value):.6g}"
        else:
            text = str(value)
        return text.replace("|", "\\|").replace("\n", "<br>")

    headers = [render(column) for column in df.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in df.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(render(value) for value in row) + " |")
    return "\n".join(lines)


def _smoke_main() -> int:
    ctx = load_context()
    model = build_event_model(ctx)
    config = TradeConfig(exact_trigger_window=True)
    trades, missed = run_trades_yearly(
        ctx,
        {
            year: (model, config, "abandon")
            for year in range(WF_START, ctx.dates[-1].year + 1)
        },
    )
    print("v43_core 基线冒烟：", summarize(trades, missed))
    return 0


if __name__ == "__main__":
    raise SystemExit(_smoke_main())
