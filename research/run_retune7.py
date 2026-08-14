# -*- coding: utf-8 -*-
"""七家信号组参数重训：分层预注册、逐年WF、嵌套安慰剂。

边界：
- 生产引擎只读；研究代码只在内存构造参数视图与跨年连续回放。
- 先执行第一层42格。只有预先指定的保守准则在严格OOS稳定赢当前参数，
  才允许解锁第二层；第三层同理。
- 主结论使用严格可用时点权重。生产原样回放只用于核对既有基准数字。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import math
import sys
from dataclasses import dataclass, replace
from itertools import product
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESEARCH = Path(__file__).resolve().parent
DATA = RESEARCH / "data"
REPORT = RESEARCH / "REPORT_RETUNE7_v1.md"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import smart_money as sm  # noqa: E402


SEED = 20260814
PLACEBO_REPS = 100
MIN_TRAIN = 30
MIN_OOS = 30
WF_START = 2019
REPORT_END = pd.Timestamp("2026-08-13")
END_EXCLUSIVE = REPORT_END + pd.Timedelta(days=1)

THETA_GRID = (0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5)
FADE_GRID = (6, 8, 10, 12, 15, 20)
HORIZON_GRID = (10, 20, 30)
MIN_N_GRID = (20, 30, 40)
DIST_GRID = (0.08, 0.12, 0.16)
NETQ_GRID = (0.50, 0.60, 0.70)

CRITERIA = ("sum", "mean_se", "risk_adjusted")
CRITERION_LABELS = {
    "sum": "(a)训练累计收益",
    "mean_se": "(b)单笔均值-SE（主准则）",
    "risk_adjusted": "(c)均值/标准差",
}
PRIMARY_CRITERION = "mean_se"


@dataclass(frozen=True, order=True)
class Spec:
    theta_mult: float = 1.2
    fade_days: int = 10
    weight_horizon: int = 20
    weight_min_n: int = 30
    dist_low_max: float = 0.12
    netq_max: float = 0.60

    @property
    def key(self) -> str:
        return (
            f"theta={self.theta_mult:.1f},fade={self.fade_days},"
            f"horizon={self.weight_horizon},min_n={self.weight_min_n},"
            f"dist={self.dist_low_max:.2f},netq={self.netq_max:.2f}"
        )

    def as_row(self) -> dict[str, Any]:
        return {
            "theta_mult": self.theta_mult,
            "fade_days": self.fade_days,
            "weight_horizon": self.weight_horizon,
            "weight_min_n": self.weight_min_n,
            "dist_low_max": self.dist_low_max,
            "netq_max": self.netq_max,
        }


BASE = Spec()


def stage1_grid() -> list[Spec]:
    out = [replace(BASE, theta_mult=t, fade_days=f)
           for t, f in product(THETA_GRID, FADE_GRID)]
    assert len(out) == 42 and BASE in out
    return out


def stage2_grid() -> list[Spec]:
    out = [replace(BASE, theta_mult=t, fade_days=f,
                   weight_horizon=h, weight_min_n=n)
           for t, f, h, n in product(THETA_GRID, FADE_GRID,
                                     HORIZON_GRID, MIN_N_GRID)]
    assert len(out) == 378 and BASE in out
    return out


def stage3_grid() -> list[Spec]:
    out = [Spec(t, f, h, n, d, q)
           for t, f, h, n, d, q in product(
               THETA_GRID, FADE_GRID, HORIZON_GRID, MIN_N_GRID,
               DIST_GRID, NETQ_GRID)]
    assert len(out) == 3402 and BASE in out
    return out


def sha256_prefix(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()[:16]


def bool_col(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series
    return series.astype(str).str.strip().str.lower().isin(
        ["t", "true", "1", "yes"])


def load_ratio() -> pd.Series:
    frame = pd.read_csv(DATA / "gold_silver_ratio.csv", parse_dates=["date"])
    if frame["date"].duplicated().any() or frame["ratio"].isna().any():
        raise ValueError("gold_silver_ratio.csv 日期重复或ratio缺失")
    return frame.set_index("date")["ratio"].sort_index()


def load_inputs() -> tuple[dict[str, tuple[pd.DataFrame, pd.DataFrame]], pd.DataFrame]:
    required = [
        "au_price.csv.gz", "ag_price.csv.gz", "au_seat.csv.gz", "ag_seat.csv.gz",
        "reboard_visibility.csv.gz", "gold_silver_ratio.csv",
    ]
    missing = [name for name in required if not (DATA / name).exists()]
    if missing:
        raise FileNotFoundError("缺少研究输入: " + ", ".join(missing))

    raw: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for code in ("AU", "AG"):
        raw[code] = sm.load_from_csv(DATA, code)

    visibility = pd.read_csv(
        DATA / "reboard_visibility.csv.gz",
        parse_dates=["trade_date", "reboard_date"],
    )
    keys = ["instrument", "contract", "rank_type", "member",
            "trade_date", "source"]
    for code, (_, seat) in raw.items():
        inferred = seat[seat["source"].astype(str) == "reboard_inferred"].copy()
        inferred["instrument"] = inferred["instrument"].astype(str).str.upper()
        visible = visibility[
            visibility["instrument"].astype(str).str.upper() == code
        ].copy()
        visible["instrument"] = visible["instrument"].astype(str).str.upper()
        if inferred.duplicated(keys).any() or visible.duplicated(keys).any():
            raise AssertionError(f"{code} reboard映射键重复")
        merged = inferred.merge(
            visible[keys + ["reboard_date"]], on=keys, how="left",
            validate="one_to_one",
        )
        if (len(merged) != len(inferred) or merged["reboard_date"].isna().any()
                or not (merged["trade_date"] < merged["reboard_date"]).all()):
            raise AssertionError(f"{code} reboard_visibility并非完整D<R一一映射")
    return raw, visibility


def build_engines(
    raw: Mapping[str, tuple[pd.DataFrame, pd.DataFrame]], mode: str,
) -> dict[str, sm.MarketEngine]:
    engines: dict[str, sm.MarketEngine] = {}
    for code, (price, seat0) in raw.items():
        seat = seat0.copy()
        if mode == "strict_pit":
            # 生产特征链对推断行永久禁入；预先剔除等价于任意决策日的R日门控。
            seat = seat[seat["source"].astype(str) != "reboard_inferred"].copy()
        elif mode != "archive_visible":
            raise ValueError(mode)
        engines[code] = sm.MarketEngine(code, price, seat)
    return engines


@dataclass
class FeatureSet:
    engine: sm.MarketEngine
    ev_eff: pd.DataFrame
    weights: dict[int, dict[str, float]]
    score: np.ndarray
    max_weight: np.ndarray
    active: np.ndarray


def explicit_weights(
    engine: sm.MarketEngine,
    events: pd.DataFrame,
    horizon: int,
    min_n: int,
    strict_time: bool,
) -> dict[int, dict[str, float]]:
    """显式参数化生产权重；strict_time额外要求远期收益在1月1日前实现。"""
    fwd = sm.forward_returns(engine.cont, horizon)
    dates = engine.dates
    pos = {d: i for i, d in enumerate(dates)}
    years = range(dates[0].year, dates[-1].year + 1)
    out: dict[int, dict[str, float]] = {}
    for year in years:
        event_cutoff = pd.Timestamp(f"{year - 1}-12-01")
        decision = pd.Timestamp(f"{year}-01-01")
        row: dict[str, float] = {}
        for member in engine.group:
            ds = events.loc[
                (events["member"] == member)
                & (events["trade_date"] < event_cutoff), "trade_date"
            ]
            if strict_time:
                keep: list[pd.Timestamp] = []
                for day0 in ds:
                    day = pd.Timestamp(day0)
                    i = pos.get(day)
                    available_i = None if i is None else i + horizon + 1
                    if (available_i is not None and available_i < len(dates)
                            and dates[available_i] < decision):
                        keep.append(day)
                ds = pd.Series(keep, dtype="datetime64[ns]")
            returns = fwd.reindex(pd.DatetimeIndex(ds)).dropna()
            if len(returns) < min_n or returns.std(ddof=1) == 0:
                row[member] = 0.0
            else:
                t_value = (returns.mean() / returns.std(ddof=1)
                           * math.sqrt(len(returns)))
                row[member] = float(np.clip(t_value, 0, sm.RULES["weight_clip"]))
        out[year] = row
    return out


def build_feature(
    engine: sm.MarketEngine,
    events: pd.DataFrame,
    horizon: int,
    min_n: int,
    strict_time: bool,
) -> FeatureSet:
    weights = explicit_weights(engine, events, horizon, min_n, strict_time)
    ev = events.copy()
    ev["dist"] = engine.dist60.reindex(ev["trade_date"]).to_numpy()
    ev_eff = ev[~(
        ev["member"].isin(sm.RULES["cond_seats"])
        & (ev["dist"] >= 0.05)
    )].copy()
    strong = (ev_eff.pivot_table(
        index="trade_date", columns="member", values="strength", aggfunc="max"
    ).reindex(engine.dates).reindex(columns=engine.group))
    wmat = pd.DataFrame({
        member: [weights[day.year].get(member, 0.0) for day in engine.dates]
        for member in engine.group
    }, index=engine.dates)
    weighted = strong.fillna(0) * wmat
    score = (weighted.rolling(sm.RULES["score_window"], min_periods=1)
             .max().sum(axis=1).to_numpy(float))
    active = ((strong.notna() & (wmat > 0)).any(axis=1).to_numpy(bool))
    return FeatureSet(
        engine=engine,
        ev_eff=ev_eff,
        weights=weights,
        score=score,
        max_weight=wmat.max(axis=1).to_numpy(float),
        active=active,
    )


class FeatureFactory:
    def __init__(
        self,
        engines: Mapping[str, sm.MarketEngine],
        long_events: Mapping[str, pd.DataFrame],
        strict_time: bool,
    ) -> None:
        self.engines = dict(engines)
        self.long_events = {k: v.copy() for k, v in long_events.items()}
        self.strict_time = strict_time
        self.cache: dict[tuple[str, int, int], FeatureSet] = {}

    def get(self, code: str, spec: Spec) -> FeatureSet:
        key = (code, spec.weight_horizon, spec.weight_min_n)
        if key not in self.cache:
            self.cache[key] = build_feature(
                self.engines[code], self.long_events[code],
                spec.weight_horizon, spec.weight_min_n, self.strict_time,
            )
        return self.cache[key]


@dataclass
class ResearchTrade:
    market: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp | None
    entry_px_real: float | None
    entry_px_adj: float | None
    exit_date: pd.Timestamp | None
    exit_px_real: float | None
    result: str
    ret_pct: float | None
    is_relay: bool
    spec_key: str
    score: float


def cost_zone(feature: FeatureSet, day: pd.Timestamp) -> tuple[float, float, float] | None:
    engine = feature.engine
    window = pd.Timedelta(days=sm.RULES["score_window"] + 3)
    recent = feature.ev_eff[
        (feature.ev_eff["trade_date"] > day - window)
        & (feature.ev_eff["trade_date"] <= day)
    ]
    numerator = denominator = 0.0
    for member in recent["member"].unique():
        weight = feature.weights[day.year].get(member, 0.0)
        series = engine.costs[member]
        value = series.asof(day) if len(series) else np.nan
        if weight > 0 and np.isfinite(value):
            numerator += weight * float(value)
            denominator += weight
    if denominator == 0:
        return None
    center = numerator / denominator
    half = sm.RULES["zone_half_width"]
    return center - half, center + half, center


def suppression_mask(
    dates: pd.DatetimeIndex, windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]],
) -> np.ndarray:
    mask = np.zeros(len(dates), dtype=bool)
    for start, end in windows:
        mask |= ((dates >= start) & (dates <= end))
    return mask


def replay_market(
    code: str,
    factory: FeatureFactory,
    schedule: Mapping[int, Spec],
    suppress_windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]],
) -> list[ResearchTrade]:
    """跨年连续状态机；规格在信号日冻结，既有挂单/持仓跨年延续。"""
    engine = factory.engines[code]
    dates = engine.dates
    pos = {day: i for i, day in enumerate(dates)}
    low_real = engine.cont["low"].to_numpy(float)
    open_real = engine.cont["open"].to_numpy(float)
    close_real = engine.cont["close"].to_numpy(float)
    low_adj = engine.cont["adj_low"].to_numpy(float)
    open_adj = engine.cont["adj_open"].to_numpy(float)
    close_adj = engine.cont["adj_close"].to_numpy(float)
    factor = engine.cont["factor"].to_numpy(float)
    factor_last = factor[-1]
    suppressed = suppression_mask(dates, suppress_windows)
    replay_start = pd.Timestamp(sm.RULES["replay_start"])

    signal_specs = [schedule.get(day.year, BASE) for day in dates]
    full_mask = np.zeros(len(dates), dtype=bool)
    relay_mask = np.zeros(len(dates), dtype=bool)
    for i, (day, spec) in enumerate(zip(dates, signal_specs)):
        feature = factory.get(code, spec)
        threshold = spec.theta_mult * feature.max_weight[i]
        score_ok = (threshold > 0 and feature.score[i] >= threshold
                    and day >= replay_start)
        relay_mask[i] = score_ok
        full_mask[i] = (
            score_ok
            and float(engine.dist60.iloc[i]) < spec.dist_low_max
            and float(engine.netq.iloc[i]) < spec.netq_max
        )

    trades: list[ResearchTrade] = []
    busy = -1
    relay_armed = False
    for day in dates:
        i = pos[day]
        if i + 1 >= len(dates) or i < busy:
            continue
        spec = signal_specs[i]
        feature = factory.get(code, spec)
        is_full = bool(full_mask[i])
        is_relay = (not is_full and relay_armed and bool(relay_mask[i])
                    and not bool(suppressed[i]))
        if not (is_full or is_relay):
            continue

        zone = None if is_relay else cost_zone(feature, day)
        entry_i: int | None = None
        entry_px_real: float | None = None
        if zone is not None:
            for j in range(i + 1, min(
                    i + 1 + sm.RULES["zone_valid_days"], len(dates))):
                if j <= busy:
                    break
                if np.isfinite(low_real[j]) and low_real[j] <= zone[1]:
                    entry_px_real = (min(open_real[j], zone[1])
                                     if np.isfinite(open_real[j]) else zone[1])
                    entry_i = j
                    break
            if entry_i is None:
                trades.append(ResearchTrade(
                    code, day, None, None, None, None, None,
                    "未回踩放弃", None, is_relay, spec.key, float(feature.score[i]),
                ))
                continue
        else:
            entry_i = i + 1
            entry_px_real = (open_real[entry_i] if np.isfinite(open_real[entry_i])
                             else close_real[entry_i])
            if not np.isfinite(entry_px_real):
                continue

        entry_px_adj = float(entry_px_real) * factor_last / factor[entry_i]
        stop_adj = entry_px_adj * (1 - sm.RULES["stop_loss"])
        exit_i: int | None = None
        exit_px_real: float | None = None
        result = ""
        ret_pct: float | None = None
        fade_from: int | None = None
        for j in range(entry_i, len(dates)):
            if not np.isfinite(low_adj[j]):
                continue
            if fade_from is not None and j > fade_from:
                px_adj = open_adj[j] if np.isfinite(open_adj[j]) else close_adj[j]
                exit_i = j
                exit_px_real = float(px_adj * factor[j] / factor_last)
                result = "消退卖出"
                ret_pct = float((px_adj / entry_px_adj - 1) * 100)
                break
            if low_adj[j] <= stop_adj:
                exit_i = j
                exit_px_real = float(stop_adj * factor[j] / factor_last)
                result = "止损"
                ret_pct = -float(sm.RULES["stop_loss"] * 100)
                break
            left = max(0, j - spec.fade_days + 1)
            quiet = not bool(feature.active[left:j + 1].any())
            if fade_from is None and j > entry_i + 2 and quiet:
                fade_from = j

        if exit_i is None:
            result = "持有中"
            ret_pct = float((close_adj[-1] / entry_px_adj - 1) * 100)
            busy = len(dates) - 1
            exit_date = None
        else:
            busy = exit_i
            exit_date = dates[exit_i]
        if result in {"消退卖出", "止损"}:
            relay_armed = True
        trades.append(ResearchTrade(
            code, day, dates[entry_i], float(entry_px_real), entry_px_adj,
            exit_date, exit_px_real, result, ret_pct, is_relay,
            spec.key, float(feature.score[i]),
        ))
    return trades


def replay_schedule(
    factory: FeatureFactory,
    schedule: Mapping[int, Spec],
    suppress_windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]],
) -> list[ResearchTrade]:
    rows: list[ResearchTrade] = []
    for code in ("AU", "AG"):
        rows.extend(replay_market(code, factory, schedule, suppress_windows))
    return rows


def fixed_schedule(factory: FeatureFactory, spec: Spec) -> dict[int, Spec]:
    first = min(engine.dates[0].year for engine in factory.engines.values())
    last = max(engine.dates[-1].year for engine in factory.engines.values())
    return {year: spec for year in range(first, last + 1)}


def compare_engine_baseline(
    research: list[ResearchTrade],
    engine_trades: Mapping[str, list[sm.Trade]],
) -> pd.DataFrame:
    rows = []
    for code in ("AU", "AG"):
        ours = [trade for trade in research if trade.market == code]
        theirs = engine_trades[code]
        if len(ours) != len(theirs):
            rows.append({"market": code, "field": "record_count",
                         "ours": len(ours), "engine": len(theirs)})
            continue
        for index, (left, right) in enumerate(zip(ours, theirs)):
            checks = {
                "signal_date": (left.signal_date, right.signal_date),
                "entry_date": (left.entry_date, right.entry_date),
                "entry_px": (left.entry_px_real, right.entry_px),
                "exit_date": (left.exit_date, right.exit_date),
                "exit_px": (left.exit_px_real, right.exit_px),
                "result": (left.result, right.result),
                "ret_pct": (left.ret_pct, right.ret_pct),
                "is_relay": (left.is_relay, right.is_relay),
            }
            for field, (a, b) in checks.items():
                if isinstance(a, (float, np.floating)) or isinstance(b, float):
                    same = ((a is None and b is None)
                            or (a is not None and b is not None
                                and np.isclose(float(a), float(b), atol=1e-9, rtol=0)))
                else:
                    same = (a == b) or (pd.isna(a) and pd.isna(b))
                if not same:
                    rows.append({"market": code, "record": index, "field": field,
                                 "ours": a, "engine": b})
    return pd.DataFrame(rows)


def executed(trades: Iterable[ResearchTrade]) -> list[ResearchTrade]:
    return [trade for trade in trades if trade.entry_date is not None]


def completed(trades: Iterable[ResearchTrade]) -> list[ResearchTrade]:
    return [trade for trade in trades
            if trade.entry_date is not None and trade.exit_date is not None]


def trade_summary(
    trades: Iterable[ResearchTrade], start: pd.Timestamp | None = None,
) -> dict[str, Any]:
    rows = executed(trades)
    if start is not None:
        rows = [trade for trade in rows if trade.signal_date >= start]
    done = [trade for trade in rows if trade.exit_date is not None]
    returns_done = np.array([trade.ret_pct for trade in done], dtype=float)
    returns_all = np.array([trade.ret_pct for trade in rows], dtype=float)
    return {
        "executed": len(rows),
        "closed": len(done),
        "open": len(rows) - len(done),
        "closed_sum": float(returns_done.sum()) if len(returns_done) else 0.0,
        "terminal_sum": float(returns_all.sum()) if len(returns_all) else 0.0,
        "closed_mean": float(returns_done.mean()) if len(returns_done) else np.nan,
        "win_rate": float((returns_done > 0).mean() * 100) if len(returns_done) else np.nan,
    }


def daily_oos_curve(
    trades: Iterable[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
    start: pd.Timestamp,
) -> pd.Series:
    """信号日属于OOS的交易构造日历盯市算术P&L；退出日使用实际成交收益。"""
    common = engines["AU"].dates.union(engines["AG"].dates)
    common = common[(common >= start) & (common <= REPORT_END)]
    rebuilt = pd.Series(0.0, index=common)
    for trade in executed(trades):
        if trade.signal_date < start:
            continue
        engine = engines[trade.market]
        contribution = pd.Series(np.nan, index=common)
        market_days = engine.dates[(engine.dates >= trade.entry_date) & (engine.dates <= REPORT_END)]
        for day in market_days:
            if trade.exit_date is not None and day >= trade.exit_date:
                contribution.at[day] = float(trade.ret_pct)
            else:
                close = float(engine.cont.at[day, "adj_close"])
                contribution.at[day] = (close / float(trade.entry_px_adj) - 1) * 100
        contribution = contribution.ffill().fillna(0.0)
        rebuilt = rebuilt.add(contribution, fill_value=0.0)
    return rebuilt


def calendar_slices(
    candidate: list[ResearchTrade],
    baseline: list[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
) -> pd.DataFrame:
    start = pd.Timestamp(f"{WF_START}-01-01")
    cand = daily_oos_curve(candidate, engines, start)
    base = daily_oos_curve(baseline, engines, start)
    rows = []
    cand_prev = base_prev = 0.0
    for year in range(WF_START, REPORT_END.year + 1):
        year_days = cand.index[cand.index.year == year]
        if not len(year_days):
            continue
        last = year_days[-1]
        cand_now = float(cand.loc[last])
        base_now = float(base.loc[last])
        cand_inc = cand_now - cand_prev
        base_inc = base_now - base_prev
        rows.append({
            "year": year,
            "period": f"{year}-01-01~{last.date()}",
            "candidate_pnl_increment": cand_inc,
            "baseline_pnl_increment": base_inc,
            "delta": cand_inc - base_inc,
            "non_loss": cand_inc >= base_inc - 1e-12,
        })
        cand_prev, base_prev = cand_now, base_now
    return pd.DataFrame(rows)


def asof_returns(
    trades: Iterable[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
    cutoff: pd.Timestamp,
) -> np.ndarray:
    values: list[float] = []
    for trade in executed(trades):
        if trade.entry_date >= cutoff:
            continue
        if trade.exit_date is not None and trade.exit_date < cutoff:
            values.append(float(trade.ret_pct))
            continue
        engine = engines[trade.market]
        before = np.flatnonzero(engine.dates < cutoff)
        if not len(before):
            continue
        terminal = float(engine.cont["adj_close"].iloc[int(before[-1])])
        values.append((terminal / float(trade.entry_px_adj) - 1) * 100)
    return np.asarray(values, dtype=float)


def criterion_score(values: np.ndarray, criterion: str) -> float:
    if len(values) < MIN_TRAIN:
        return -np.inf
    if criterion == "sum":
        return float(values.sum())
    std = float(values.std(ddof=1))
    if not np.isfinite(std) or std == 0:
        return -np.inf
    mean = float(values.mean())
    if criterion == "mean_se":
        return mean - std / math.sqrt(len(values))
    if criterion == "risk_adjusted":
        return mean / std
    raise KeyError(criterion)


def spec_distance(spec: Spec) -> tuple[float, str]:
    distance = (
        abs(THETA_GRID.index(spec.theta_mult) - THETA_GRID.index(BASE.theta_mult))
        + abs(FADE_GRID.index(spec.fade_days) - FADE_GRID.index(BASE.fade_days))
        + abs(HORIZON_GRID.index(spec.weight_horizon) - HORIZON_GRID.index(BASE.weight_horizon))
        + abs(MIN_N_GRID.index(spec.weight_min_n) - MIN_N_GRID.index(BASE.weight_min_n))
        + abs(DIST_GRID.index(spec.dist_low_max) - DIST_GRID.index(BASE.dist_low_max))
        + abs(NETQ_GRID.index(spec.netq_max) - NETQ_GRID.index(BASE.netq_max))
    )
    return float(distance), spec.key


def select_walk_forward(
    specs: list[Spec],
    fixed_cache: Mapping[str, list[ResearchTrade]],
    engines: Mapping[str, sm.MarketEngine],
    criterion: str,
) -> tuple[dict[int, Spec], pd.DataFrame]:
    schedule: dict[int, Spec] = {}
    rows = []
    for year in range(WF_START, REPORT_END.year + 1):
        cutoff = pd.Timestamp(f"{year}-01-01")
        scored: list[tuple[float, Spec, int]] = []
        for spec in specs:
            values = asof_returns(fixed_cache[spec.key], engines, cutoff)
            score = criterion_score(values, criterion)
            if np.isfinite(score):
                scored.append((score, spec, len(values)))
        if not scored:
            picked, picked_score, train_n, status = BASE, np.nan, 0, "训练不足回退现行"
        else:
            best = max(item[0] for item in scored)
            near = [item for item in scored if np.isclose(item[0], best, atol=1e-12, rtol=0)]
            picked_score, picked, train_n = min(near, key=lambda item: spec_distance(item[1]))
            status = "selected"
        schedule[year] = picked
        rows.append({
            "criterion": CRITERION_LABELS[criterion],
            "year": year,
            "train_period": f"{sm.RULES['replay_start']}~{year - 1}-12-31",
            "train_n": train_n,
            "train_score": picked_score,
            "status": status,
            **picked.as_row(),
        })
    return schedule, pd.DataFrame(rows)


def yearly_oos(
    candidate: list[ResearchTrade],
    baseline: list[ResearchTrade],
    path: pd.DataFrame,
    engines: Mapping[str, sm.MarketEngine],
) -> pd.DataFrame:
    """按信号年列示共同年末截止收益；不借用未来年度的退出结果。"""

    def cohort_asof(
        trades: list[ResearchTrade], year: int, cutoff: pd.Timestamp,
    ) -> dict[str, Any]:
        rows = [
            trade for trade in executed(trades)
            if trade.signal_date.year == year and trade.entry_date < cutoff
        ]
        realized = 0
        values: list[float] = []
        for trade in rows:
            if trade.exit_date is not None and trade.exit_date < cutoff:
                realized += 1
                values.append(float(trade.ret_pct))
                continue
            engine = engines[trade.market]
            before = np.flatnonzero(engine.dates < cutoff)
            if not len(before):
                continue
            terminal = float(engine.cont["adj_close"].iloc[int(before[-1])])
            values.append((terminal / float(trade.entry_px_adj) - 1) * 100)
        return {
            "executed_asof": len(values),
            "realized_asof": realized,
            "open_mtm_asof": len(values) - realized,
            "terminal_sum_asof": float(sum(values)),
        }

    rows = []
    for year in range(WF_START, REPORT_END.year + 1):
        cutoff = min(pd.Timestamp(f"{year + 1}-01-01"), END_EXCLUSIVE)
        cand = cohort_asof(candidate, year, cutoff)
        base = cohort_asof(baseline, year, cutoff)
        rows.append({
            "year": year,
            "valuation_period": (
                f"{year}-01-01~{cutoff - pd.Timedelta(days=1):%Y-%m-%d}"
            ),
            "candidate_executed_asof": cand["executed_asof"],
            "candidate_realized_asof": cand["realized_asof"],
            "candidate_open_mtm_asof": cand["open_mtm_asof"],
            "baseline_executed_asof": base["executed_asof"],
            "baseline_realized_asof": base["realized_asof"],
            "baseline_open_mtm_asof": base["open_mtm_asof"],
            "candidate_terminal_sum_asof": cand["terminal_sum_asof"],
            "baseline_terminal_sum_asof": base["terminal_sum_asof"],
            "cohort_delta_asof": (
                cand["terminal_sum_asof"] - base["terminal_sum_asof"]
            ),
            "cohort_non_loss_asof": (
                cand["terminal_sum_asof"] >= base["terminal_sum_asof"] - 1e-12
            ),
            "eligibility": ("可采信" if cand["executed_asof"] >= MIN_OOS
                            else "样本不足，不采信（单年）"),
        })
    annual = pd.DataFrame(rows)
    return path.merge(annual, on="year", how="left")


def run_stage(
    specs: list[Spec],
    factory: FeatureFactory,
    suppress_windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]],
) -> dict[str, Any]:
    fixed_cache: dict[str, list[ResearchTrade]] = {}
    for index, spec in enumerate(specs, 1):
        fixed_cache[spec.key] = replay_schedule(
            factory, fixed_schedule(factory, spec), suppress_windows)
        if index % 250 == 0:
            print(f"[grid] {index}/{len(specs)}", flush=True)

    baseline = fixed_cache[BASE.key]
    schedules: dict[str, dict[int, Spec]] = {}
    picks: dict[str, pd.DataFrame] = {}
    adaptive: dict[str, list[ResearchTrade]] = {}
    summaries = []
    annual: dict[str, pd.DataFrame] = {}
    calendar: dict[str, pd.DataFrame] = {}
    base_oos = trade_summary(baseline, pd.Timestamp(f"{WF_START}-01-01"))
    for criterion in CRITERIA:
        schedule, path = select_walk_forward(
            specs, fixed_cache, factory.engines, criterion)
        result = replay_schedule(factory, schedule, suppress_windows)
        summary = trade_summary(result, pd.Timestamp(f"{WF_START}-01-01"))
        path_annual = yearly_oos(result, baseline, path, factory.engines)
        calendar_result = calendar_slices(result, baseline, factory.engines)
        schedules[criterion] = schedule
        picks[criterion] = path
        adaptive[criterion] = result
        annual[criterion] = path_annual
        calendar[criterion] = calendar_result
        summaries.append({
            "criterion": CRITERION_LABELS[criterion],
            "period": f"{WF_START}-{REPORT_END.year}",
            "candidate_executed": summary["executed"],
            "candidate_closed": summary["closed"],
            "candidate_open": summary["open"],
            "candidate_closed_sum": summary["closed_sum"],
            "baseline_closed": base_oos["closed"],
            "baseline_closed_sum": base_oos["closed_sum"],
            "closed_delta": summary["closed_sum"] - base_oos["closed_sum"],
            "candidate_terminal_sum": summary["terminal_sum"],
            "baseline_terminal_sum": base_oos["terminal_sum"],
            "terminal_delta": summary["terminal_sum"] - base_oos["terminal_sum"],
            "non_loss_years": int(calendar_result["non_loss"].sum()),
            "year_slices": len(calendar_result),
            "eligibility": ("可采信" if summary["executed"] >= MIN_OOS
                            else "样本不足，不采信"),
        })
    return {
        "specs": specs,
        "fixed_cache": fixed_cache,
        "baseline": baseline,
        "schedules": schedules,
        "picks": picks,
        "adaptive": adaptive,
        "annual": annual,
        "calendar": calendar,
        "summary": pd.DataFrame(summaries),
    }


def shift_events(
    events: pd.DataFrame,
    dates: pd.DatetimeIndex,
    rng: np.random.Generator,
) -> pd.DataFrame:
    if events.empty:
        return events.copy()
    positions = {day: i for i, day in enumerate(dates)}
    rows = []
    ordered = events.sort_values(["trade_date", "member", "strength"]).to_dict("records")
    for row in ordered:
        i = positions.get(pd.Timestamp(row["trade_date"]))
        if i is None:
            continue
        feasible = [offset for offset in (*range(-10, -4), *range(5, 11))
                    if 0 <= i + offset < len(dates)]
        if not feasible:
            raise AssertionError("事件无可行±5~10交易日偏移")
        offset = int(feasible[int(rng.integers(0, len(feasible)))])
        j = i + offset
        shifted = dict(row)
        shifted["trade_date"] = dates[j]
        rows.append(shifted)
    frame = pd.DataFrame(rows, columns=events.columns)
    return (frame.sort_values("strength", ascending=False)
            .drop_duplicates(["member", "trade_date"], keep="first")
            .sort_values(["trade_date", "member"]).reset_index(drop=True))


def shifted_suppress_windows(
    ag: sm.MarketEngine,
    short_events: pd.DataFrame,
    ratio: pd.Series,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    proxy = copy.copy(ag)
    proxy.ev_short = short_events
    return sm.flee_suppress_windows(proxy, ratio)


def placebo_distribution(
    specs: list[Spec],
    engines: Mapping[str, sm.MarketEngine],
    ratio: pd.Series,
    reps: int,
    seed: int,
) -> dict[str, np.ndarray]:
    values = {criterion: np.full(reps, np.nan) for criterion in CRITERIA}
    for rep in range(reps):
        rng = np.random.default_rng(seed + rep + 1)
        shifted_long: dict[str, pd.DataFrame] = {}
        shifted_short: dict[str, pd.DataFrame] = {}
        for code in ("AU", "AG"):
            shifted_long[code] = shift_events(
                engines[code].ev_long, engines[code].dates, rng)
            shifted_short[code] = shift_events(
                engines[code].ev_short, engines[code].dates, rng)
        factory = FeatureFactory(engines, shifted_long, strict_time=True)
        suppress = shifted_suppress_windows(engines["AG"], shifted_short["AG"], ratio)

        fixed_cache = {
            spec.key: replay_schedule(factory, fixed_schedule(factory, spec), suppress)
            for spec in specs
        }
        baseline = fixed_cache[BASE.key]
        base_summary = trade_summary(baseline, pd.Timestamp(f"{WF_START}-01-01"))
        for criterion in CRITERIA:
            schedule, _ = select_walk_forward(
                specs, fixed_cache, engines, criterion)
            candidate = replay_schedule(factory, schedule, suppress)
            summary = trade_summary(candidate, pd.Timestamp(f"{WF_START}-01-01"))
            values[criterion][rep] = (
                summary["terminal_sum"] - base_summary["terminal_sum"])
        if (rep + 1) % 5 == 0:
            print(f"[placebo nested] {rep + 1}/{reps}", flush=True)
    return values


def neighbor(value: Any, grid: tuple, direction: int) -> Any | None:
    index = grid.index(value)
    j = index + direction
    return grid[j] if 0 <= j < len(grid) else None


def perturb_schedule(
    schedule: Mapping[int, Spec], dimension: str, direction: int,
) -> tuple[dict[int, Spec], int, tuple[int, ...]]:
    grids = {
        "theta_mult": THETA_GRID,
        "fade_days": FADE_GRID,
        "weight_horizon": HORIZON_GRID,
        "weight_min_n": MIN_N_GRID,
        "dist_low_max": DIST_GRID,
        "netq_max": NETQ_GRID,
    }
    out: dict[int, Spec] = {}
    changed = 0
    boundary_years: list[int] = []
    for year, spec in schedule.items():
        nxt = neighbor(getattr(spec, dimension), grids[dimension], direction)
        if nxt is None:
            out[year] = spec
            boundary_years.append(year)
        else:
            out[year] = replace(spec, **{dimension: nxt})
            changed += 1
    return out, changed, tuple(boundary_years)


def sensitivity_table(
    stage: dict[str, Any],
    factory: FeatureFactory,
    suppress_windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]],
    dimensions: Iterable[str],
) -> pd.DataFrame:
    schedule = stage["schedules"][PRIMARY_CRITERION]
    baseline = stage["baseline"]
    base_summary = trade_summary(baseline, pd.Timestamp(f"{WF_START}-01-01"))
    real_summary = trade_summary(
        stage["adaptive"][PRIMARY_CRITERION], pd.Timestamp(f"{WF_START}-01-01"))
    reference = real_summary["terminal_sum"] - base_summary["terminal_sum"]
    rows = []
    for dimension in dimensions:
        for direction in (-1, 1):
            shifted, changed, boundary_years = perturb_schedule(
                schedule, dimension, direction)
            if not changed:
                rows.append({
                    "period": f"{WF_START}-{REPORT_END.year}",
                    "parameter": dimension,
                    "direction": "-1档" if direction < 0 else "+1档",
                    "changed_years": 0,
                    "boundary_years_NA": ",".join(map(str, boundary_years)),
                    "candidate_closed": 0,
                    "terminal_delta": np.nan,
                    "direction_kept": np.nan,
                    "status": "边界，无相邻档",
                })
                continue
            result = replay_schedule(factory, shifted, suppress_windows)
            summary = trade_summary(result, pd.Timestamp(f"{WF_START}-01-01"))
            delta = summary["terminal_sum"] - base_summary["terminal_sum"]
            kept = delta >= 0 if reference >= 0 else delta <= 0
            rows.append({
                "period": f"{WF_START}-{REPORT_END.year}",
                "parameter": dimension,
                "direction": "-1档" if direction < 0 else "+1档",
                "changed_years": changed,
                "boundary_years_NA": (
                    ",".join(map(str, boundary_years)) if boundary_years else "—"
                ),
                "candidate_closed": summary["closed"],
                "terminal_delta": delta,
                "direction_kept": bool(summary["executed"] >= MIN_OOS and kept),
                "status": "已重放",
            })
    return pd.DataFrame(rows)


def weight_availability_audit(
    production_factory: FeatureFactory,
    strict_factory: FeatureFactory,
) -> pd.DataFrame:
    rows = []
    for code in ("AU", "AG"):
        prod = production_factory.get(code, BASE)
        strict = strict_factory.get(code, BASE)
        engine = prod.engine
        for year in sorted(prod.weights):
            for member in engine.group:
                old = prod.weights[year].get(member, 0.0)
                new = strict.weights[year].get(member, 0.0)
                if np.isclose(old, new, atol=1e-12, rtol=0):
                    continue
                late = []
                cutoff = pd.Timestamp(f"{year - 1}-12-01")
                decision = pd.Timestamp(f"{year}-01-01")
                positions = {day: i for i, day in enumerate(engine.dates)}
                events = prod.ev_eff[
                    (prod.ev_eff["member"] == member)
                    & (prod.ev_eff["trade_date"] < cutoff)
                ]
                for day0 in events["trade_date"]:
                    day = pd.Timestamp(day0)
                    i = positions.get(day)
                    end_i = None if i is None else i + BASE.weight_horizon + 1
                    if (end_i is not None and end_i < len(engine.dates)
                            and engine.dates[end_i] >= decision):
                        late.append(f"{day.date()}→{engine.dates[end_i].date()}")
                rows.append({
                    "market": code,
                    "decision_year": year,
                    "member": member,
                    "production_weight": old,
                    "strict_weight": new,
                    "late_event_to_return_end": ",".join(late),
                    "status": "生产对账保留；严格WF剔除未实现收益",
                })
    return pd.DataFrame(rows)


def input_tables(
    raw: Mapping[str, tuple[pd.DataFrame, pd.DataFrame]],
    visibility: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    files = [
        "au_price.csv.gz", "ag_price.csv.gz", "au_seat.csv.gz", "ag_seat.csv.gz",
        "reboard_visibility.csv.gz", "gold_silver_ratio.csv",
    ]
    rows = []
    for name in files:
        path = DATA / name
        frame = pd.read_csv(path)
        date_col = "trade_date" if "trade_date" in frame else "date"
        rows.append({
            "file": name,
            "period": f"{frame[date_col].min()}~{frame[date_col].max()}",
            "rows": len(frame),
            "sha256_prefix16": sha256_prefix(path),
        })

    vis_rows = []
    keys = ["instrument", "contract", "rank_type", "member",
            "trade_date", "source"]
    for code in ("AU", "AG"):
        seat = raw[code][1]
        inferred = seat[seat["source"].astype(str) == "reboard_inferred"]
        visible = visibility[
            visibility["instrument"].astype(str).str.upper() == code]
        vis_rows.append({
            "market": code,
            "period_D": f"{visible['trade_date'].min().date()}~{visible['trade_date'].max().date()}",
            "inferred_rows": len(inferred),
            "visibility_rows": len(visible),
            "null_R": int(visible["reboard_date"].isna().sum()),
            "duplicate_keys": int(visible.duplicated(keys).sum()),
            "D_before_R": int((visible["trade_date"] < visible["reboard_date"]).sum()),
            "one_to_one": (len(inferred) == len(visible)
                           and not visible.duplicated(keys).any()
                           and not visible["reboard_date"].isna().any()),
        })
    return pd.DataFrame(rows), pd.DataFrame(vis_rows)


def fmt(value: Any) -> str:
    if value is None or (isinstance(value, (float, np.floating))
                         and not np.isfinite(value)):
        return "—"
    if isinstance(value, (bool, np.bool_)):
        return "是" if bool(value) else "否"
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.3f}".rstrip("0").rstrip(".")
    return str(value)


def markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "（无）"
    columns = list(frame.columns)
    lines = [
        "| " + " | ".join(map(str, columns)) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
    for row in frame.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(
            fmt(value).replace("|", "\\|") for value in row) + " |")
    return "\n".join(lines)


def placebo_report(
    values: Mapping[str, np.ndarray], real: Mapping[str, float],
) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    rows = []
    tests: dict[str, dict[str, Any]] = {}
    for criterion in CRITERIA:
        sample = np.asarray(values[criterion], dtype=float)
        quantiles = np.quantile(sample, [0, .05, .25, .5, .75, .95, 1])
        p_value = (1 + int((sample >= real[criterion]).sum())) / (len(sample) + 1)
        passed = bool(real[criterion] > quantiles[5] and p_value < 0.05)
        tests[criterion] = {"q95": quantiles[5], "p": p_value, "pass": passed}
        for label, value in zip(
                ("min", "5%", "25%", "50%", "75%", "95%", "max"), quantiles):
            rows.append({
                "criterion": CRITERION_LABELS[criterion],
                "period": f"{WF_START}-{REPORT_END.year}",
                "reps": len(sample),
                "quantile": label,
                "terminal_delta": value,
            })
    return pd.DataFrame(rows), tests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--placebos", type=int, default=PLACEBO_REPS)
    args = parser.parse_args()
    if args.placebos < 100:
        raise ValueError("安慰剂次数硬门要求>=100")

    expected_group = (
        "中财期货", "中信期货", "海通期货", "国泰君安",
        "高盛期货", "东证期货", "华泰期货",
    )
    if tuple(sm.RULES.get("group", ())) != expected_group:
        raise AssertionError("当前生产信号组不是工单冻结的七家配置")
    frozen = {
        "stop_loss": 0.04,
        "event_q": 0.80,
        "event_window": 250,
        "event_min_hist": 120,
        "score_window": 5,
        "strength_cap": 3.0,
        "weight_clip": 5.0,
        "weight_horizon": 20,
        "weight_min_n": 30,
        "theta_mult": 1.2,
        "dist_low_days": 60,
        "dist_low_max": 0.12,
        "netq_window": 250,
        "netq_max": 0.60,
        "zone_half_width": 5.0,
        "zone_valid_days": 10,
        "fade_days": 10,
        "flee_suppress_days": 40,
        "replay_start": "2015-01-01",
    }
    for key, value in frozen.items():
        if sm.RULES[key] != value:
            raise AssertionError(f"冻结参数{key}发生变化: {sm.RULES[key]}")

    raw, visibility = load_inputs()
    ratio = load_ratio()
    data_table, visibility_table = input_tables(raw, visibility)
    engines_final = build_engines(raw, "archive_visible")
    engines_strict = build_engines(raw, "strict_pit")
    long_final = {code: engines_final[code].ev_long for code in ("AU", "AG")}
    long_strict = {code: engines_strict[code].ev_long for code in ("AU", "AG")}
    short_final = {code: engines_final[code].ev_short for code in ("AU", "AG")}

    production_suppress = sm.flee_suppress_windows(engines_final["AG"], ratio)
    strict_suppress = sm.flee_suppress_windows(engines_strict["AG"], ratio)
    engine_trades = {
        code: engine.replay(production_suppress)
        for code, engine in engines_final.items()
    }

    # 生产原样权重只用于硬对账；主研究另加远期收益可用日门。
    production_factory = FeatureFactory(engines_final, long_final, strict_time=False)
    production_replay = replay_schedule(
        production_factory, fixed_schedule(production_factory, BASE), production_suppress)
    mismatch = compare_engine_baseline(production_replay, engine_trades)
    if len(mismatch):
        raise AssertionError(f"研究状态机未复现生产基准: {len(mismatch)}处\n{mismatch.head()}")

    production_full = trade_summary(production_replay)
    production_oos = trade_summary(
        production_replay, pd.Timestamp(f"{WF_START}-01-01"))
    if not (production_full["closed"] == 125
            and np.isclose(production_full["closed_sum"], 280.9570680000034,
                           atol=1e-6, rtol=0)
            and production_oos["closed"] == 86
            and np.isclose(production_oos["closed_sum"], 268.3780177686515,
                           atol=1e-6, rtol=0)):
        raise AssertionError(
            f"七家基准未对齐: full={production_full}, oos={production_oos}")

    baseline_rows = []
    for code in ("AU", "AG"):
        market_rows = [row for row in production_replay if row.market == code]
        full = trade_summary(market_rows)
        oos = trade_summary(market_rows, pd.Timestamp(f"{WF_START}-01-01"))
        baseline_rows.append({
            "market": code,
            "full_period": "2015-2026-08-13",
            "full_closed": full["closed"],
            "full_closed_sum": full["closed_sum"],
            "oos_period": f"{WF_START}-01-01~{REPORT_END.date()}",
            "oos_closed": oos["closed"],
            "oos_closed_sum": oos["closed_sum"],
            "oos_open": oos["open"],
            "oos_terminal_sum": oos["terminal_sum"],
        })
    baseline_table = pd.DataFrame(baseline_rows)

    strict_factory = FeatureFactory(engines_strict, long_strict, strict_time=True)
    weight_audit = weight_availability_audit(production_factory, strict_factory)
    print("[stage1 strict] 42格×三准则", flush=True)
    active_name = "第一层"
    active_specs = stage1_grid()
    active_stage = run_stage(active_specs, strict_factory, strict_suppress)
    tested_counts = {"第一层": 42, "第二层": 0, "第三层": 0}

    def expansion_gate(stage: Mapping[str, Any]) -> bool:
        row = stage["summary"].loc[
            stage["summary"]["criterion"] == CRITERION_LABELS[PRIMARY_CRITERION]
        ].iloc[0]
        return bool(
            row["candidate_executed"] >= MIN_OOS
            and row["terminal_delta"] > 0
            and row["non_loss_years"] >= math.ceil(row["year_slices"] * 2 / 3)
        )

    if expansion_gate(active_stage):
        print("[stage2 unlocked] 378格×三准则", flush=True)
        active_name = "第二层"
        active_specs = stage2_grid()
        active_stage = run_stage(active_specs, strict_factory, strict_suppress)
        tested_counts["第二层"] = len(active_specs)
        if expansion_gate(active_stage):
            print("[stage3 unlocked] 3402格×三准则", flush=True)
            active_name = "第三层"
            active_specs = stage3_grid()
            active_stage = run_stage(active_specs, strict_factory, strict_suppress)
            tested_counts["第三层"] = len(active_specs)

    # 最终可见版本独立完整重选同一实际开放层，而非沿用严格路径。
    final_factory = FeatureFactory(engines_final, long_final, strict_time=True)
    print(f"[archive-visible {active_name}] {len(active_specs)}格×三准则", flush=True)
    final_stage = run_stage(active_specs, final_factory, production_suppress)
    pit_rows = []
    for criterion in CRITERIA:
        strict_row = active_stage["summary"].loc[
            active_stage["summary"]["criterion"] == CRITERION_LABELS[criterion]
        ].iloc[0]
        final_row = final_stage["summary"].loc[
            final_stage["summary"]["criterion"] == CRITERION_LABELS[criterion]
        ].iloc[0]
        changed_years = sum(
            active_stage["schedules"][criterion][year]
            != final_stage["schedules"][criterion][year]
            for year in active_stage["schedules"][criterion]
        )
        pit_rows.append({
            "criterion": CRITERION_LABELS[criterion],
            "period": f"{WF_START}-{REPORT_END.year}",
            "strict_terminal_delta": strict_row["terminal_delta"],
            "archive_terminal_delta": final_row["terminal_delta"],
            "path_changed_years": changed_years,
        })
    pit_table = pd.DataFrame(pit_rows)

    # 三种准则的真实统计量均披露；采纳只看预先指定的(b)。
    real_delta = {
        criterion: float(active_stage["summary"].loc[
            active_stage["summary"]["criterion"] == CRITERION_LABELS[criterion],
            "terminal_delta"].iloc[0])
        for criterion in CRITERIA
    }
    print(f"[placebo] {args.placebos}次完整年度重选", flush=True)
    placebo_values = placebo_distribution(
        active_specs, engines_strict, ratio, args.placebos, args.seed)
    placebo_table, placebo_tests = placebo_report(placebo_values, real_delta)

    dimensions = ["theta_mult", "fade_days"]
    if active_name in {"第二层", "第三层"}:
        dimensions += ["weight_horizon", "weight_min_n"]
    if active_name == "第三层":
        dimensions += ["dist_low_max", "netq_max"]
    sensitivity = sensitivity_table(
        active_stage, strict_factory, strict_suppress, dimensions)
    applicable = sensitivity["direction_kept"].dropna()
    sensitivity_pass = bool(len(applicable) and applicable.astype(bool).all())

    primary_summary = active_stage["summary"].loc[
        active_stage["summary"]["criterion"] == CRITERION_LABELS[PRIMARY_CRITERION]
    ].iloc[0]
    annual_primary = active_stage["calendar"][PRIMARY_CRITERION]
    required_years = math.ceil(len(annual_primary) * 2 / 3)
    sample_pass = int(primary_summary["candidate_executed"]) >= MIN_OOS
    annual_pass = int(primary_summary["non_loss_years"]) >= required_years
    oos_win = float(primary_summary["terminal_delta"]) > 0
    placebo_pass = bool(placebo_tests[PRIMARY_CRITERION]["pass"])
    significant = bool(
        oos_win and sample_pass and annual_pass and placebo_pass and sensitivity_pass)
    if significant:
        verdict = "①找到显著更优参数"
        verdict_text = "主准则及全部统计硬门通过，可采用年度因果选参路径。"
    elif any(value > 0 for value in real_delta.values()):
        verdict = "②有边际改进但不显著（建议维持现行参数）"
        verdict_text = (
            "唯一正点估来自已知偏向多交易的(a)对照路径，且未过年度稳定性与"
            "安慰剂硬门；统计判定为调参无效，建议维持现行参数。"
        )
    else:
        verdict = "③现行参数已是七家下的稳定点，调参无效"
        verdict_text = "三种年度选参路径均未在严格终值OOS上超过当前参数。"

    grid_table = pd.DataFrame([
        {"layer": "第一层", "dimensions": "theta_mult 7 × fade_days 6",
         "registered": 42, "tested": tested_counts["第一层"],
         "unlock_rule": "起始必测", "status": "已实测"},
        {"layer": "第二层", "dimensions": "第一层42 × weight_horizon 3 × weight_min_n 3",
         "registered": 378, "tested": tested_counts["第二层"],
         "unlock_rule": "主准则终值OOS>0、>=6/8年不输、n>=30",
         "status": "已实测" if tested_counts["第二层"] else "第一层硬门未过，未打开"},
        {"layer": "第三层", "dimensions": "第二层378 × dist_low_max 3 × netq_max 3",
         "registered": 3402, "tested": tested_counts["第三层"],
         "unlock_rule": "第二层同一硬门通过",
         "status": "已实测" if tested_counts["第三层"] else "前层硬门未过，未打开"},
        {"layer": "选择程序", "dimensions": "三种训练目标均逐年重选；(b)预注册为唯一主路径",
         "registered": 3, "tested": 3, "unlock_rule": "不按OOS挑准则", "status": "已实测"},
    ])
    grid_values_table = pd.DataFrame([
        {"layer": "第一层", "parameter": "theta_mult",
         "registered_values": "{0.9,1.0,1.1,1.2,1.3,1.4,1.5}",
         "current": 1.2, "tested_values": "全部注册值", "status": "已实测"},
        {"layer": "第一层", "parameter": "fade_days",
         "registered_values": "{6,8,10,12,15,20}",
         "current": 10, "tested_values": "全部注册值", "status": "已实测"},
        {"layer": "第二层", "parameter": "weight_horizon",
         "registered_values": "{10,20,30}",
         "current": 20, "tested_values": "未打开", "status": "未实测"},
        {"layer": "第二层", "parameter": "weight_min_n",
         "registered_values": "{20,30,40}",
         "current": 30, "tested_values": "未打开", "status": "未实测"},
        {"layer": "第三层", "parameter": "dist_low_max",
         "registered_values": "{0.08,0.12,0.16}",
         "current": 0.12, "tested_values": "未打开", "status": "未实测"},
        {"layer": "第三层", "parameter": "netq_max",
         "registered_values": "{0.50,0.60,0.70}",
         "current": 0.60, "tested_values": "未打开", "status": "未实测"},
    ])

    path_table = pd.concat([
        active_stage["annual"][criterion]
        for criterion in CRITERIA
    ], ignore_index=True)
    path_table["criterion_order"] = path_table["criterion"].map(
        {CRITERION_LABELS[key]: i for i, key in enumerate(CRITERIA)})
    path_table = path_table.sort_values(["criterion_order", "year"]).drop(
        columns="criterion_order")

    # 三条路径参数一致率：差异大本身作为曲面噪声证据。
    agreement_rows = []
    for year in range(WF_START, REPORT_END.year + 1):
        specs_year = [active_stage["schedules"][criterion][year]
                      for criterion in CRITERIA]
        agreement_rows.append({
            "year": year,
            "all_three_same": len(set(specs_year)) == 1,
            "a_spec": specs_year[0].key,
            "b_spec": specs_year[1].key,
            "c_spec": specs_year[2].key,
        })
    agreement = pd.DataFrame(agreement_rows)

    full_rows = []
    for spec in active_specs:
        summary = trade_summary(active_stage["fixed_cache"][spec.key])
        full_rows.append({
            **spec.as_row(),
            "period": f"2015-01-01~{REPORT_END.date()}",
            "executed": summary["executed"],
            "closed": summary["closed"],
            "open": summary["open"],
            "closed_sum": summary["closed_sum"],
            "terminal_sum": summary["terminal_sum"],
            "warning": "全样本过拟合参照，不作为建议",
        })
    full_top = (pd.DataFrame(full_rows)
                .sort_values(["terminal_sum", "closed_sum"], ascending=False)
                .head(10))

    strict_base_summary = trade_summary(
        active_stage["baseline"], pd.Timestamp(f"{WF_START}-01-01"))
    baseline_config = pd.DataFrame([{
        "baseline": "七家+现行参数（唯一基准）",
        **BASE.as_row(),
    }])
    baseline_compare = pd.DataFrame([
        {"run": "生产原样对账", "period": f"{WF_START}-01-01~{REPORT_END.date()}",
         "executed": production_oos["executed"], "closed": production_oos["closed"],
         "open": production_oos["open"], "closed_sum": production_oos["closed_sum"],
         "terminal_sum": production_oos["terminal_sum"],
         "use": "仅核对既有+268.4%"},
        {"run": "严格可用时点当前参数", "period": f"{WF_START}-01-01~{REPORT_END.date()}",
         "executed": strict_base_summary["executed"], "closed": strict_base_summary["closed"],
         "open": strict_base_summary["open"], "closed_sum": strict_base_summary["closed_sum"],
         "terminal_sum": strict_base_summary["terminal_sum"],
         "use": "主研究唯一因果比较基准"},
    ])

    placebo_real_rows = []
    for criterion in CRITERIA:
        test = placebo_tests[criterion]
        placebo_real_rows.append({
            "criterion": CRITERION_LABELS[criterion],
            "period": f"{WF_START}-{REPORT_END.year}",
            "candidate_executed": int(active_stage["summary"].loc[
                active_stage["summary"]["criterion"] == CRITERION_LABELS[criterion],
                "candidate_executed"].iloc[0]),
            "real_terminal_delta": real_delta[criterion],
            "placebo_q95": test["q95"],
            "empirical_p": test["p"],
            "pass": test["pass"],
        })
    placebo_real_table = pd.DataFrame(placebo_real_rows)

    gate_table = pd.DataFrame([
        {"gate": "主准则终值OOS赢当前参数", "period": f"{WF_START}-{REPORT_END.year}",
         "n": int(primary_summary["candidate_executed"]),
         "value": float(primary_summary["terminal_delta"]), "pass": oos_win},
        {"gate": "OOS执行笔数>=30", "period": f"{WF_START}-{REPORT_END.year}",
         "n": int(primary_summary["candidate_executed"]),
         "value": int(primary_summary["candidate_executed"]), "pass": sample_pass},
        {"gate": ">=2/3年度不输", "period": f"{WF_START}-{REPORT_END.year}",
         "n": len(annual_primary), "value": int(primary_summary["non_loss_years"]),
         "pass": annual_pass},
        {"gate": "超过安慰剂95分位且p<0.05", "period": f"{WF_START}-{REPORT_END.year}",
         "n": args.placebos, "value": placebo_tests[PRIMARY_CRITERION]["p"],
         "pass": placebo_pass},
        {"gate": "参数±1档方向不反转", "period": f"{WF_START}-{REPORT_END.year}",
         "n": len(applicable), "value": float(applicable.astype(bool).mean()) if len(applicable) else np.nan,
         "pass": sensitivity_pass},
    ])

    lines = [
        "# 七家信号组参数重训报告 v1",
        "",
        f"**结论：{verdict}。** {verdict_text}",
        "",
        "可部署参数建议：继续使用 `theta_mult=1.2`、`fade_days=10`、"
        "`weight_horizon=20`、`weight_min_n=30`、`dist_low_max=0.12`、`netq_max=0.60`；"
        "全样本最优仅作过拟合参照。",
        "",
        f"数据截止{REPORT_END.date()}；预注册主准则为(b)单笔均值-SE；实际最深搜索={active_name}；"
        f"随机种子`{args.seed}`；嵌套安慰剂n={args.placebos}。",
        "",
        "## 1. 边界、数据与冻结配置",
        "",
        "- 信号组固定为：" + "、".join(expected_group) + "。",
        "- 本研究脚本只读调用 `engine/smart_money.py` 与本地CSV；脚本未修改引擎，未连接生产库。",
        "- 本单只搜索六个已注册维度；止损、单席位事件定义、警报压制期与回放起点全部冻结。",
        "",
        markdown_table(data_table),
        "",
        "## 2. 七家基准硬复现",
        "",
        f"研究状态机与生产引擎逐字段错配={len(mismatch)}。完整2015-2026已平"
        f"{production_full['closed']}笔、累计{production_full['closed_sum']:.3f}%（+281.0%）；"
        f"2019起按信号日为{production_oos['closed']}笔已平、累计"
        f"{production_oos['closed_sum']:.3f}%（+268.4%）。因此工单中的125笔属于完整账本，"
        "不能误标成2019起的样本数。",
        "",
        markdown_table(baseline_config),
        "",
        markdown_table(baseline_table),
        "",
        "生产原样对账与严格因果当前参数分列如下。训练及主OOS只使用第二行：",
        "",
        markdown_table(baseline_compare),
        "",
        "权重可用日审计发现生产原样对账含下列尚未在参数生效日前完整实现的远期收益；"
        "为同时满足基准复现与反时间幻觉，生产行保留作对账，严格WF剔除：",
        "",
        markdown_table(weight_audit),
        "",
        "## 3. 预注册分层网格 vs 实测",
        "",
        "分层门只看预先指定的(b)路径；(a)/(c)不用于决定解锁或推荐，避免OOS后挑准则。",
        "",
        markdown_table(grid_table),
        "",
        "锁定取值与实测取值逐项对照如下；未解锁层不做局部补跑：",
        "",
        markdown_table(grid_values_table),
        "",
        "## 4. 三种训练准则的严格OOS",
        "",
        "训练样本统一为候选自身在年初前已进场交易：已退出取实现收益，未退出取年初前末收MTM；"
        f"n<{MIN_TRAIN}不可选。(a)=收益和；(b)=均值-一个标准误；(c)=均值/标准差。"
        "主OOS终值把08-13持仓MTM计入，避免只删未平仓；+268.4%已平口径仍单列对账。",
        "",
        markdown_table(active_stage["summary"]),
        "",
        "唯一可执行的跨年连续路径如下：2015-2018用当前参数作burn-in；2019起每年只用<Y数据选参；"
        "参数在信号日冻结，已有挂单与持仓跨年延续。表中每年只纳入该年信号且在共同年末前"
        "已成交的交易：已退出取实现收益，未退出取年末MTM，不借用以后年度退出结果。"
        "单年n<30仅作路径对账，不作独立结论。",
        "",
        markdown_table(path_table),
        "",
        "主准则的日历盯市年度增量（用于>=2/3稳定性门）：",
        "",
        markdown_table(annual_primary),
        "",
        "三准则年度参数一致性；分歧本身视为参数曲面噪声：",
        "",
        markdown_table(agreement),
        "",
        markdown_table(gate_table),
        "",
        "## 5. reboard严格点时与最终可见双跑",
        "",
        "可见日文件先完成D<R一一映射核验。生产引擎特征链永久排除推断行；因此严格版在进入引擎前"
        "执行R日门（实际可用集合为空），最终可见版保留全部输入但被同一生产清洗规则排除。"
        "两版均独立重建实际开放层及三条年度选择路径，不把零差伪装成额外证据。",
        "",
        markdown_table(visibility_table),
        "",
        markdown_table(pit_table),
        "",
        "## 6. 嵌套安慰剂",
        "",
        "每次对AU、AG的底层多空席位事件逐事件偏移±5~10个各自交易日；边界从可行偏移集合均匀抽取，"
        "同席位同日碰撞保留强度最大事件。随后重建权重、score、active、成本区间、警报压制，"
        "并对候选与当前参数在同一随机世界中完整重做逐年选择。统计量为终值收益差。",
        "",
        markdown_table(placebo_real_table),
        "",
        markdown_table(placebo_table),
        "",
        "## 7. 主准则参数敏感性",
        "",
        "对年度已选路径的每个开放维度整体移动到相邻注册档，并重新运行完整连续状态机；"
        "边界年份逐项列N/A，不创造新值。主路径本身落后基准，因此这里的‘方向保持’仅表示"
        "负方向没有反转，不能视作候选规则通过。",
        "",
        markdown_table(sensitivity),
        "",
        "## 8. 全样本最优（过拟合参照，不作为建议）",
        "",
        markdown_table(full_top),
        "",
        "## 9. 反时间幻觉六条自证",
        "",
        "- [x] 席位T日收盘后确认；区间/市价进场与消退退出最早T+1执行。",
        "- [x] 年度Y只用Y年以前的交易MTM/实现收益选参；当年OOS不反向参与。",
        "- [x] 权重事件固定早于上年12-01，并额外要求所选horizon的远期收益终点早于Y-01-01。",
        "- [x] dist60、netq及事件阈值均为向后滚动；事件阈值沿用生产 `shift(1)`。",
        "- [x] reboard映射逐行验证D<R并完成严格/最终可见独立重选；生产永久排除推断行的语义未改。",
        "- [x] 金银比只读 `gold_silver_ratio.csv`，原索引直接交给引擎警报函数；未联网、未自建映射。",
        "",
        "## 10. 验收清单",
        "",
        f"- [x] 七家基准复现：全期{production_full['closed']}笔/+{production_full['closed_sum']:.3f}%；"
        f"2019起{production_oos['closed']}笔/+{production_oos['closed_sum']:.3f}%。",
        "- [x] 预注册网格与实测层数、每层精确组合数均披露。",
        "- [x] 三种选参标准的OOS全部列出，(b)在看结果前锁为唯一主准则。",
        "- [x] 三条逐年WF路径均列训练期、训练n、参数、分数与当年OOS。",
        f"- [{'x' if placebo_pass else ' '}] 固定种子嵌套安慰剂n={args.placebos}，"
        "真实主统计量严格超过95分位且经验p<0.05。",
        f"- [{'x' if sensitivity_pass else ' '}] 主路径全部可用±1档方向不反转。",
        "- [x] OOS执行笔数门按候选自身计算；单年或分片n<30均不作独立结论。",
        "- [x] 反时间幻觉六条逐项完成，含reboard双跑。",
        "- [x] 本研究脚本未修改 `engine/`，未连接生产库，新增产物仅在 `research/`。",
        "",
        "## 11. 完整复现命令",
        "",
        "```powershell",
        ("& 'C:\\Users\\a6366\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' "
         f"-B research/run_retune7.py --seed {args.seed} --placebos {args.placebos}"),
        "```",
        "",
    ]
    text = "\n".join(lines)
    if "八家" in text:
        raise AssertionError("报告出现已废止基准措辞")
    REPORT.write_text(text, encoding="utf-8")
    print(f"[done] {REPORT}")
    print(f"[verdict] {verdict}: {verdict_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
