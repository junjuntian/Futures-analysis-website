# -*- coding: utf-8 -*-
"""七席位年度权重规格与一条入场确认规则：探索、封存、一次确认。

研究上界被故意写死为 2022-12-31，交易重放从 2015-01-01 开始：

- CSV 必须按日期单调；读取器在首次遇到 2023-01-01 后立即停止迭代，后续
  CSV 记录不再消费。文件起点至 2014 年末只作为 2015 信号所需的历史 burn-in。
- 2015-2018 是初始训练窗与状态热身：不进入 2019-2022 OOS 汇总，但
  作为 <Y 历史参与后续逐年选参；训练只使用决策年 Y 前已可得的结果/盯市值。
- ``explore`` 子进程只允许2022前缀；独占写PREREG后退出。``confirm``
  先验证脚本/封存hash并独占创建REPORT锁，之后才允许读取2023+，且只跑一次。
- 生产引擎只读。先用生产口径 W1/min30/clip5 对拍，再用严格可用时点口径
  搜索阶段一 A；除权重统计量、min_n 和 clip 外，现行参数全部冻结。

唯一研究产物是本脚本、PREREG与最终REPORT；不写中间结果文件。
"""
from __future__ import annotations

import argparse
import copy
import csv
import gzip
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import datetime
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESEARCH = Path(__file__).resolve().parent
DEFAULT_DATA = RESEARCH / "data"
PREREG_PATH = RESEARCH / "PREREG_WEIGHTSPEC.md"
REPORT_PATH = RESEARCH / "REPORT_WEIGHTSPEC_v1.md"
DEFAULT_SEED = 20260814
DEFAULT_PLACEBOS = 100
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import smart_money as sm  # noqa: E402


WINDOW_START = pd.Timestamp("2015-01-01")
WINDOW_END = pd.Timestamp("2022-12-31")
WINDOW_END_EXCLUSIVE = WINDOW_END + pd.Timedelta(days=1)
CONFIRM_END = pd.Timestamp("2026-08-13")
CONFIRM_END_EXCLUSIVE = CONFIRM_END + pd.Timedelta(days=1)
WF_START = 2019
WF_END = 2022
MIN_TRAIN = 30
WEIGHT_HORIZON = 20

MIN_N_GRID = (10, 15, 20, 25, 30, 40, 50)
CLIP_GRID = (3.0, 5.0, 8.0, 12.0, 999.0)
SHRINK_K_GRID = (10, 30, 60)
CAPPED_T_GRID = (30, 60, 100)

CRITERIA = ("sum", "mean_se", "risk_adjusted")
CRITERION_LABELS = {
    "sum": "(a)训练累计收益",
    "mean_se": "(b)单笔均值-SE（主准则）",
    "risk_adjusted": "(c)均值/标准差",
}
PRIMARY_CRITERION = "mean_se"


def configure_runtime_end(end: pd.Timestamp) -> None:
    """在任何输入读取前锁定本进程可见的研究上界。"""
    global WINDOW_END, WINDOW_END_EXCLUSIVE
    end = pd.Timestamp(end).normalize()
    if end not in {pd.Timestamp("2022-12-31"), CONFIRM_END}:
        raise ValueError(f"未注册的运行上界: {end.date()}")
    WINDOW_END = end
    WINDOW_END_EXCLUSIVE = end + pd.Timedelta(days=1)

FAMILY_LABELS = {
    "w1_t": "W1 t",
    "w2_mean": "W2 mean",
    "w3_mean_log": "W3 mean*log(1+n)",
    "w4_shrink": "W4 shrink",
    "w5_capped_t": "W5 capped-t",
}

# 用断言而不是静默跟随 engine 的未来改动，保证“其余现行参数冻结”。
FROZEN_RULES: dict[str, Any] = {
    "group": (
        "中财期货", "中信期货", "海通期货", "国泰君安",
        "高盛期货", "东证期货", "华泰期货",
    ),
    "cond_seats": ("国泰君安", "东证期货"),
    "event_q": 0.80,
    "event_window": 250,
    "event_min_hist": 120,
    "score_window": 5,
    "strength_cap": 3.0,
    "weight_horizon": 20,
    "weight_min_n": 30,
    "weight_clip": 5.0,
    "theta_mult": 1.2,
    "dist_low_days": 60,
    "dist_low_max": 0.12,
    "netq_window": 250,
    "netq_max": 0.60,
    "zone_half_width": 5.0,
    "zone_valid_days": 10,
    "stop_loss": 0.04,
    "fade_days": 10,
    "flee_suppress_days": 40,
    "replay_start": "2015-01-01",
    "ratio_extreme_low": 48.0,
}


def assert_frozen_rules() -> None:
    """拒绝在未重新审阅时把 engine 参数变化混入本次搜索。"""
    mismatches: list[str] = []
    for key, expected in FROZEN_RULES.items():
        actual = sm.RULES.get(key)
        if isinstance(expected, tuple):
            same = tuple(actual) == expected if actual is not None else False
        elif isinstance(expected, float):
            same = isinstance(actual, (int, float)) and math.isclose(
                float(actual), expected, rel_tol=0.0, abs_tol=1e-12,
            )
        else:
            same = actual == expected
        if not same:
            mismatches.append(f"{key}: expected={expected!r}, actual={actual!r}")
    if mismatches:
        raise RuntimeError("现行参数已偏离阶段一 A 冻结快照:\n" + "\n".join(mismatches))


@dataclass(frozen=True)
class WeightSpec:
    """一个注册格；W4 的 ``min_n`` 是保留标签，不参与公式。"""

    family: str
    min_n: int
    clip: float
    parameter: int | None = None

    @property
    def key(self) -> str:
        suffix = ""
        if self.family == "w4_shrink":
            suffix = f",k={self.parameter}"
        elif self.family == "w5_capped_t":
            suffix = f",cap_n={self.parameter}"
        return (
            f"{self.family},min_n={self.min_n},clip={self.clip:g}{suffix}"
        )

    @property
    def effective_key(self) -> str:
        """实际计算键；W4 不使用 hard min_n，故排除该标签。"""
        if self.family == "w4_shrink":
            return f"{self.family},clip={self.clip:g},k={self.parameter}"
        return self.key

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "label": FAMILY_LABELS[self.family],
            "min_n": self.min_n,
            "clip": self.clip,
            "shrink_k": self.parameter if self.family == "w4_shrink" else None,
            "cap_n": self.parameter if self.family == "w5_capped_t" else None,
            "min_n_is_label_only": self.family == "w4_shrink",
            "key": self.key,
            "effective_key": self.effective_key,
        }


BASE = WeightSpec("w1_t", min_n=30, clip=5.0)


@dataclass(frozen=True)
class BSpec:
    """独立席位确认门；NONE 是不增加任何门槛的现行基准。"""

    key: str
    min_members: int | None
    lookback_days: int | None

    @property
    def enabled(self) -> bool:
        return self.min_members is not None and self.lookback_days is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "min_members": self.min_members,
            "lookback_trading_days": self.lookback_days,
            "enabled": self.enabled,
        }


NONE = BSpec("NONE", None, None)
B1 = BSpec("B1", 2, 5)
B2 = BSpec("B2", 2, 3)
B3 = BSpec("B3", 3, 5)
B4 = BSpec("B4", 3, 3)
B5 = BSpec("B5", 2, 8)
B6 = BSpec("B6", 3, 8)
B_SPECS: tuple[BSpec, ...] = (NONE, B1, B2, B3, B4, B5, B6)
B_LOOKBACKS: tuple[int, ...] = tuple(sorted({
    int(spec.lookback_days) for spec in B_SPECS if spec.enabled
}))


def stage1_a_grid() -> list[WeightSpec]:
    """返回锁定的 315 个注册格（其中 225 个有效唯一格）。"""
    specs: list[WeightSpec] = []
    for min_n, clip in product(MIN_N_GRID, CLIP_GRID):
        specs.extend([
            WeightSpec("w1_t", min_n, clip),
            WeightSpec("w2_mean", min_n, clip),
            WeightSpec("w3_mean_log", min_n, clip),
        ])
        specs.extend(
            WeightSpec("w4_shrink", min_n, clip, k)
            for k in SHRINK_K_GRID
        )
        specs.extend(
            WeightSpec("w5_capped_t", min_n, clip, cap_n)
            for cap_n in CAPPED_T_GRID
        )
    effective = {spec.effective_key for spec in specs}
    assert len(specs) == 315
    assert len({spec.key for spec in specs}) == 315
    assert len(effective) == 225
    assert BASE in specs
    return specs


def grid_metadata(specs: Sequence[WeightSpec]) -> dict[str, Any]:
    return {
        "registered_count": len(specs),
        "effective_unique_count": len({spec.effective_key for spec in specs}),
        "structural_duplicate_count": (
            len(specs) - len({spec.effective_key for spec in specs})
        ),
        "axes": {
            "min_n": list(MIN_N_GRID),
            "clip": list(CLIP_GRID),
            "clip_999_semantics": "仅保留下限0，不设上限",
            "statistics": {
                "W1": "t = mean/std*sqrt(n)",
                "W2": "mean（百分比点）",
                "W3": "mean（百分比点）*log(1+n)",
                "W4": {
                    "formula": (
                        "n/(n+k)*member_mean + k/(n+k)*group_mean；"
                        "member/group mean 均为百分比点；n=0时回退group_mean"
                    ),
                    "k": list(SHRINK_K_GRID),
                    "hard_min_n": False,
                    "registered_min_n_labels_retained": True,
                },
                "W5": "mean/std*sqrt(min(n,cap_n))",
                "W5_cap_n": list(CAPPED_T_GRID),
            },
        },
        "baseline": BASE.as_dict(),
    }


def _pick_csv(data_dir: Path, stem: str) -> Path:
    for extension in (".csv.gz", ".csv"):
        candidate = data_dir / f"{stem}{extension}"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"缺少 {data_dir}/{stem}.csv[.gz]")


def _assert_frame_window(frame: pd.DataFrame, date_col: str, label: str) -> None:
    if frame.empty:
        raise ValueError(f"{label} 在锁定窗口内为空")
    dates = pd.DatetimeIndex(frame[date_col])
    if dates.isna().any():
        raise ValueError(f"{label}.{date_col} 含无效日期")
    outside = dates >= WINDOW_END_EXCLUSIVE
    if bool(outside.any()):
        raise AssertionError(f"{label} 出现研究窗口外日期")


def read_windowed_csv(
    path: Path,
    date_col: str,
    *,
    chunk_size: int,
    row_filter: Callable[[Mapping[str, str]], bool] | None = None,
) -> pd.DataFrame:
    """验证边界前日期单调；信任源文件全局升序并在 2023 边界停止。"""
    kept: list[pd.DataFrame] = []
    previous_date: pd.Timestamp | None = None
    rows: list[dict[str, str]] = []
    row_dates: list[pd.Timestamp] = []

    def flush() -> None:
        nonlocal rows, row_dates
        if not rows:
            return
        dates = pd.DatetimeIndex(row_dates)
        if not dates.is_monotonic_increasing:
            raise ValueError(f"{path.name}.{date_col} 分块内不是日期升序")
        frame = pd.DataFrame.from_records(rows)
        frame[date_col] = dates.to_numpy()
        kept.append(frame)
        rows, row_dates = [], []

    # csv.DictReader 每次只请求一条记录；遇到第一条边界记录便 break，不会再
    # 请求后面的 CSV 记录。只有边界前的文本行才进入 DataFrame/类型推断。
    opener = gzip.open if path.suffix == ".gz" else Path.open
    if path.suffix == ".gz":
        stream_context = opener(path, mode="rt", encoding="utf-8-sig", newline="")
    else:
        stream_context = opener(path, mode="r", encoding="utf-8-sig", newline="")
    with stream_context as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or date_col not in reader.fieldnames:
            raise ValueError(f"{path.name} 缺少日期列 {date_col}")
        for row in reader:
            day = pd.Timestamp(row[date_col])
            if pd.isna(day):
                raise ValueError(f"{path.name}.{date_col} 含无效日期")
            if previous_date is not None and day < previous_date:
                raise ValueError(f"{path.name}.{date_col} 不是日期升序")
            if day >= WINDOW_END_EXCLUSIVE:
                break
            if row_filter is not None and not row_filter(row):
                previous_date = day
                continue
            rows.append(row)
            row_dates.append(day)
            previous_date = day
            if len(rows) >= chunk_size:
                flush()
        flush()
    if not kept:
        raise ValueError(f"{path.name} 在 2023 上界前无数据")
    frame = pd.concat(kept, ignore_index=True)
    _assert_frame_window(frame, date_col, path.name)
    return frame


def _coerce_bounded_inputs(
    price: pd.DataFrame, seat: pd.DataFrame, code: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    price = price.copy()
    seat = seat.copy()
    for column in (
        "open_price", "high_price", "low_price", "close_price",
        "settlement_price", "volume", "open_interest",
    ):
        if column in price:
            price[column] = pd.to_numeric(price[column], errors="coerce")
    for column in ("quantity", "change"):
        if column in seat:
            seat[column] = pd.to_numeric(seat[column], errors="coerce")
    _assert_frame_window(price, "trade_date", f"{code}.price")
    _assert_frame_window(seat, "trade_date", f"{code}.seat")
    return price, seat


def _assert_required_history(
    frame: pd.DataFrame, date_col: str, label: str,
) -> None:
    """阶段一必须有 pre-2015 burn-in、2015 起点和 2022 年末数据。"""
    dates = pd.DatetimeIndex(frame[date_col])
    if dates.min() >= WINDOW_START:
        raise ValueError(f"{label} 缺少 pre-2015 burn-in")
    if not bool((dates.year == WINDOW_START.year).any()):
        raise ValueError(f"{label} 缺少 2015 数据")
    if dates.max() < pd.Timestamp("2022-12-01"):
        raise ValueError(f"{label} 未覆盖 2022 年末")


@dataclass
class BoundedInputs:
    raw: dict[str, tuple[pd.DataFrame, pd.DataFrame]]
    ratio: pd.Series
    visibility: pd.DataFrame
    manifest: dict[str, Any]


def _manifest_row(path: Path, frame: pd.DataFrame, date_col: str) -> dict[str, Any]:
    """只对已经截断的 frame 统计；不读取/哈希原文件的窗口外内容。"""
    dates = pd.DatetimeIndex(frame[date_col])
    normalized = frame.copy()
    for column in normalized.columns:
        if pd.api.types.is_datetime64_any_dtype(normalized[column]):
            normalized[column] = normalized[column].dt.strftime(
                "%Y-%m-%dT%H:%M:%S",
            )
    row_hashes = pd.util.hash_pandas_object(
        normalized.astype("string").fillna("<NA>"), index=False,
    ).to_numpy(dtype=np.uint64)
    return {
        "file": path.name,
        "rows_in_window": int(len(frame)),
        "first_date": str(dates.min().date()),
        "last_date": str(dates.max().date()),
        "bounded_content_sha256": hashlib.sha256(
            row_hashes.tobytes(),
        ).hexdigest(),
    }


REBOARD_KEYS = (
    "instrument", "contract", "rank_type", "member", "trade_date", "source",
)


def load_bounded_inputs(data_dir: Path, chunk_size: int) -> BoundedInputs:
    """加载且硬截断四张行情/席位表、可见日映射及金银比。"""
    visibility_path = _pick_csv(data_dir, "reboard_visibility")
    def reboard_visible_before_bound(row: Mapping[str, str]) -> bool:
        # 只解析R作为输入门；R>=上界的整行不进入DataFrame/manifest/统计。
        return pd.Timestamp(row["reboard_date"]) < WINDOW_END_EXCLUSIVE

    visibility = read_windowed_csv(
        visibility_path, "trade_date", chunk_size=chunk_size,
        row_filter=reboard_visible_before_bound,
    )
    visibility["reboard_date"] = pd.to_datetime(
        visibility["reboard_date"], errors="raise",
    )
    # 仅把运行上界前已经回榜的历史推断行交给archive输入；R在封存期的行
    # 只用于立即拒绝，不进入返回DataFrame、manifest或任何研究统计。
    _assert_frame_window(
        visibility, "trade_date", "reboard_visibility",
    )
    if (visibility["reboard_date"] >= WINDOW_END_EXCLUSIVE).any():
        raise AssertionError("可见日门未在输入层生效")
    visible_keys = {
        (
            str(row.instrument), str(row.contract), str(row.rank_type),
            str(row.member), pd.Timestamp(row.trade_date).strftime("%Y-%m-%d"),
            str(row.source),
        )
        for row in visibility.itertuples(index=False)
    }

    def seat_row_visible(row: Mapping[str, str]) -> bool:
        if row.get("source") != "reboard_inferred":
            return True
        key = tuple(str(row.get(column, "")) for column in REBOARD_KEYS)
        return key in visible_keys

    raw: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    manifest: dict[str, Any] = {}
    for code in ("AU", "AG"):
        prefix = code.lower()
        price_path = _pick_csv(data_dir, f"{prefix}_price")
        seat_path = _pick_csv(data_dir, f"{prefix}_seat")
        price = read_windowed_csv(
            price_path, "trade_date", chunk_size=chunk_size,
        )
        seat = read_windowed_csv(
            seat_path, "trade_date", chunk_size=chunk_size,
            row_filter=seat_row_visible,
        )
        price, seat = _coerce_bounded_inputs(price, seat, code)
        _assert_required_history(price, "trade_date", f"{code}.price")
        _assert_required_history(seat, "trade_date", f"{code}.seat")
        raw[code] = (price, seat)
        manifest[f"{prefix}_price"] = _manifest_row(
            price_path, price, "trade_date",
        )
        manifest[f"{prefix}_seat"] = _manifest_row(
            seat_path, seat, "trade_date",
        )

    ratio_path = _pick_csv(data_dir, "gold_silver_ratio")
    ratio_frame = read_windowed_csv(
        ratio_path, "date", chunk_size=chunk_size,
    )
    ratio_frame["ratio"] = pd.to_numeric(ratio_frame["ratio"], errors="raise")
    _assert_frame_window(ratio_frame, "date", "gold_silver_ratio")
    _assert_required_history(ratio_frame, "date", "gold_silver_ratio")
    if ratio_frame["date"].duplicated().any() or ratio_frame["ratio"].isna().any():
        raise ValueError("窗口内 gold_silver_ratio 日期重复或 ratio 缺失")
    ratio = ratio_frame.set_index("date")["ratio"].sort_index()
    if (ratio.index >= WINDOW_END_EXCLUSIVE).any():
        raise AssertionError("金银比窗口截断失效")
    manifest["gold_silver_ratio"] = _manifest_row(
        ratio_path, ratio_frame, "date",
    )
    manifest["reboard_visibility"] = _manifest_row(
        visibility_path, visibility, "trade_date",
    )
    return BoundedInputs(
        raw=raw, ratio=ratio, visibility=visibility, manifest=manifest,
    )


def validate_reboard_visibility(inputs: BoundedInputs) -> dict[str, Any]:
    """验证窗口内推断行与可见日映射一一对应且 D<R。"""
    inferred = pd.concat([
        seat.loc[seat["source"].eq("reboard_inferred"), list(REBOARD_KEYS)]
        for _, seat in inputs.raw.values()
    ], ignore_index=True)
    visibility = inputs.visibility.loc[
        inputs.visibility["source"].eq("reboard_inferred"),
        [*REBOARD_KEYS, "reboard_date"],
    ].copy()
    if inferred.duplicated(list(REBOARD_KEYS)).any():
        raise AssertionError("窗口内 inferred seat key 非一一")
    if visibility.duplicated(list(REBOARD_KEYS)).any():
        raise AssertionError("窗口内 visibility key 非一一")
    merged = inferred.merge(
        visibility, on=list(REBOARD_KEYS), how="left", validate="one_to_one",
        indicator=True,
    )
    missing = int(merged["reboard_date"].isna().sum())
    bad_order = int((merged["reboard_date"] <= merged["trade_date"]).sum())
    if missing or bad_order or not merged["_merge"].eq("both").all():
        raise AssertionError(
            f"reboard映射失败: missing={missing}, D>=R={bad_order}",
        )
    return {
        "period": (
            f"{merged['trade_date'].min().date()}~"
            f"{merged['trade_date'].max().date()}"
        ) if len(merged) else "—",
        "inferred_rows": int(len(inferred)),
        "mapped_rows": int(len(merged)),
        "mapping_rate": 1.0 if len(inferred) else None,
        "d_lt_r_rows": int((merged["trade_date"] < merged["reboard_date"]).sum()),
        "visible_on_fact_date_rows": int(
            (merged["reboard_date"] <= merged["trade_date"]).sum()
        ),
        "strict_rule": "事实日D仅当R<=D可见；因此所有D<R推断行在D决策均排除",
        "engine_rule": "clean_seat永久排除source=reboard_inferred",
    }


def raw_for_visibility_mode(
    inputs: BoundedInputs, mode: str,
) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
    if mode not in {"strict_pit", "archive_visible"}:
        raise ValueError(mode)
    out: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for code, (price, seat) in inputs.raw.items():
        if mode == "strict_pit":
            # 对事实日D应用R<=D门；映射审计已证明全部推断行D<R。
            seat_view = seat.loc[
                ~seat["source"].eq("reboard_inferred")
            ].copy()
        else:
            seat_view = seat.copy()
        out[code] = (price.copy(), seat_view)
    return out


def _assert_index_window(index: Iterable[Any], label: str) -> None:
    dates = pd.DatetimeIndex(index)
    if len(dates) and (dates >= WINDOW_END_EXCLUSIVE).any():
        raise AssertionError(f"派生数据 {label} 越过锁定窗口")


def assert_engine_window(engine: sm.MarketEngine) -> None:
    """特征工厂接管前逐项审计引擎的有日期派生对象。"""
    _assert_index_window(engine.dates, f"{engine.instrument}.dates")
    _assert_index_window(engine.cont.index, f"{engine.instrument}.cont")
    _assert_index_window(engine.mc["trade_date"], f"{engine.instrument}.mc")
    for name in ("md", "ev_long", "ev_short", "ev_eff", "seat_long"):
        frame = getattr(engine, name)
        if not frame.empty:
            _assert_index_window(
                frame["trade_date"], f"{engine.instrument}.{name}",
            )
    _assert_index_window(engine.dist60.index, f"{engine.instrument}.dist60")
    _assert_index_window(engine.netq.index, f"{engine.instrument}.netq")
    for name in (
        "netsum", "wmat", "score", "theta", "active", "fade_run",
    ):
        derived = getattr(engine, name)
        _assert_index_window(derived.index, f"{engine.instrument}.{name}")
    if len(engine.long_floor):
        _assert_index_window(
            engine.long_floor.index.get_level_values("trade_date"),
            f"{engine.instrument}.long_floor",
        )
    for member, costs in engine.costs.items():
        _assert_index_window(costs.index, f"{engine.instrument}.costs.{member}")
    if any(year > WINDOW_END.year for year in engine.weights):
        raise AssertionError(f"{engine.instrument}.weights 出现窗口外年份")


def build_bounded_engines(
    raw: Mapping[str, tuple[pd.DataFrame, pd.DataFrame]],
) -> dict[str, sm.MarketEngine]:
    engines: dict[str, sm.MarketEngine] = {}
    for code in ("AU", "AG"):
        price0, seat0 = raw[code]
        # 在构造派生数据前再次执行显式边界，避免调用方绕开 loader。
        price = price0[
            price0["trade_date"] < WINDOW_END_EXCLUSIVE
        ].copy()
        seat = seat0[
            seat0["trade_date"] < WINDOW_END_EXCLUSIVE
        ].copy()
        _assert_frame_window(price, "trade_date", f"{code}.price.pre_engine")
        _assert_frame_window(seat, "trade_date", f"{code}.seat.pre_engine")
        engine = sm.MarketEngine(code, price, seat)
        assert_engine_window(engine)
        if engine.dates[0] >= WINDOW_START or engine.dates[-1].year != WINDOW_END.year:
            raise AssertionError(f"{code} 引擎历史不足以覆盖 burn-in~2022")
        engines[code] = engine
    return engines


def visibility_engine_equivalence(
    strict: Mapping[str, sm.MarketEngine],
    archive: Mapping[str, sm.MarketEngine],
) -> dict[str, Any]:
    """证明生产永久禁入规则使两种输入臂的研究特征完全相同。"""
    mismatches: list[str] = []
    checked = 0
    for code in ("AU", "AG"):
        left, right = strict[code], archive[code]
        for name in ("md", "ev_long", "ev_short", "ev_eff", "seat_long"):
            checked += 1
            if not getattr(left, name).equals(getattr(right, name)):
                mismatches.append(f"{code}.{name}")
        for name in (
            "dist60", "netq", "netsum", "wmat", "score", "theta",
            "active", "fade_run", "long_floor",
        ):
            checked += 1
            if not getattr(left, name).equals(getattr(right, name)):
                mismatches.append(f"{code}.{name}")
        checked += 1
        if left.weights != right.weights:
            mismatches.append(f"{code}.weights")
    if mismatches:
        raise AssertionError("reboard双臂派生对象不一致: " + ",".join(mismatches))
    return {
        "period": f"文件起点~{WINDOW_END.date()}",
        "strict_input": "推断行按D日R<=D门预排除",
        "archive_input": "保留全部窗口内推断行后交给引擎",
        "derived_objects_checked": checked,
        "mismatch_count": 0,
        "interpretation": (
            "engine.clean_seat永久排除推断行，故R门在当前生产特征链前"
            "结构性无可观察影响；未绕过引擎制造差异"
        ),
    }


@dataclass
class WeightLedger:
    """与规格无关的 fwd20（原值/百分比点）及其严格可得日。"""

    returns_decimal: pd.Series
    returns_pp: pd.Series
    available_date: pd.Series


def build_weight_ledger(engine: sm.MarketEngine) -> WeightLedger:
    assert_engine_window(engine)
    returns_decimal = sm.forward_returns(engine.cont, WEIGHT_HORIZON)
    returns_pp = returns_decimal * 100.0
    _assert_index_window(returns_pp.index, f"{engine.instrument}.fwd20")
    dates = engine.dates
    available = np.full(len(dates), np.datetime64("NaT"), dtype="datetime64[ns]")
    for index in range(len(dates)):
        end_index = index + WEIGHT_HORIZON + 1
        if end_index < len(dates) and pd.notna(returns_pp.iloc[index]):
            available[index] = dates[end_index].to_datetime64()
    available_date = pd.Series(available, index=dates)
    _assert_index_window(
        available_date.dropna().to_numpy(), f"{engine.instrument}.available_date",
    )
    return WeightLedger(
        returns_decimal=returns_decimal,
        returns_pp=returns_pp,
        available_date=available_date,
    )


def _member_samples(
    engine: sm.MarketEngine,
    events: pd.DataFrame,
    ledger: WeightLedger,
    year: int,
    strict_availability: bool,
    *,
    percentage_points: bool,
) -> dict[str, np.ndarray]:
    event_cutoff = pd.Timestamp(f"{year - 1}-12-01")
    decision = pd.Timestamp(f"{year}-01-01")
    out: dict[str, np.ndarray] = {}
    for member in engine.group:
        event_dates = pd.DatetimeIndex(events.loc[
            (events["member"] == member)
            & (events["trade_date"] < event_cutoff),
            "trade_date",
        ])
        returns = ledger.returns_pp if percentage_points else ledger.returns_decimal
        values = returns.reindex(event_dates)
        valid = values.notna()
        if strict_availability:
            realized = ledger.available_date.reindex(event_dates)
            valid &= realized.notna() & (realized < decision)
        samples = values.loc[valid].to_numpy(dtype=float)
        out[member] = samples[np.isfinite(samples)]
    return out


def _clip_weight(value: float, clip: float) -> float:
    if not np.isfinite(value):
        return 0.0
    if clip >= 999.0:
        return float(max(value, 0.0))
    return float(np.clip(value, 0.0, clip))


def explicit_weights(
    engine: sm.MarketEngine,
    events: pd.DataFrame,
    ledger: WeightLedger,
    spec: WeightSpec,
    strict_availability: bool,
) -> dict[int, dict[str, float]]:
    """按注册公式逐年扩窗；严格模式只接纳决策日前已实现 fwd20。"""
    _assert_index_window(events["trade_date"], f"{engine.instrument}.weight_events")
    weights: dict[int, dict[str, float]] = {}
    first_year = int(engine.dates[0].year)
    last_year = min(int(engine.dates[-1].year), WINDOW_END.year)
    for year in range(first_year, last_year + 1):
        samples = _member_samples(
            engine, events, ledger, year, strict_availability,
            percentage_points=spec.family in {
                "w2_mean", "w3_mean_log", "w4_shrink",
            },
        )
        row: dict[str, float] = {}
        if spec.family == "w4_shrink":
            member_means = {
                member: float(values.mean())
                for member, values in samples.items() if len(values) > 0
            }
            group_mean = (
                float(np.mean(list(member_means.values())))
                if member_means else np.nan
            )
            k = int(spec.parameter or 0)
            for member in engine.group:
                values = samples[member]
                n = len(values)
                if not np.isfinite(group_mean):
                    row[member] = 0.0
                    continue
                value = group_mean if n == 0 else (
                    n / (n + k) * float(values.mean())
                    + k / (n + k) * group_mean
                )
                row[member] = _clip_weight(value, spec.clip)
            weights[year] = row
            continue

        for member in engine.group:
            values = samples[member]
            n = len(values)
            if n < spec.min_n:
                row[member] = 0.0
                continue
            mean = float(values.mean())
            if spec.family == "w2_mean":
                value = mean
            elif spec.family == "w3_mean_log":
                value = mean * math.log1p(n)
            else:
                std = float(values.std(ddof=1))
                if not np.isfinite(std) or std == 0.0:
                    row[member] = 0.0
                    continue
                if spec.family == "w1_t":
                    value = mean / std * math.sqrt(n)
                elif spec.family == "w5_capped_t":
                    cap_n = int(spec.parameter or 0)
                    value = mean / std * math.sqrt(min(n, cap_n))
                else:
                    raise KeyError(spec.family)
            row[member] = _clip_weight(value, spec.clip)
        weights[year] = row
    return weights


@dataclass
class FeatureSet:
    engine: sm.MarketEngine
    ev_eff: pd.DataFrame
    weights: dict[int, dict[str, float]]
    score: np.ndarray
    max_weight: np.ndarray
    active: np.ndarray
    distinct_positive_members: dict[int, np.ndarray]


def build_feature(
    engine: sm.MarketEngine,
    events: pd.DataFrame,
    ledger: WeightLedger,
    spec: WeightSpec,
    strict_availability: bool,
) -> FeatureSet:
    # 事件作为派生输入再次显式截断后才进入权重/计分构造。
    events = events[
        events["trade_date"] < WINDOW_END_EXCLUSIVE
    ].copy()
    _assert_index_window(events["trade_date"], f"{engine.instrument}.feature_events")
    weights = explicit_weights(
        engine, events, ledger, spec, strict_availability,
    )
    ev = events.copy()
    ev["dist"] = engine.dist60.reindex(ev["trade_date"]).to_numpy()
    ev_eff = ev[~(
        ev["member"].isin(sm.RULES["cond_seats"])
        & (ev["dist"] >= 0.05)
    )].copy()
    _assert_index_window(ev_eff["trade_date"], f"{engine.instrument}.ev_eff")
    strong = (
        ev_eff.pivot_table(
            index="trade_date", columns="member", values="strength", aggfunc="max",
        )
        .reindex(engine.dates)
        .reindex(columns=engine.group)
    )
    weight_matrix = pd.DataFrame({
        member: [weights[day.year].get(member, 0.0) for day in engine.dates]
        for member in engine.group
    }, index=engine.dates)
    weighted = strong.fillna(0.0) * weight_matrix
    score = (
        weighted.rolling(sm.RULES["score_window"], min_periods=1)
        .max().sum(axis=1).to_numpy(dtype=float)
    )
    positive_effective = strong.notna() & (weight_matrix > 0)
    active = positive_effective.any(axis=1).to_numpy(bool)
    distinct_positive_members = {
        lookback: (
            strong.notna().astype(np.int8)
            .rolling(lookback, min_periods=1)
            .max()
            .where(weight_matrix > 0, 0)
            .sum(axis=1)
            .to_numpy(dtype=np.int16)
        )
        for lookback in B_LOOKBACKS
    }
    return FeatureSet(
        engine=engine,
        ev_eff=ev_eff,
        weights=weights,
        score=score,
        max_weight=weight_matrix.max(axis=1).to_numpy(dtype=float),
        active=active,
        distinct_positive_members=distinct_positive_members,
    )


class FeatureFactory:
    def __init__(
        self,
        engines: Mapping[str, sm.MarketEngine],
        *,
        strict_availability: bool,
        events_by_code: Mapping[str, pd.DataFrame] | None = None,
    ) -> None:
        self.engines = dict(engines)
        self.strict_availability = strict_availability
        self.events: dict[str, pd.DataFrame] = {}
        self.ledgers: dict[str, WeightLedger] = {}
        for code, engine in self.engines.items():
            assert_engine_window(engine)
            source_events = (
                events_by_code[code]
                if events_by_code is not None else engine.ev_long
            )
            events = source_events[
                source_events["trade_date"] < WINDOW_END_EXCLUSIVE
            ].copy()
            _assert_index_window(events["trade_date"], f"{code}.factory_events")
            self.events[code] = events
            self.ledgers[code] = build_weight_ledger(engine)
        self.cache: dict[tuple[str, str], FeatureSet] = {}

    def get(self, code: str, spec: WeightSpec) -> FeatureSet:
        key = (code, spec.effective_key)
        if key not in self.cache:
            self.cache[key] = build_feature(
                self.engines[code], self.events[code], self.ledgers[code],
                spec, self.strict_availability,
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
    b_key: str
    score: float


def cost_zone(
    feature: FeatureSet, day: pd.Timestamp,
) -> tuple[float, float, float] | None:
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
    if denominator == 0.0:
        return None
    center = numerator / denominator
    half = sm.RULES["zone_half_width"]
    return center - half, center + half, center


def suppression_mask(
    dates: pd.DatetimeIndex,
    windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]],
) -> np.ndarray:
    mask = np.zeros(len(dates), dtype=bool)
    for start, end in windows:
        if end >= WINDOW_END_EXCLUSIVE:
            raise AssertionError("压制窗口越过锁定研究期")
        mask |= (dates >= start) & (dates <= end)
    return mask


def replay_market(
    code: str,
    factory: FeatureFactory,
    schedule: Mapping[int, WeightSpec],
    suppress_windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]],
    b_schedule: Mapping[int, BSpec] | None = None,
) -> list[ResearchTrade]:
    """全账本连续重放；年度 A/B 规格只使用信号日已知状态。"""
    engine = factory.engines[code]
    assert_engine_window(engine)
    dates = engine.dates
    positions = {day: index for index, day in enumerate(dates)}
    low_real = engine.cont["low"].to_numpy(dtype=float)
    open_real = engine.cont["open"].to_numpy(dtype=float)
    close_real = engine.cont["close"].to_numpy(dtype=float)
    low_adj = engine.cont["adj_low"].to_numpy(dtype=float)
    open_adj = engine.cont["adj_open"].to_numpy(dtype=float)
    close_adj = engine.cont["adj_close"].to_numpy(dtype=float)
    factor = engine.cont["factor"].to_numpy(dtype=float)
    factor_last = factor[-1]
    suppressed = suppression_mask(dates, suppress_windows)

    signal_specs = [schedule.get(day.year, BASE) for day in dates]
    signal_b_specs = [
        (b_schedule or {}).get(day.year, NONE) for day in dates
    ]
    # 消退活跃位按 candidate 日的年度 A 规格拼接，不能将信号年的
    # 权重规格冻结到一笔跨年持仓的整个生命周期。
    daily_active = np.asarray([
        factory.get(code, spec).active[index]
        for index, spec in enumerate(signal_specs)
    ], dtype=bool)
    full_mask = np.zeros(len(dates), dtype=bool)
    relay_mask = np.zeros(len(dates), dtype=bool)
    for index, (day, spec, b_spec) in enumerate(
        zip(dates, signal_specs, signal_b_specs)
    ):
        feature = factory.get(code, spec)
        threshold = sm.RULES["theta_mult"] * feature.max_weight[index]
        b_ok = (
            not b_spec.enabled
            or int(feature.distinct_positive_members[
                int(b_spec.lookback_days)
            ][index]) >= int(b_spec.min_members)
        )
        score_ok = (
            threshold > 0.0
            and feature.score[index] >= threshold
            and b_ok
            and day >= WINDOW_START
        )
        relay_mask[index] = score_ok
        full_mask[index] = (
            score_ok
            and float(engine.dist60.iloc[index]) < sm.RULES["dist_low_max"]
            and float(engine.netq.iloc[index]) < sm.RULES["netq_max"]
        )

    trades: list[ResearchTrade] = []
    busy = -1
    relay_armed = False
    for day in dates:
        index = positions[day]
        if index + 1 >= len(dates) or index < busy:
            continue
        spec = signal_specs[index]
        b_spec = signal_b_specs[index]
        feature = factory.get(code, spec)
        is_full = bool(full_mask[index])
        is_relay = (
            not is_full
            and relay_armed
            and bool(relay_mask[index])
            and not bool(suppressed[index])
        )
        if not (is_full or is_relay):
            continue

        zone = None if is_relay else cost_zone(feature, day)
        entry_index: int | None = None
        entry_px_real: float | None = None
        if zone is not None:
            stop = min(
                index + 1 + sm.RULES["zone_valid_days"], len(dates),
            )
            for candidate in range(index + 1, stop):
                if candidate <= busy:
                    break
                if np.isfinite(low_real[candidate]) and low_real[candidate] <= zone[1]:
                    entry_px_real = (
                        min(open_real[candidate], zone[1])
                        if np.isfinite(open_real[candidate]) else zone[1]
                    )
                    entry_index = candidate
                    break
            if entry_index is None:
                trades.append(ResearchTrade(
                    code, day, None, None, None, None, None,
                    "未回踩放弃", None, is_relay, spec.key,
                    b_spec.key,
                    float(feature.score[index]),
                ))
                continue
        else:
            entry_index = index + 1
            entry_px_real = (
                open_real[entry_index]
                if np.isfinite(open_real[entry_index])
                else close_real[entry_index]
            )
            if not np.isfinite(entry_px_real):
                continue

        entry_px_adj = float(entry_px_real) * factor_last / factor[entry_index]
        stop_adj = entry_px_adj * (1.0 - sm.RULES["stop_loss"])
        exit_index: int | None = None
        exit_px_real: float | None = None
        result = ""
        ret_pct: float | None = None
        fade_from: int | None = None
        for candidate in range(entry_index, len(dates)):
            if not np.isfinite(low_adj[candidate]):
                continue
            if fade_from is not None and candidate > fade_from:
                px_adj = (
                    open_adj[candidate]
                    if np.isfinite(open_adj[candidate])
                    else close_adj[candidate]
                )
                exit_index = candidate
                exit_px_real = float(px_adj * factor[candidate] / factor_last)
                result = "消退卖出"
                ret_pct = float((px_adj / entry_px_adj - 1.0) * 100.0)
                break
            if low_adj[candidate] <= stop_adj:
                exit_index = candidate
                exit_px_real = float(stop_adj * factor[candidate] / factor_last)
                result = "止损"
                ret_pct = -float(sm.RULES["stop_loss"] * 100.0)
                break
            left = max(0, candidate - sm.RULES["fade_days"] + 1)
            quiet = not bool(daily_active[left:candidate + 1].any())
            if fade_from is None and candidate > entry_index + 2 and quiet:
                fade_from = candidate

        if exit_index is None:
            result = "持有中"
            ret_pct = float((close_adj[-1] / entry_px_adj - 1.0) * 100.0)
            busy = len(dates) - 1
            exit_date = None
        else:
            busy = exit_index
            exit_date = dates[exit_index]
        if result in {"消退卖出", "止损"}:
            relay_armed = True
        trades.append(ResearchTrade(
            code, day, dates[entry_index], float(entry_px_real), entry_px_adj,
            exit_date, exit_px_real, result, ret_pct, is_relay,
            spec.key, b_spec.key, float(feature.score[index]),
        ))
    return trades


def replay_schedule(
    factory: FeatureFactory,
    schedule: Mapping[int, WeightSpec],
    suppress_windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]],
    b_schedule: Mapping[int, BSpec] | None = None,
) -> list[ResearchTrade]:
    rows: list[ResearchTrade] = []
    for code in ("AU", "AG"):
        rows.extend(replay_market(
            code, factory, schedule, suppress_windows, b_schedule,
        ))
    return rows


def fixed_schedule(
    engines: Mapping[str, sm.MarketEngine], spec: WeightSpec,
) -> dict[int, WeightSpec]:
    first_year = min(int(engine.dates[0].year) for engine in engines.values())
    return {year: spec for year in range(first_year, WINDOW_END.year + 1)}


def fixed_b_schedule(
    engines: Mapping[str, sm.MarketEngine], spec: BSpec,
) -> dict[int, BSpec]:
    first_year = min(int(engine.dates[0].year) for engine in engines.values())
    return {year: spec for year in range(first_year, WINDOW_END.year + 1)}


def _safe_compare(left: Any, right: Any) -> bool:
    if isinstance(left, (float, np.floating)) or isinstance(right, float):
        return (
            (left is None and right is None)
            or (
                left is not None and right is not None
                and np.isclose(float(left), float(right), atol=1e-9, rtol=0.0)
            )
        )
    return bool((left == right) or (pd.isna(left) and pd.isna(right)))


def compare_engine_baseline(
    research: Sequence[ResearchTrade],
    engine_trades: Mapping[str, Sequence[sm.Trade]],
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for code in ("AU", "AG"):
        ours = [trade for trade in research if trade.market == code]
        theirs = list(engine_trades[code])
        if len(ours) != len(theirs):
            mismatches.append({
                "market": code,
                "field": "record_count",
                "research": len(ours),
                "engine": len(theirs),
            })
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
            for field, (ours_value, engine_value) in checks.items():
                if not _safe_compare(ours_value, engine_value):
                    mismatches.append({
                        "market": code,
                        "record": index,
                        "field": field,
                        "research": ours_value,
                        "engine": engine_value,
                    })
    return mismatches


def compare_engine_features(factory: FeatureFactory) -> list[dict[str, Any]]:
    """对拍年度权重、逐日分数/门槛/活跃位及成本区间。"""
    mismatches: list[dict[str, Any]] = []
    for code, engine in factory.engines.items():
        feature = factory.get(code, BASE)
        if set(feature.weights) != set(engine.weights):
            mismatches.append({
                "market": code, "field": "weight_years",
                "research": sorted(feature.weights),
                "engine": sorted(engine.weights),
            })
        for year in sorted(set(feature.weights) & set(engine.weights)):
            for member in engine.group:
                left = feature.weights[year].get(member, 0.0)
                right = engine.weights[year].get(member, 0.0)
                if not np.isclose(left, right, atol=1e-12, rtol=0.0):
                    mismatches.append({
                        "market": code, "field": "weight",
                        "year": year, "member": member,
                        "research": left, "engine": right,
                    })

        arrays = {
            "score": (
                feature.score, engine.score.to_numpy(dtype=float),
            ),
            "max_weight": (
                feature.max_weight,
                engine.wmat.max(axis=1).to_numpy(dtype=float),
            ),
        }
        for field, (left, right) in arrays.items():
            same = np.isclose(left, right, atol=1e-10, rtol=0.0, equal_nan=True)
            if not bool(same.all()):
                index = int(np.flatnonzero(~same)[0])
                mismatches.append({
                    "market": code, "field": field,
                    "date": engine.dates[index],
                    "research": left[index], "engine": right[index],
                })
        active_engine = engine.active.to_numpy(dtype=bool)
        if not np.array_equal(feature.active, active_engine):
            index = int(np.flatnonzero(feature.active != active_engine)[0])
            mismatches.append({
                "market": code, "field": "active",
                "date": engine.dates[index],
                "research": feature.active[index], "engine": active_engine[index],
            })

        for day in engine.dates:
            left = cost_zone(feature, day)
            right = engine.cost_zone(day)
            if left is None or right is None:
                same_zone = left is None and right is None
            else:
                same_zone = bool(np.allclose(
                    np.asarray(left, dtype=float), np.asarray(right, dtype=float),
                    atol=1e-9, rtol=0.0, equal_nan=True,
                ))
            if not same_zone:
                mismatches.append({
                    "market": code, "field": "cost_zone", "date": day,
                    "research": left, "engine": right,
                })
                break
    return mismatches


def run_baseline_parity(
    engines: Mapping[str, sm.MarketEngine],
    suppress_windows: Sequence[tuple[pd.Timestamp, pd.Timestamp]],
) -> tuple[dict[str, Any], list[ResearchTrade]]:
    """先以生产可用时点口径对拍，严格搜索口径另建 factory。"""
    production_factory = FeatureFactory(
        engines, strict_availability=False,
    )
    research = replay_schedule(
        production_factory, fixed_schedule(engines, BASE), suppress_windows,
    )
    engine_trades = {
        code: engine.replay(list(suppress_windows))
        for code, engine in engines.items()
    }
    mismatches = compare_engine_features(production_factory)
    mismatches.extend(compare_engine_baseline(research, engine_trades))
    payload = {
        "passed": not mismatches,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:100],
        "research_trade_records": len(research),
        "engine_trade_records": int(sum(map(len, engine_trades.values()))),
        "mode": "bounded production timing; W1/min30/clip5",
    }
    return payload, research


def strict_baseline_parity(
    engines: Mapping[str, sm.MarketEngine],
    suppress_windows: Sequence[tuple[pd.Timestamp, pd.Timestamp]],
) -> dict[str, Any]:
    factory = FeatureFactory(engines, strict_availability=True)
    research = replay_schedule(
        factory, fixed_schedule(engines, BASE), suppress_windows,
        fixed_b_schedule(engines, NONE),
    )
    engine_trades = {
        code: engine.replay(list(suppress_windows))
        for code, engine in engines.items()
    }
    mismatches = compare_engine_baseline(research, engine_trades)
    return {
        "period": f"2015-01-01~{WINDOW_END.date()}",
        "mismatch_count": len(mismatches),
        "passed": not mismatches,
        "mismatches": mismatches[:100],
        "note": "严格fwd20可得日门后的BASE账本 vs 生产engine",
    }


def executed(trades: Iterable[ResearchTrade]) -> list[ResearchTrade]:
    return [trade for trade in trades if trade.entry_date is not None]


def trade_summary(
    trades: Iterable[ResearchTrade],
    start: pd.Timestamp | None = None,
) -> dict[str, Any]:
    rows = executed(trades)
    if start is not None:
        rows = [trade for trade in rows if trade.signal_date >= start]
    done = [trade for trade in rows if trade.exit_date is not None]
    returns_done = np.asarray([trade.ret_pct for trade in done], dtype=float)
    returns_all = np.asarray([trade.ret_pct for trade in rows], dtype=float)
    return {
        "executed": len(rows),
        "closed": len(done),
        "open": len(rows) - len(done),
        "closed_sum": float(returns_done.sum()) if len(returns_done) else 0.0,
        "terminal_sum": float(returns_all.sum()) if len(returns_all) else 0.0,
        "closed_mean": float(returns_done.mean()) if len(returns_done) else None,
        "win_rate": (
            float((returns_done > 0).mean() * 100.0)
            if len(returns_done) else None
        ),
    }


def full_strategy_curve(
    trades: Iterable[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
) -> pd.Series:
    """完整账本算术P&L曲线；含跨评价边界的既有仓位。"""
    common = engines["AU"].dates.union(engines["AG"].dates)
    common = common[common < WINDOW_END_EXCLUSIVE]
    curve = pd.Series(0.0, index=common)
    for trade in executed(trades):
        if trade.entry_date is None:
            continue
        engine = engines[trade.market]
        contribution = pd.Series(np.nan, index=common)
        market_days = engine.dates[
            (engine.dates >= trade.entry_date)
            & (engine.dates < WINDOW_END_EXCLUSIVE)
        ]
        for day in market_days:
            if trade.exit_date is not None and day >= trade.exit_date:
                contribution.at[day] = float(trade.ret_pct)
            else:
                close = float(engine.cont.at[day, "adj_close"])
                contribution.at[day] = (
                    close / float(trade.entry_px_adj) - 1.0
                ) * 100.0
        contribution = contribution.ffill().fillna(0.0)
        curve = curve.add(contribution, fill_value=0.0)
    return curve


def normalized_period_curve(
    trades: Iterable[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
    start: pd.Timestamp,
) -> pd.Series:
    full = full_strategy_curve(trades, engines)
    before = full.index[full.index < start]
    origin = float(full.loc[before[-1]]) if len(before) else 0.0
    return full.loc[full.index >= start] - origin


def period_terminal_delta(
    candidate: Iterable[ResearchTrade],
    baseline: Iterable[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
    start: pd.Timestamp,
) -> tuple[float, float, float]:
    left = normalized_period_curve(candidate, engines, start)
    right = normalized_period_curve(baseline, engines, start)
    if left.empty or right.empty:
        return 0.0, 0.0, 0.0
    left_value = float(left.iloc[-1])
    right_value = float(right.iloc[-1])
    return left_value, right_value, left_value - right_value


def asof_returns(
    trades: Iterable[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
    cutoff: pd.Timestamp,
) -> np.ndarray:
    """只使用 cutoff 前已退出收益或 cutoff 前最后收盘的盯市值。"""
    if cutoff > WINDOW_END_EXCLUSIVE:
        raise AssertionError("训练截止日要求读取 2023+ 数据")
    values: list[float] = []
    for trade in executed(trades):
        if trade.entry_date is None or trade.entry_date >= cutoff:
            continue
        if trade.exit_date is not None and trade.exit_date < cutoff:
            values.append(float(trade.ret_pct))
            continue
        engine = engines[trade.market]
        before = np.flatnonzero(engine.dates < cutoff)
        if not len(before):
            continue
        terminal = float(engine.cont["adj_close"].iloc[int(before[-1])])
        values.append((terminal / float(trade.entry_px_adj) - 1.0) * 100.0)
    return np.asarray(values, dtype=float)


def criterion_score(values: np.ndarray, criterion: str) -> float:
    if len(values) < MIN_TRAIN:
        return -np.inf
    if criterion == "sum":
        return float(values.sum())
    std = float(values.std(ddof=1))
    if not np.isfinite(std) or std == 0.0:
        return -np.inf
    mean = float(values.mean())
    if criterion == "mean_se":
        return mean - std / math.sqrt(len(values))
    if criterion == "risk_adjusted":
        return mean / std
    raise KeyError(criterion)


def _variant_index(spec: WeightSpec) -> int:
    variants: list[tuple[str, int | None]] = [
        ("w1_t", None),
        ("w2_mean", None),
        ("w3_mean_log", None),
        *(("w4_shrink", k) for k in SHRINK_K_GRID),
        *(("w5_capped_t", cap_n) for cap_n in CAPPED_T_GRID),
    ]
    return variants.index((spec.family, spec.parameter))


def spec_distance(spec: WeightSpec) -> tuple[int, str]:
    distance = (
        abs(MIN_N_GRID.index(spec.min_n) - MIN_N_GRID.index(BASE.min_n))
        + abs(CLIP_GRID.index(spec.clip) - CLIP_GRID.index(BASE.clip))
        + _variant_index(spec)
    )
    return distance, spec.key


def select_walk_forward(
    specs: Sequence[WeightSpec],
    fixed_cache: Mapping[str, Sequence[ResearchTrade]],
    engines: Mapping[str, sm.MarketEngine],
    criterion: str,
    registry_ids: Mapping[str, str],
) -> tuple[dict[int, WeightSpec], list[dict[str, Any]], list[dict[str, Any]]]:
    """每个 Y 对每个候选做 2015~Y-1 的内生全账本训练。"""
    first_year = min(int(engine.dates[0].year) for engine in engines.values())
    schedule = {year: BASE for year in range(first_year, WF_START)}
    path: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for year in range(WF_START, WF_END + 1):
        cutoff = pd.Timestamp(f"{year}-01-01")
        scored: list[tuple[float, WeightSpec, int]] = []
        for spec in specs:
            values = asof_returns(
                fixed_cache[spec.effective_key], engines, cutoff,
            )
            score = criterion_score(values, criterion)
            eligible = bool(np.isfinite(score))
            if eligible:
                scored.append((score, spec, len(values)))
            audit.append({
                "decision": "walk_forward",
                "criterion": criterion,
                "year": year,
                "registered_id": registry_ids[spec.key],
                "effective_key": spec.effective_key,
                "train_n": len(values),
                "eligible": eligible,
                "train_score": float(score) if eligible else None,
            })

        if not scored:
            picked = BASE
            picked_score = None
            train_n = 0
            status = "训练不足回退现行"
        else:
            best = max(item[0] for item in scored)
            tied = [
                item for item in scored
                if np.isclose(item[0], best, atol=1e-12, rtol=0.0)
            ]
            picked_score, picked, train_n = min(
                tied, key=lambda item: spec_distance(item[1]),
            )
            picked_score = float(picked_score)
            status = "selected"
        schedule[year] = picked
        eligible_effective = {
            spec.effective_key for _, spec, _ in scored
        }
        path.append({
            "criterion": criterion,
            "criterion_label": CRITERION_LABELS[criterion],
            "year": year,
            "train_period": f"2015-01-01~{year - 1}-12-31",
            "train_rule": "entry_date < Y-01-01；未退出交易按 Y 前末个收盘盯市",
            "min_train": MIN_TRAIN,
            "eligible_registered_count": len(scored),
            "eligible_effective_count": len(eligible_effective),
            "train_n": train_n,
            "train_score": picked_score,
            "status": status,
            "registered_id": registry_ids[picked.key],
            "picked": picked.as_dict(),
        })
    return schedule, path, audit


def select_deployment_fit(
    specs: Sequence[WeightSpec],
    fixed_cache: Mapping[str, Sequence[ResearchTrade]],
    engines: Mapping[str, sm.MarketEngine],
    criterion: str,
    registry_ids: Mapping[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """用截至 2022 年末的账本选一个部署规格；不回放部署年份。"""
    cutoff = WINDOW_END_EXCLUSIVE
    scored: list[tuple[float, WeightSpec, int]] = []
    audit: list[dict[str, Any]] = []
    for spec in specs:
        values = asof_returns(
            fixed_cache[spec.effective_key], engines, cutoff,
        )
        score = criterion_score(values, criterion)
        eligible = bool(np.isfinite(score))
        if eligible:
            scored.append((score, spec, len(values)))
        audit.append({
            "decision": "deployment_fit",
            "criterion": criterion,
            "cutoff_exclusive": str(cutoff.date()),
            "registered_id": registry_ids[spec.key],
            "effective_key": spec.effective_key,
            "train_n": len(values),
            "eligible": eligible,
            "train_score": float(score) if eligible else None,
        })

    if not scored:
        picked = BASE
        picked_score = None
        train_n = 0
        status = "训练不足回退现行"
    else:
        best = max(item[0] for item in scored)
        tied = [
            item for item in scored
            if np.isclose(item[0], best, atol=1e-12, rtol=0.0)
        ]
        picked_score, picked, train_n = min(
            tied, key=lambda item: spec_distance(item[1]),
        )
        picked_score = float(picked_score)
        status = "selected"
    result = {
        "criterion": criterion,
        "criterion_label": CRITERION_LABELS[criterion],
        "training_period": "2015-01-01~2022-12-31",
        "cutoff_exclusive": str(cutoff.date()),
        "replay_after_cutoff_performed": False,
        "min_train": MIN_TRAIN,
        "eligible_registered_count": len(scored),
        "eligible_effective_count": len({
            spec.effective_key for _, spec, _ in scored
        }),
        "train_n": train_n,
        "train_score": picked_score,
        "status": status,
        "registered_id": registry_ids[picked.key],
        "picked": picked.as_dict(),
    }
    return result, audit


def b_spec_distance(spec: BSpec) -> tuple[int, str]:
    """精确同分时先取更简单的 NONE，再按封存前注册顺序。"""
    return B_SPECS.index(spec), spec.key


def _select_b_at_cutoff(
    a_spec: WeightSpec,
    b_specs: Sequence[BSpec],
    joint_fixed_cache: Mapping[tuple[str, str], Sequence[ResearchTrade]],
    engines: Mapping[str, sm.MarketEngine],
    criterion: str,
    cutoff: pd.Timestamp,
    decision: str,
) -> tuple[BSpec, dict[str, Any], list[dict[str, Any]]]:
    """在一个已经由训练期选定的 A 规格下选择独立席位确认门。"""
    scored: list[tuple[float, BSpec, int]] = []
    audit: list[dict[str, Any]] = []
    for b_spec in b_specs:
        values = asof_returns(
            joint_fixed_cache[(a_spec.effective_key, b_spec.key)],
            engines,
            cutoff,
        )
        score = criterion_score(values, criterion)
        eligible = bool(np.isfinite(score))
        if eligible:
            scored.append((score, b_spec, len(values)))
        audit.append({
            "decision": decision,
            "criterion": criterion,
            "cutoff_exclusive": str(cutoff.date()),
            "a_effective_key": a_spec.effective_key,
            "b_key": b_spec.key,
            "train_n": len(values),
            "eligible": eligible,
            "train_score": float(score) if eligible else None,
        })

    if not scored:
        picked = NONE
        picked_score = None
        train_n = 0
        status = "训练不足回退现行"
    else:
        best = max(item[0] for item in scored)
        tied = [
            item for item in scored
            if np.isclose(item[0], best, atol=1e-12, rtol=0.0)
        ]
        picked_score, picked, train_n = min(
            tied, key=lambda item: b_spec_distance(item[1]),
        )
        picked_score = float(picked_score)
        status = "selected"
    row = {
        "criterion": criterion,
        "criterion_label": CRITERION_LABELS[criterion],
        "cutoff_exclusive": str(cutoff.date()),
        "a_fixed_for_b_training": a_spec.as_dict(),
        "min_train": MIN_TRAIN,
        "eligible_count": len(scored),
        "train_n": train_n,
        "train_score": picked_score,
        "status": status,
        "picked": picked.as_dict(),
    }
    return picked, row, audit


def select_b_walk_forward(
    a_schedule: Mapping[int, WeightSpec],
    b_specs: Sequence[BSpec],
    joint_fixed_cache: Mapping[tuple[str, str], Sequence[ResearchTrade]],
    engines: Mapping[str, sm.MarketEngine],
    criterion: str,
) -> tuple[dict[int, BSpec], list[dict[str, Any]], list[dict[str, Any]]]:
    """先接受每年已选 A，再仅用 Y 年之前账本选当年 B。"""
    first_year = min(int(engine.dates[0].year) for engine in engines.values())
    schedule = {year: NONE for year in range(first_year, WF_START)}
    path: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for year in range(WF_START, WF_END + 1):
        a_spec = a_schedule[year]
        picked, row, rows = _select_b_at_cutoff(
            a_spec,
            b_specs,
            joint_fixed_cache,
            engines,
            criterion,
            pd.Timestamp(f"{year}-01-01"),
            "b_walk_forward",
        )
        schedule[year] = picked
        path.append({
            **row,
            "year": year,
            "train_period": f"2015-01-01~{year - 1}-12-31",
            "train_rule": "先定 A；B 仅使用 entry_date < Y-01-01 的固定 A/B 账本",
        })
        audit.extend(rows)
    return schedule, path, audit


def select_b_deployment_fit(
    a_spec: WeightSpec,
    b_specs: Sequence[BSpec],
    joint_fixed_cache: Mapping[tuple[str, str], Sequence[ResearchTrade]],
    engines: Mapping[str, sm.MarketEngine],
    criterion: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    picked, row, audit = _select_b_at_cutoff(
        a_spec,
        b_specs,
        joint_fixed_cache,
        engines,
        criterion,
        WINDOW_END_EXCLUSIVE,
        "b_deployment_fit",
    )
    row.update({
        "training_period": "2015-01-01~2022-12-31",
        "replay_after_cutoff_performed": False,
        "picked": picked.as_dict(),
    })
    return row, audit


def daily_oos_curve(
    trades: Iterable[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
) -> pd.Series:
    rebuilt = normalized_period_curve(
        trades, engines, pd.Timestamp(f"{WF_START}-01-01"),
    )
    _assert_index_window(rebuilt.index, "daily_oos_curve")
    return rebuilt


def calendar_slices(
    candidate: Sequence[ResearchTrade],
    baseline: Sequence[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
) -> list[dict[str, Any]]:
    candidate_curve = daily_oos_curve(candidate, engines)
    baseline_curve = daily_oos_curve(baseline, engines)
    rows = calendar_slices_from_curves(candidate_curve, baseline_curve)
    return attach_calendar_entry_counts(rows, candidate, baseline)


def calendar_slices_from_curves(
    candidate_curve: pd.Series, baseline_curve: pd.Series,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    candidate_previous = baseline_previous = 0.0
    for year in range(WF_START, WF_END + 1):
        year_days = candidate_curve.index[candidate_curve.index.year == year]
        if not len(year_days):
            continue
        last = year_days[-1]
        candidate_now = float(candidate_curve.loc[last])
        baseline_now = float(baseline_curve.loc[last])
        candidate_increment = candidate_now - candidate_previous
        baseline_increment = baseline_now - baseline_previous
        rows.append({
            "year": year,
            "period": f"{year}-01-01~{last.date()}",
            "candidate_pnl_increment": candidate_increment,
            "baseline_pnl_increment": baseline_increment,
            "delta": candidate_increment - baseline_increment,
            "non_loss": candidate_increment >= baseline_increment - 1e-12,
        })
        candidate_previous = candidate_now
        baseline_previous = baseline_now
    return rows


def attach_calendar_entry_counts(
    rows: list[dict[str, Any]],
    candidate: Sequence[ResearchTrade],
    baseline: Sequence[ResearchTrade],
) -> list[dict[str, Any]]:
    for row in rows:
        year = int(row["year"])
        row["candidate_new_entries"] = sum(
            trade.entry_date is not None and trade.entry_date.year == year
            for trade in candidate
        )
        row["baseline_new_entries"] = sum(
            trade.entry_date is not None and trade.entry_date.year == year
            for trade in baseline
        )
        row["pnl_basis"] = "日历账本增量（含跨年carry）；n为当年新进场"
    return rows


def yearly_oos(
    candidate: Sequence[ResearchTrade],
    baseline: Sequence[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
) -> list[dict[str, Any]]:
    """按入场年在同一当年末估值，2022 持仓只盯市到窗口末。"""
    def cohort_asof(
        trades: Sequence[ResearchTrade], year: int, cutoff: pd.Timestamp,
    ) -> dict[str, Any]:
        rows = [
            trade for trade in executed(trades)
            if trade.signal_date >= pd.Timestamp(f"{WF_START}-01-01")
            and trade.entry_date is not None
            and trade.entry_date.year == year
            and trade.entry_date < cutoff
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
            values.append(
                (terminal / float(trade.entry_px_adj) - 1.0) * 100.0
            )
        return {
            "executed_asof": len(values),
            "realized_asof": realized,
            "open_mtm_asof": len(values) - realized,
            "terminal_sum_asof": float(sum(values)),
        }

    rows: list[dict[str, Any]] = []
    for year in range(WF_START, WF_END + 1):
        cutoff = min(
            pd.Timestamp(f"{year + 1}-01-01"), WINDOW_END_EXCLUSIVE,
        )
        candidate_row = cohort_asof(candidate, year, cutoff)
        baseline_row = cohort_asof(baseline, year, cutoff)
        rows.append({
            "year": year,
            "cohort_basis": "entry_year；且 signal_date>=2019-01-01",
            "valuation_period": (
                f"{year}-01-01~{cutoff - pd.Timedelta(days=1):%Y-%m-%d}"
            ),
            "candidate": candidate_row,
            "baseline": baseline_row,
            "cohort_delta_asof": (
                candidate_row["terminal_sum_asof"]
                - baseline_row["terminal_sum_asof"]
            ),
        })
    return rows


def _registry(specs: Sequence[WeightSpec]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rows: list[dict[str, Any]] = []
    identifiers: dict[str, str] = {}
    for index, spec in enumerate(specs, 1):
        registered_id = f"A{index:03d}"
        identifiers[spec.key] = registered_id
        rows.append({"registered_id": registered_id, **spec.as_dict()})
    return rows, identifiers


def _b_registry() -> list[dict[str, Any]]:
    rationales = {
        "B1": "一周内至少两个受信任来源表达同向增多，可过滤单席位调仓噪音并容纳错峰执行。",
        "B2": "三日内第二来源快速确认，信息更新鲜，较少拼接互不相关的旧事件。",
        "B3": "一周内三个来源可进一步降低单一客户流、套保或报告噪音的影响。",
        "B4": "三日内三来源代表最密集的独立确认，理论精度最高但最可能漏样本。",
        "B5": "八日内两来源容纳大资金分批、错峰建仓，但承担事件陈旧风险。",
        "B6": "八日内三来源用更高广度抵消长窗口偶然拼接，但可能只捕捉迟到共识。",
    }
    return [
        {
            **spec.as_dict(),
            "registered_candidate": spec is not NONE,
            "economic_rationale": (
                rationales[spec.key] if spec is not NONE else "现行规则对照"
            ),
            "family_hypothesis": (
                "现行加权总分可能由单一高权重席位独自越阈；同一短交易日窗"
                "内多个正权重席位各自发生既有有效增多，来源更分散，较可能"
                "是可跟随的共同判断。"
            ),
            "applies_to": "主信号与中继信号",
            "failed_gate_action": "当日信号作废，不排队",
        }
        for spec in B_SPECS
    ]


def run_stage1_a(
    engines: Mapping[str, sm.MarketEngine],
    suppress_windows: Sequence[tuple[pd.Timestamp, pd.Timestamp]],
    specs: Sequence[WeightSpec],
    *,
    factory: FeatureFactory | None = None,
    detailed: bool = True,
    progress: bool = True,
) -> dict[str, Any]:
    """严格阶段一 A→B 层级搜索；返回 JSON 可序列化结果。"""
    factory = factory or FeatureFactory(
        engines, strict_availability=True,
    )
    representatives: dict[str, WeightSpec] = {}
    for spec in specs:
        representatives.setdefault(spec.effective_key, spec)

    fixed_cache: dict[str, list[ResearchTrade]] = {}
    effective_results: list[dict[str, Any]] = []
    total = len(representatives)
    for index, (effective_key, spec) in enumerate(representatives.items(), 1):
        trades = replay_schedule(
            factory, fixed_schedule(engines, spec), suppress_windows,
        )
        fixed_cache[effective_key] = trades
        if detailed:
            effective_results.append({
                "effective_key": effective_key,
                "representative": spec.as_dict(),
                "full_ledger_summary": trade_summary(trades),
                "oos_summary": trade_summary(
                    trades, pd.Timestamp(f"{WF_START}-01-01"),
                ),
            })
        if progress and (index % 25 == 0 or index == total):
            print(f"[stage1-a] fixed replay {index}/{total}", flush=True)

    strict_baseline = fixed_cache[BASE.effective_key]
    registry, registry_ids = _registry(specs)
    criteria_results: dict[str, Any] = {}
    candidate_scores: dict[str, dict[str, list[dict[str, Any]]]] = {}
    joint_fixed_cache: dict[tuple[str, str], list[ResearchTrade]] = {
        (effective_key, NONE.key): trades
        for effective_key, trades in fixed_cache.items()
    }

    def ensure_joint_cache(a_spec: WeightSpec) -> None:
        for b_spec in B_SPECS:
            cache_key = (a_spec.effective_key, b_spec.key)
            if cache_key in joint_fixed_cache:
                continue
            joint_fixed_cache[cache_key] = replay_schedule(
                factory,
                fixed_schedule(engines, a_spec),
                suppress_windows,
                fixed_b_schedule(engines, b_spec),
            )

    baseline_oos = trade_summary(
        strict_baseline, pd.Timestamp(f"{WF_START}-01-01"),
    )
    baseline_calendar_curve = daily_oos_curve(strict_baseline, engines)
    for criterion in CRITERIA:
        schedule, path, audit = select_walk_forward(
            specs, fixed_cache, engines, criterion, registry_ids,
        )
        deployment_fit, deployment_audit = select_deployment_fit(
            specs, fixed_cache, engines, criterion, registry_ids,
        )
        deployment_a_spec = next(
            spec for spec in specs
            if spec.key == deployment_fit["picked"]["key"]
        )
        selected_a_specs = {
            spec.effective_key: spec
            for spec in [
                *(schedule[year] for year in range(WF_START, WF_END + 1)),
                deployment_a_spec,
            ]
        }
        for selected_a in selected_a_specs.values():
            ensure_joint_cache(selected_a)
        b_schedule, b_path, b_audit = select_b_walk_forward(
            schedule, B_SPECS, joint_fixed_cache, engines, criterion,
        )
        b_deployment_fit, b_deployment_audit = select_b_deployment_fit(
            deployment_a_spec,
            B_SPECS,
            joint_fixed_cache,
            engines,
            criterion,
        )
        b_reference = trade_summary(
            joint_fixed_cache[(deployment_a_spec.effective_key, NONE.key)],
        )
        b_fixed_results = []
        for b_candidate in B_SPECS:
            candidate_summary = trade_summary(
                joint_fixed_cache[
                    (deployment_a_spec.effective_key, b_candidate.key)
                ],
            )
            b_fixed_results.append({
                "b": b_candidate.as_dict(),
                "period": "2015-01-01~2022-12-31（固定部署A，过拟合参照）",
                "executed": candidate_summary["executed"],
                "closed": candidate_summary["closed"],
                "open": candidate_summary["open"],
                "terminal_sum": candidate_summary["terminal_sum"],
                "vs_none_terminal_delta": (
                    candidate_summary["terminal_sum"]
                    - b_reference["terminal_sum"]
                ),
            })
        adaptive = replay_schedule(
            factory, schedule, suppress_windows, b_schedule,
        )
        summary = trade_summary(
            adaptive, pd.Timestamp(f"{WF_START}-01-01"),
        )
        candidate_calendar_curve = daily_oos_curve(adaptive, engines)
        calendar = calendar_slices_from_curves(
            candidate_calendar_curve, baseline_calendar_curve,
        )
        calendar = attach_calendar_entry_counts(
            calendar, adaptive, strict_baseline,
        )
        calendar_candidate = (
            float(candidate_calendar_curve.iloc[-1])
            if len(candidate_calendar_curve) else 0.0
        )
        calendar_baseline = (
            float(baseline_calendar_curve.iloc[-1])
            if len(baseline_calendar_curve) else 0.0
        )
        calendar_delta = calendar_candidate - calendar_baseline
        criteria_results[criterion] = {
            "label": CRITERION_LABELS[criterion],
            "primary": criterion == PRIMARY_CRITERION,
            "schedule": {
                str(year): {
                    "a": schedule[year].as_dict(),
                    "b": b_schedule.get(year, NONE).as_dict(),
                }
                for year in range(2015, 2023)
            },
            "selection_path": {
                "a_first": path,
                "b_after_a": b_path,
            },
            "deployment_fit": {
                "a_first": deployment_fit,
                "b_after_a": b_deployment_fit,
            },
            "b_deployment_fixed_results": b_fixed_results,
            "oos_summary": summary,
            "strict_baseline_oos_summary": baseline_oos,
            "calendar_terminal_candidate": calendar_candidate,
            "calendar_terminal_baseline": calendar_baseline,
            "terminal_delta": calendar_delta,
            "yearly_oos": yearly_oos(
                adaptive, strict_baseline, engines,
            ),
            "calendar_oos": calendar,
        }
        candidate_scores[criterion] = (
            {
                "a": audit + deployment_audit,
                "b": b_audit + b_deployment_audit,
            }
            if detailed else {"a": [], "b": []}
        )

    return {
        "registry": registry,
        "b_registry": _b_registry(),
        "effective_replays": effective_results,
        "strict_baseline": {
            "spec": BASE.as_dict(),
            "full_ledger_summary": trade_summary(strict_baseline),
            "oos_summary": baseline_oos,
        },
        "criteria": criteria_results,
        "candidate_scores": candidate_scores,
    }


def shift_events_partitioned(
    events: pd.DataFrame,
    dates: pd.DatetimeIndex,
    rng: np.random.Generator,
    *,
    boundary: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """逐事件偏移±5~10交易日；封存边界两侧绝不互穿。"""
    if events.empty:
        return events.copy(), {"input": 0, "output": 0, "collisions": 0}
    positions = {day: index for index, day in enumerate(dates)}
    rows: list[dict[str, Any]] = []
    offsets = (*range(-10, -4), *range(5, 11))
    ordered = events.sort_values(
        ["trade_date", "member", "strength"], kind="mergesort",
    ).to_dict("records")
    for row in ordered:
        source_day = pd.Timestamp(row["trade_date"])
        source_index = positions.get(source_day)
        if source_index is None:
            continue
        feasible: list[int] = []
        for offset in offsets:
            target_index = source_index + offset
            if not (0 <= target_index < len(dates)):
                continue
            target_day = dates[target_index]
            if boundary is not None and (
                (source_day < boundary) != (target_day < boundary)
            ):
                continue
            feasible.append(offset)
        if not feasible:
            raise AssertionError(f"{source_day.date()} 无同分区±5~10日偏移")
        offset = feasible[int(rng.integers(0, len(feasible)))]
        shifted = dict(row)
        shifted["trade_date"] = dates[source_index + offset]
        rows.append(shifted)
    frame = pd.DataFrame(rows, columns=events.columns)
    before = len(frame)
    frame = (
        frame.sort_values("strength", ascending=False, kind="mergesort")
        .drop_duplicates(["member", "trade_date"], keep="first")
        .sort_values(["trade_date", "member"], kind="mergesort")
        .reset_index(drop=True)
    )
    return frame, {
        "input": int(len(events)),
        "output": int(len(frame)),
        "collisions": int(before - len(frame)),
    }


def shifted_suppress_windows(
    ag: sm.MarketEngine,
    short_events: pd.DataFrame,
    ratio: pd.Series,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    proxy = copy.copy(ag)
    proxy.ev_short = short_events
    return sm.flee_suppress_windows(proxy, ratio)


def event_stream_rng(
    seed: int, rep: int, code: str, direction: str,
) -> np.random.Generator:
    """各市场/方向独立流，保证阶段三扩展同一随机世界时前缀不变。"""
    code_id = {"AU": 1, "AG": 2}[code]
    direction_id = {"long": 1, "short": 2}[direction]
    return np.random.default_rng(
        np.random.SeedSequence([int(seed), int(rep + 1), code_id, direction_id]),
    )


def event_frame_sha256(events: pd.DataFrame) -> str:
    columns = [column for column in (
        "member", "trade_date", "strength", "hands",
    ) if column in events]
    frame = events[columns].copy()
    if "trade_date" in frame:
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.strftime(
            "%Y-%m-%d",
        )
    hashed = pd.util.hash_pandas_object(
        frame.astype("string").fillna("<NA>"), index=False,
    ).to_numpy(dtype=np.uint64)
    return hashlib.sha256(hashed.tobytes()).hexdigest()


def placebo_test(
    sample: Sequence[float], real: float,
) -> dict[str, Any]:
    values = np.asarray(sample, dtype=float)
    if len(values) < 100 or not np.isfinite(values).all():
        raise AssertionError("安慰剂样本不足或包含非有限值")
    quantile_levels = (0.0, 0.05, 0.25, 0.5, 0.75, 0.95, 1.0)
    quantiles = np.quantile(values, quantile_levels, method="higher")
    p_value = (1 + int((values >= real).sum())) / (len(values) + 1)
    return {
        "reps": int(len(values)),
        "real_terminal_delta": float(real),
        "quantiles": {
            label: float(value) for label, value in zip(
                ("min", "5%", "25%", "50%", "75%", "95%", "max"),
                quantiles,
            )
        },
        "q95_method": "numpy.quantile(method='higher')",
        "empirical_p": float(p_value),
        "pass": bool(real > quantiles[5] and p_value < 0.05),
    }


def stage1_placebo_distribution(
    engines: Mapping[str, sm.MarketEngine],
    ratio: pd.Series,
    specs: Sequence[WeightSpec],
    reps: int,
    seed: int,
) -> dict[str, Any]:
    """每次在随机世界完整重算年度权重及 A→B 两级选择。"""
    if reps < 100:
        raise ValueError("安慰剂硬门要求至少100次")
    values = {criterion: [] for criterion in CRITERIA}
    collision_rows: list[dict[str, Any]] = []
    deployment_fits: list[dict[str, Any]] = []
    for rep in range(reps):
        shifted_long: dict[str, pd.DataFrame] = {}
        shifted_short: dict[str, pd.DataFrame] = {}
        audit: dict[str, Any] = {"rep": rep + 1}
        for code in ("AU", "AG"):
            shifted_long[code], long_audit = shift_events_partitioned(
                engines[code].ev_long, engines[code].dates,
                event_stream_rng(seed, rep, code, "long"),
                boundary=pd.Timestamp("2023-01-01"),
            )
            shifted_short[code], short_audit = shift_events_partitioned(
                engines[code].ev_short, engines[code].dates,
                event_stream_rng(seed, rep, code, "short"),
                boundary=pd.Timestamp("2023-01-01"),
            )
            long_audit["prefix_sha256"] = event_frame_sha256(shifted_long[code])
            short_audit["prefix_sha256"] = event_frame_sha256(shifted_short[code])
            audit[f"{code}_long"] = long_audit
            audit[f"{code}_short"] = short_audit
        factory = FeatureFactory(
            engines, strict_availability=True,
            events_by_code=shifted_long,
        )
        suppress = shifted_suppress_windows(
            engines["AG"], shifted_short["AG"], ratio,
        )
        result = run_stage1_a(
            engines, suppress, specs, factory=factory,
            detailed=False, progress=False,
        )
        for criterion in CRITERIA:
            values[criterion].append(
                float(result["criteria"][criterion]["terminal_delta"]),
            )
        deployment_fits.append({
            criterion: result["criteria"][criterion]["deployment_fit"]
            for criterion in CRITERIA
        })
        collision_rows.append(audit)
        if (rep + 1) % 5 == 0:
            print(f"[stage1-placebo] {rep + 1}/{reps}", flush=True)
    return {
        "seed": seed,
        "reps": reps,
        "offsets": "各市场交易日±{5,6,7,8,9,10}；边界同分区抽取",
        "collision_rule": "同member+shifted_date保留strength最大事件",
        "values": values,
        "deployment_fits": deployment_fits,
        "collision_audit": collision_rows,
    }


def now_shanghai() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    cleaned = _json_clean(dict(payload))
    cleaned.pop("seal_payload_sha256", None)
    raw = json.dumps(
        cleaned, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def weight_spec_from_dict(row: Mapping[str, Any]) -> WeightSpec:
    parameter = row.get("shrink_k")
    if row["family"] == "w5_capped_t":
        parameter = row.get("cap_n")
    return WeightSpec(
        str(row["family"]), int(row["min_n"]), float(row["clip"]),
        int(parameter) if parameter is not None else None,
    )


def b_spec_from_dict(row: Mapping[str, Any]) -> BSpec:
    key = str(row["key"])
    matches = [spec for spec in B_SPECS if spec.key == key]
    if len(matches) != 1:
        raise ValueError(f"未知B规格: {key}")
    return matches[0]


def stage1_discovery_gate(
    search: Mapping[str, Any],
    placebo: Mapping[str, Any],
) -> dict[str, Any]:
    primary = search["criteria"][PRIMARY_CRITERION]
    calendar = primary["calendar_oos"]
    non_loss = int(sum(bool(row["non_loss"]) for row in calendar))
    required = math.ceil(len(calendar) * 2 / 3)
    test = placebo_test(
        placebo["values"][PRIMARY_CRITERION],
        float(primary["terminal_delta"]),
    )
    n = int(primary["oos_summary"]["executed"])
    checks = {
        "terminal_delta_positive": float(primary["terminal_delta"]) > 0.0,
        "candidate_n_at_least_30": n >= 30,
        "calendar_non_loss_gate": non_loss >= required,
        "placebo_gate": bool(test["pass"]),
    }
    return {
        "period": "2019-01-01~2022-12-31",
        "criterion": PRIMARY_CRITERION,
        "candidate_executed": n,
        "terminal_delta": float(primary["terminal_delta"]),
        "non_loss_years": non_loss,
        "year_slices": len(calendar),
        "required_non_loss_years": required,
        "placebo": test,
        "checks": checks,
        "pass": bool(all(checks.values())),
    }


def selected_deployment(
    search: Mapping[str, Any], discovery: Mapping[str, Any],
) -> dict[str, Any]:
    if discovery["pass"]:
        fit = search["criteria"][PRIMARY_CRITERION]["deployment_fit"]
        a = dict(fit["a_first"]["picked"])
        b = dict(fit["b_after_a"]["picked"])
        status = "阶段一硬门通过，封存主准则部署拟合"
    else:
        a = BASE.as_dict()
        b = NONE.as_dict()
        status = "阶段一硬门未全过，封存现行权重与无新增规则"
    return {
        "criterion": PRIMARY_CRITERION,
        "a_weight_spec": a,
        "b_rule": b,
        "status": status,
        "post_2022_adjustment_allowed": False,
        "carried_trade_semantics": (
            "信号/成本区使用信号日年度规格；持仓消退按候选日年度规格重算"
        ),
    }


def _md_escape(value: Any) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "—"
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
    elif isinstance(value, bool):
        text = "是" if value else "否"
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def markdown_table(
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str] | None = None,
) -> str:
    if not rows:
        return "（无）"
    columns = list(columns or rows[0].keys())
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for row in rows:
        lines.append(
            "| " + " | ".join(_md_escape(row.get(column)) for column in columns)
            + " |"
        )
    return "\n".join(lines)


SEALED_BEGIN = "<!-- WEIGHTSPEC_SEALED_JSON_BEGIN"
SEALED_END = "WEIGHTSPEC_SEALED_JSON_END -->"


def render_prereg(payload: Mapping[str, Any]) -> str:
    search = payload["stage1"]["search"]
    discovery = payload["stage1"]["discovery_gate"]
    selected = payload["sealed_deployment"]
    b_rows = [
        {
            "候选": row["key"],
            "K": row["min_members"],
            "L(交易日)": row["lookback_trading_days"],
            "经济理由（测试前写定）": row["economic_rationale"],
        }
        for row in search["b_registry"] if row["registered_candidate"]
    ]
    criteria_rows = []
    conditional_pairs = {
        (row["a_effective_key"], row["b_key"])
        for criterion in CRITERIA
        for row in search["candidate_scores"][criterion]["b"]
        if row["b_key"] != "NONE"
    }
    for criterion in CRITERIA:
        result = search["criteria"][criterion]
        fit = result["deployment_fit"]
        criteria_rows.append({
            "准则": CRITERION_LABELS[criterion],
            "period": "2019-2022",
            "n": result["oos_summary"]["executed"],
            "terminal_delta": result["terminal_delta"],
            "部署A": fit["a_first"]["picked"]["key"],
            "部署B": fit["b_after_a"]["picked"]["key"],
        })
    machine = json.dumps(
        _json_clean(dict(payload)), ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    )
    expectation = (
        "预期封存期仍应取得正向账本差值、至少30笔，并通过固定方案安慰剂；"
        "若任一门失败，不回调参数。"
        if discovery["pass"] else
        "阶段一未同时通过账本、年度与安慰剂硬门；封存BASE+NONE，预期封存期"
        "不会出现可归因于调参的优势。"
    )
    return "\n".join([
        "# 权重机制与一条新规则：阶段二封存",
        "",
        f"- 封存时间（本机Asia/Shanghai）：`{payload['sealed_at']}`",
        f"- 随机种子：`{payload['seed']}`；阶段一安慰剂：`{payload['placebos']}` 次。",
        f"- 脚本 SHA-256：`{payload['script_sha256']}`",
        f"- 只读引擎 SHA-256：`{payload['engine_sha256']}`",
        f"- 封存载荷 SHA-256：`{payload['seal_payload_sha256']}`",
        "- 阶段一进程只构造文件起点至2022-12-31的数据；读取器会机械解析首个边界记录用于硬停，visibility会解析R作可见日输入门，但这些越界行不进入DataFrame、manifest、特征或统计。这不是物理字节零读取。",
        "- 主选参准则预先固定为(b)单笔均值-SE；(a)/(c)只作对照，不能看OOS后改主准则。",
        "",
        "## 1. B规则在结果前冻结的经济假设",
        "",
        "现行加权总分可能被单一高权重席位独自越过。新增规则只验证入场信息来源广度：过去L个交易日内，至少K个当前正权重席位各自出现既有有效增多。它同时约束主信号与中继；未过门的当日触发作废、不排队；不改变离场。它不使用减多、增空、翻空、OI、价格回撤或离场动作，因此与已证伪规则不同。",
        "",
        markdown_table(b_rows),
        "",
        "## 2. 阶段一试过的全部东西",
        "",
        "- A：315个注册格全部评分；W4的hard-min标签不入公式，故90个结构重复、225个有效唯一实现。未新增维度。",
        "- B：上表6个参数变体全部测试；NONE仅为现行比较项，不计候选。",
        f"- 注册候选总数=321（A的315格+B的6变体）；不是315×6联合搜索。阶段一真实数据实际构造的条件A/B有效配对={len(conditional_pairs)}个（另有NONE对照）。",
        "- 选择程序：三条准则均逐年先选A、再在当年A下选B；不做A×B全笛卡尔积。",
        f"- 安慰剂：{payload['placebos']}个随机事件世界，每次完整重算年度权重和A→B逐年选择。",
        "- 数据计算前放弃、从未测试：‘滚动历史命中率再次重加权’（会与A重复且需额外窗口）；‘直接用原始dLong计数’（会改写冻结事件定义）。二者测试次数均为0。",
        "- 阶段一候选结果暴露后未增加、删除或改写任何候选。",
        "",
        "## 3. 三准则探索结果与唯一封存方案",
        "",
        markdown_table(criteria_rows),
        "",
        f"阶段一主硬门：`{'通过' if discovery['pass'] else '未通过'}`；"
        f"2019-2022新信号cohort n={discovery['candidate_executed']}，"
        f"完整日历账本terminal delta={discovery['terminal_delta']:.6f}（含carry），"
        f"年度不输={discovery['non_loss_years']}/{discovery['year_slices']}，"
        f"placebo p={discovery['placebo']['empirical_p']:.6f}。",
        "",
        f"封存A：`{selected['a_weight_spec']['key']}`；封存B：`{selected['b_rule']['key']}`。",
        f"封存理由：{selected['status']}。",
        "",
        "## 4. 对封存期的事前预期",
        "",
        expectation,
        "",
        "> 本机文件时间与哈希只能证明本次工作区内的顺序，不是第三方可信时间戳。",
        "",
        SEALED_BEGIN,
        machine,
        SEALED_END,
        "",
    ])


def write_text_exclusive_atomic(path: Path, text_value: str) -> None:
    if path.exists():
        raise FileExistsError(f"封存目标已存在，拒绝覆盖: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text_value)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_sealed_payload(path: Path) -> dict[str, Any]:
    text_value = path.read_text(encoding="utf-8")
    start = text_value.find(SEALED_BEGIN)
    end = text_value.find(SEALED_END)
    if start < 0 or end < 0 or end <= start:
        raise ValueError("PREREG缺少机器封存块")
    raw = text_value[start + len(SEALED_BEGIN):end].strip()
    payload = json.loads(raw)
    expected = payload.get("seal_payload_sha256")
    actual = canonical_payload_hash(payload)
    if expected != actual:
        raise AssertionError("PREREG机器载荷hash不匹配")
    return payload


def deployment_schedules(
    engines: Mapping[str, sm.MarketEngine],
    a_spec: WeightSpec,
    b_spec: BSpec,
) -> tuple[dict[int, WeightSpec], dict[int, BSpec]]:
    first_year = min(int(engine.dates[0].year) for engine in engines.values())
    a_schedule: dict[int, WeightSpec] = {}
    b_schedule: dict[int, BSpec] = {}
    for year in range(first_year, WINDOW_END.year + 1):
        a_schedule[year] = a_spec if year >= 2023 else BASE
        b_schedule[year] = b_spec if year >= 2023 else NONE
    return a_schedule, b_schedule


def period_curve(
    trades: Sequence[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
    start: pd.Timestamp,
) -> pd.Series:
    return normalized_period_curve(trades, engines, start)


def confirmation_calendar_slices(
    candidate: Sequence[ResearchTrade],
    baseline: Sequence[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
) -> list[dict[str, Any]]:
    start = pd.Timestamp("2023-01-01")
    left = period_curve(candidate, engines, start)
    right = period_curve(baseline, engines, start)
    rows: list[dict[str, Any]] = []
    left_previous = right_previous = 0.0
    for year in range(2023, WINDOW_END.year + 1):
        days = left.index[left.index.year == year]
        if not len(days):
            continue
        last = days[-1]
        left_now, right_now = float(left.at[last]), float(right.at[last])
        left_increment = left_now - left_previous
        right_increment = right_now - right_previous
        rows.append({
            "year": year,
            "period": f"{year}-01-01~{last.date()}",
            "candidate_pnl_increment": left_increment,
            "baseline_pnl_increment": right_increment,
            "delta": left_increment - right_increment,
            "non_loss": left_increment >= right_increment - 1e-12,
        })
        left_previous, right_previous = left_now, right_now
    return attach_calendar_entry_counts(rows, candidate, baseline)


def confirmation_market_rows(
    candidate: Sequence[ResearchTrade],
    baseline: Sequence[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
) -> list[dict[str, Any]]:
    start = pd.Timestamp("2023-01-01")
    rows: list[dict[str, Any]] = []
    for code in ("AU", "AG", "TOTAL"):
        left = [t for t in candidate if code == "TOTAL" or t.market == code]
        right = [t for t in baseline if code == "TOTAL" or t.market == code]
        left_summary = trade_summary(left, start)
        right_summary = trade_summary(right, start)
        calendar_left, calendar_right, calendar_delta = period_terminal_delta(
            left, right, engines, start,
        )
        rows.append({
            "market": code,
            "period": f"2023-01-01~{WINDOW_END.date()}",
            "candidate_executed": left_summary["executed"],
            "candidate_closed": left_summary["closed"],
            "candidate_open": left_summary["open"],
            "candidate_terminal_sum": left_summary["terminal_sum"],
            "baseline_executed": right_summary["executed"],
            "baseline_closed": right_summary["closed"],
            "baseline_open": right_summary["open"],
            "baseline_terminal_sum": right_summary["terminal_sum"],
            "cohort_terminal_delta": (
                left_summary["terminal_sum"] - right_summary["terminal_sum"]
            ),
            "calendar_candidate_increment": calendar_left,
            "calendar_baseline_increment": calendar_right,
            "terminal_delta": calendar_delta,
            "sample_status": (
                "样本不足，只能作方向性参考，不足以支持上线"
                if left_summary["executed"] < 30 else "n>=30"
            ),
        })
    return rows


def goldman_diagnostics(
    engines: Mapping[str, sm.MarketEngine],
) -> list[dict[str, Any]]:
    cutoff = pd.Timestamp("2025-12-01")
    rows: list[dict[str, Any]] = []
    for code in ("AG", "AU"):
        engine = engines[code]
        fwd = sm.forward_returns(engine.cont, WEIGHT_HORIZON)
        dates = engine.ev_long.loc[
            engine.ev_long["member"].eq("高盛期货")
            & (engine.ev_long["trade_date"] < cutoff),
            "trade_date",
        ]
        values = fwd.reindex(pd.DatetimeIndex(dates)).dropna().to_numpy(float)
        mean_pp = float(values.mean() * 100.0)
        std = float(values.std(ddof=1))
        raw_t = float(values.mean() / std * math.sqrt(len(values)))
        rows.append({
            "market": code,
            "sample_cutoff": "event_date<2025-12-01",
            "horizon": 20,
            "n": int(len(values)),
            "mean_pct": mean_pp,
            "raw_t": raw_t,
            "current_clipped_weight": float(np.clip(raw_t, 0.0, 5.0)),
        })
    ag = next(row for row in rows if row["market"] == "AG")
    au = next(row for row in rows if row["market"] == "AU")
    if not (
        ag["n"] == 29 and np.isclose(ag["mean_pct"], 3.74, atol=0.03)
        and np.isclose(au["raw_t"], 6.56, atol=0.03)
    ):
        raise AssertionError(f"高盛诊断未对齐工单: AG={ag}, AU={au}")
    return rows


def confirmation_placebo_distribution(
    engines: Mapping[str, sm.MarketEngine],
    ratio: pd.Series,
    stage1_placebo: Mapping[str, Any],
    reps: int,
    seed: int,
) -> dict[str, Any]:
    """扩展阶段一随机世界；每次用其pre-2023完整选择后固定到封存期。"""
    if reps < 100:
        raise ValueError("安慰剂硬门要求至少100次")
    values: list[float] = []
    collision_rows: list[dict[str, Any]] = []
    base_schedule = fixed_schedule(engines, BASE)
    base_b = fixed_b_schedule(engines, NONE)
    start = pd.Timestamp("2023-01-01")
    fits = stage1_placebo["deployment_fits"]
    if len(fits) != reps:
        raise AssertionError("PREREG内阶段一随机世界部署拟合数量不符")
    for rep in range(reps):
        shifted_long: dict[str, pd.DataFrame] = {}
        shifted_short: dict[str, pd.DataFrame] = {}
        audit: dict[str, Any] = {"rep": rep + 1}
        for code in ("AU", "AG"):
            shifted_long[code], long_audit = shift_events_partitioned(
                engines[code].ev_long, engines[code].dates,
                event_stream_rng(seed, rep, code, "long"),
                boundary=start,
            )
            shifted_short[code], short_audit = shift_events_partitioned(
                engines[code].ev_short, engines[code].dates,
                event_stream_rng(seed, rep, code, "short"),
                boundary=start,
            )
            stage1_audit = stage1_placebo["collision_audit"][rep]
            long_prefix = shifted_long[code].loc[
                shifted_long[code]["trade_date"] < start
            ]
            short_prefix = shifted_short[code].loc[
                shifted_short[code]["trade_date"] < start
            ]
            long_hash = event_frame_sha256(long_prefix)
            short_hash = event_frame_sha256(short_prefix)
            if long_hash != stage1_audit[f"{code}_long"]["prefix_sha256"]:
                raise AssertionError(f"rep{rep + 1} {code} long随机前缀漂移")
            if short_hash != stage1_audit[f"{code}_short"]["prefix_sha256"]:
                raise AssertionError(f"rep{rep + 1} {code} short随机前缀漂移")
            long_audit["prefix_sha256"] = long_hash
            short_audit["prefix_sha256"] = short_hash
            audit[f"{code}_long"] = long_audit
            audit[f"{code}_short"] = short_audit
        factory = FeatureFactory(
            engines, strict_availability=True,
            events_by_code=shifted_long,
        )
        suppress = shifted_suppress_windows(
            engines["AG"], shifted_short["AG"], ratio,
        )
        fit = fits[rep][PRIMARY_CRITERION]
        null_a = weight_spec_from_dict(fit["a_first"]["picked"])
        null_b = b_spec_from_dict(fit["b_after_a"]["picked"])
        a_schedule, b_schedule = deployment_schedules(
            engines, null_a, null_b,
        )
        candidate = replay_schedule(
            factory, a_schedule, suppress, b_schedule,
        )
        baseline = replay_schedule(
            factory, base_schedule, suppress, base_b,
        )
        _, _, delta = period_terminal_delta(
            candidate, baseline, engines, start,
        )
        values.append(float(delta))
        collision_rows.append(audit)
        if (rep + 1) % 5 == 0:
            print(f"[confirm-placebo] {rep + 1}/{reps}", flush=True)
    return {
        "seed": seed,
        "reps": reps,
        "selection_policy": (
            "每个随机世界读取PREREG中由其2015-2022完整A→B选择得到的"
            "部署拟合，再固定应用2023+；封存期绝不重选，年度权重照常重算"
        ),
        "boundary_policy": "事件偏移不得跨2023-01-01封存边界",
        "values": values,
        "collision_audit": collision_rows,
    }


def research_ledger_mismatches(
    left: Sequence[ResearchTrade], right: Sequence[ResearchTrade],
) -> int:
    fields = (
        "market", "signal_date", "entry_date", "entry_px_real", "exit_date",
        "exit_px_real", "result", "ret_pct", "is_relay", "spec_key", "b_key",
    )
    if len(left) != len(right):
        return abs(len(left) - len(right)) + 1
    mismatches = 0
    for left_trade, right_trade in zip(left, right):
        if any(
            not _safe_compare(getattr(left_trade, field), getattr(right_trade, field))
            for field in fields
        ):
            mismatches += 1
    return mismatches


def factor_adjustment_audit(
    trades: Sequence[ResearchTrade],
    engines: Mapping[str, sm.MarketEngine],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in ("AU", "AG"):
        trade = next(
            row for row in trades
            if row.market == code and row.exit_date is not None
            and row.result == "消退卖出"
        )
        engine = engines[code]
        factor_last = float(engine.cont["factor"].iloc[-1])
        entry_factor = float(engine.cont.at[trade.entry_date, "factor"])
        exit_factor = float(engine.cont.at[trade.exit_date, "factor"])
        p0a = float(trade.entry_px_real) * factor_last / entry_factor
        p1a = float(trade.exit_px_real) * factor_last / exit_factor
        recomputed = (p1a / p0a - 1.0) * 100.0
        difference = recomputed - float(trade.ret_pct)
        if not np.isclose(difference, 0.0, atol=1e-9, rtol=0.0):
            raise AssertionError(f"{code}复权收益对拍失败: {difference}")
        rows.append({
            "market": code,
            "signal_date": str(trade.signal_date.date()),
            "entry_date": str(trade.entry_date.date()),
            "p0a_formula": "entry_px*f_last/f_entry",
            "engine_ret_pct": float(trade.ret_pct),
            "recomputed_ret_pct": recomputed,
            "difference": difference,
        })
    return rows


def full_file_manifest(data_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stem in (
        "au_price", "ag_price", "au_seat", "ag_seat",
        "reboard_visibility", "gold_silver_ratio",
    ):
        path = _pick_csv(data_dir, stem)
        rows.append({
            "file": path.name,
            "bytes": int(path.stat().st_size),
            "sha256": sha256_file(path),
        })
    return rows


def baseline_report_rows(
    trades: Sequence[ResearchTrade],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in ("AU", "AG", "TOTAL"):
        subset = [t for t in trades if code == "TOTAL" or t.market == code]
        full = trade_summary(subset)
        since = trade_summary(subset, pd.Timestamp("2019-01-01"))
        rows.append({
            "market": code,
            "full_period": f"2015-01-01~{WINDOW_END.date()}",
            "full_closed": full["closed"],
            "full_closed_sum": full["closed_sum"],
            "period_2019": f"2019-01-01~{WINDOW_END.date()}",
            "closed_2019": since["closed"],
            "closed_sum_2019": since["closed_sum"],
            "open_2019": since["open"],
            "terminal_sum_2019": since["terminal_sum"],
        })
    return rows


def _criterion_rows(stage1: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    search = stage1["search"]
    tests = stage1["placebo_tests"]
    for criterion in CRITERIA:
        item = search["criteria"][criterion]
        fit = item["deployment_fit"]
        calendar = item["calendar_oos"]
        rows.append({
            "criterion": CRITERION_LABELS[criterion],
            "role": "主准则" if criterion == PRIMARY_CRITERION else "对照",
            "period": "2019-2022",
            "n": item["oos_summary"]["executed"],
            "candidate_terminal": item["calendar_terminal_candidate"],
            "baseline_terminal": item["calendar_terminal_baseline"],
            "delta": item["terminal_delta"],
            "non_loss_years": int(sum(row["non_loss"] for row in calendar)),
            "year_slices": len(calendar),
            "deployment_A": fit["a_first"]["picked"]["key"],
            "deployment_B": fit["b_after_a"]["picked"]["key"],
            "placebo_q95": tests[criterion]["quantiles"]["95%"],
            "placebo_p": tests[criterion]["empirical_p"],
            "placebo_pass": tests[criterion]["pass"],
        })
    return rows


def _stage1_path_rows(stage1: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for criterion in CRITERIA:
        result = stage1["search"]["criteria"][criterion]
        a_path = result["selection_path"]["a_first"]
        b_path = result["selection_path"]["b_after_a"]
        year_metrics = {row["year"]: row for row in result["yearly_oos"]}
        calendar_metrics = {
            row["year"]: row for row in result["calendar_oos"]
        }
        for a_row, b_row in zip(a_path, b_path):
            year = int(a_row["year"])
            metrics = year_metrics[year]
            calendar = calendar_metrics[year]
            rows.append({
                "criterion": CRITERION_LABELS[criterion],
                "year": year,
                "train_period": a_row["train_period"],
                "A_train_n": a_row["train_n"],
                "A": a_row["picked"]["key"],
                "B_train_n": b_row["train_n"],
                "B": b_row["picked"]["key"],
                "oos_period": metrics["valuation_period"],
                "candidate_n_asof": metrics["candidate"]["executed_asof"],
                "baseline_n_asof": metrics["baseline"]["executed_asof"],
                "cohort_delta_asof": metrics["cohort_delta_asof"],
                "calendar_candidate_new_n": calendar["candidate_new_entries"],
                "calendar_baseline_new_n": calendar["baseline_new_entries"],
                "calendar_candidate_pnl": calendar["candidate_pnl_increment"],
                "calendar_baseline_pnl": calendar["baseline_pnl_increment"],
                "calendar_delta": calendar["delta"],
                "calendar_non_loss_gate": calendar["non_loss"],
                "sample_status": (
                    "样本不足，不采信（单年）"
                    if metrics["candidate"]["executed_asof"] < 30
                    else "单年样本达到30笔（仍只用于年度方向门）"
                ),
            })
    return rows


def _a_grid_rows(stage1: Mapping[str, Any]) -> list[dict[str, Any]]:
    result_map = {
        row["effective_key"]: row for row in stage1["search"]["effective_replays"]
    }
    rows: list[dict[str, Any]] = []
    for registered in stage1["search"]["registry"]:
        result = result_map[registered["effective_key"]]["oos_summary"]
        rows.append({
            "id": registered["registered_id"],
            "family": registered["label"],
            "min_n": registered["min_n"],
            "clip": registered["clip"],
            "k": registered["shrink_k"],
            "cap": registered["cap_n"],
            "effective_key": registered["effective_key"],
            "period": "2019-2022（固定参数过拟合参照）",
            "n": result["executed"],
            "closed": result["closed"],
            "terminal_sum": result["terminal_sum"],
        })
    return rows


def _b_result_rows(stage1: Mapping[str, Any]) -> list[dict[str, Any]]:
    registry = {
        row["key"]: row for row in stage1["search"]["b_registry"]
    }
    audits = stage1["search"]["candidate_scores"][PRIMARY_CRITERION]["b"]
    deployment = [row for row in audits if row["decision"] == "b_deployment_fit"]
    fixed = {
        row["b"]["key"]: row
        for row in stage1["search"]["criteria"][PRIMARY_CRITERION][
            "b_deployment_fixed_results"
        ]
    }
    picked_years: dict[str, int] = {key: 0 for key in registry}
    schedule = stage1["search"]["criteria"][PRIMARY_CRITERION]["schedule"]
    for year in range(2019, 2023):
        key = schedule[str(year)]["b"]["key"]
        picked_years[key] += 1
    rows: list[dict[str, Any]] = []
    for audit in deployment:
        info = registry[audit["b_key"]]
        ledger = fixed[audit["b_key"]]
        rows.append({
            "candidate": audit["b_key"],
            "registered": info["registered_candidate"],
            "K": info["min_members"],
            "L": info["lookback_trading_days"],
            "economic_reason": info["economic_rationale"],
            "period": "2015-2022 deployment fit",
            "train_n": audit["train_n"],
            "mean_minus_se_score": audit["train_score"],
            "selected_WF_years": picked_years[audit["b_key"]],
            "ledger_period": ledger["period"],
            "ledger_n": ledger["executed"],
            "ledger_terminal_sum": ledger["terminal_sum"],
            "vs_NONE_delta": ledger["vs_none_terminal_delta"],
        })
    return rows


def render_report(
    sealed: Mapping[str, Any], confirm: Mapping[str, Any], prereg_path: Path,
) -> str:
    stage1 = sealed["stage1"]
    discovery = stage1["discovery_gate"]
    total = next(row for row in confirm["confirmation_rows"] if row["market"] == "TOTAL")
    placebo = confirm["placebo_test"]
    if not discovery["pass"]:
        verdict_code = "③"
        verdict_text = "阶段一就没找到"
    elif (
        total["candidate_executed"] >= 30
        and total["terminal_delta"] > 0
        and placebo["pass"]
    ):
        verdict_code = "①"
        verdict_text = "封存期确认有效"
    else:
        verdict_code = "②"
        verdict_text = "阶段一有效但封存期没撑住"
    sample_warning = (
        "样本不足，只能作方向性参考，不足以支持上线"
        if total["candidate_executed"] < 30 else "封存期执行笔数达到30笔硬门"
    )
    baseline_ok = confirm["baseline_assertions"]["passed"]
    stage1_test = discovery["placebo"]
    seal_a = sealed["sealed_deployment"]["a_weight_spec"]["key"]
    seal_b = sealed["sealed_deployment"]["b_rule"]["key"]
    stage1_path_rows = _stage1_path_rows(stage1)
    single_year_status = (
        "所有单年候选样本均小于30，只用于年度方向门，"
        "均标‘样本不足，不采信’。"
        if all(row["candidate_n_asof"] < 30 for row in stage1_path_rows)
        else "单年样本状态已逐行按候选执行笔数动态标注；单年切片仅用于年度方向门。"
    )
    lines = [
        "# 权重机制重定 + 一条新规则：封存确认报告 v1",
        "",
        f"**结论：{verdict_code}{verdict_text}。**",
        "",
        f"阶段一主准则2019-2022：新信号cohort n={discovery['candidate_executed']}，完整日历账本delta={discovery['terminal_delta']:.3f}个百分点（含跨边界carry），年度不输{discovery['non_loss_years']}/{discovery['year_slices']}，安慰剂p={stage1_test['empirical_p']:.4f}。封存A=`{seal_a}`、B=`{seal_b}`。",
        f"阶段三2023-01-01~2026-08-13：新信号cohort n={total['candidate_executed']}（closed={total['candidate_closed']}, open={total['candidate_open']}），完整日历账本delta={total['terminal_delta']:.3f}个百分点，安慰剂p={placebo['empirical_p']:.4f}；{sample_warning}。",
        "",
        "> 盲性限制：本次脚本确实在PREREG写入后才首次机械读取/计算封存期，但同一会话此前的另一工单已向主研究者暴露过2023-2026的部分汇总。因此这里是代码与参数意义上的一次封存确认，不是研究者认知意义上的纯盲试验；若出现阳性，不能单凭本报告直接上线，仍需未来新样本。阴性结论不因该限制而变强行转阳。",
        "",
        "## 1. 封存顺序与输入",
        "",
        f"PREREG本机mtime：`{confirm['prereg_mtime']}`；确认锁创建：`{confirm['confirmation_started_at']}`；完成：`{confirm['confirmation_completed_at']}`。PREREG SHA-256=`{confirm['prereg_sha256']}`。本机mtime/hash不是第三方可信时间戳。",
        "",
        markdown_table(confirm["file_manifest"]),
        "",
        "阶段一读取器会机械解析首个2023边界记录用于硬停，visibility还会解析R作为可见日输入门；这些越界行不进入DataFrame、manifest、特征或统计，但这不等于物理字节零读取。该隔离依赖源CSV全局日期升序；运行时前缀单调已验证，导出SQL本身未提供ORDER BY，无法在不扫描封存期的前提下独立证明尾部不会回落。阶段三先以独占模式创建本报告锁，再读取完整数据；崩溃会消耗一次确认机会。",
        "",
        "## 2. 七席位唯一基准复现",
        "",
        markdown_table(confirm["baseline_rows"]),
        "",
        f"生产口径状态机错配={confirm['production_parity']['mismatch_count']}；严格fwd可得日BASE与engine账本错配={confirm['strict_baseline_parity']['mismatch_count']}；硬断言 `+281.0%/125笔` 与 `+268.4%/86笔`：{'通过' if baseline_ok else '失败'}。收益均为逐笔算术和。",
        "",
        "## 3. 两个权重诊断的独立复核",
        "",
        markdown_table(confirm["goldman_diagnostics"]),
        "",
        "诊断只说明现行阶跃/截断确会改写展示权重；依工单纪律，是否改规则只看完整账本。",
        "",
        "## 4. A部分：315格预注册与实测对照",
        "",
        "注册数=315；有效唯一实现=225；W4因连续替代hard min_n而有90个标签结构重复。下表全列，固定参数数字只作探索期过拟合参照，不作为建议。",
        "",
        markdown_table(_a_grid_rows(stage1)),
        "",
        "## 5. B部分：经济理由在结果之前固定",
        "",
        "唯一规则族是‘独立席位确认’：当前正权重席位在最近L个交易日内至少K个出现既有有效增多。它只过滤入场来源广度；不使用任何已证伪的减多、增空、峰值回落、追踪、警报直出、翻空、OI或离场动作。",
        "",
        markdown_table(_b_result_rows(stage1)),
        "",
        "注册候选总数=321（A的315格+B的6个变体）；A→B为条件选择，不是315×6联合搜索。NONE仅作现行比较。未测试的两个设计草案（滚动命中率再次重加权、直接raw dLong计数）均在计算前放弃，测试次数0。候选暴露后未增加维度。",
        "",
        "## 6. 阶段一逐年walk-forward与三准则",
        "",
        markdown_table(_criterion_rows(stage1)),
        "",
        markdown_table(stage1_path_rows),
        "",
        "Y年A先由<Y账本选择，随后B在该A下仍只用<Y账本选择；"
        "2015-2018是初始训练窗与状态热身，不进入OOS汇总但参与后续<Y训练。"
        "未退出交易统一按决策日前末收盯市。" + single_year_status +
        "主准则预先固定为(b)，没有从三条OOS路径事后挑最好者。",
        "",
        "## 7. 阶段一安慰剂",
        "",
        markdown_table([
            {
                "criterion": row["criterion"], "period": row["period"],
                "n": row["n"], "real_delta": row["delta"],
                "q95": row["placebo_q95"], "empirical_p": row["placebo_p"],
                "pass": row["placebo_pass"],
            } for row in _criterion_rows(stage1)
        ]),
        "",
        markdown_table([
            {
                "criterion": CRITERION_LABELS[criterion],
                "period": "2019-2022", "reps": stage1["placebo_tests"][criterion]["reps"],
                **stage1["placebo_tests"][criterion]["quantiles"],
            }
            for criterion in CRITERIA
        ]),
        "",
        "每个随机世界逐事件在本市场交易日历偏移±5~10日、同member/date碰撞保留strength最大者，完整重算年度权重、A选择、B选择，并与同一随机世界的现行BASE比较。",
        "",
        "## 8. 阶段二唯一封存方案",
        "",
        f"A=`{seal_a}`；B=`{seal_b}`；状态：{sealed['sealed_deployment']['status']}。PREREG载荷hash验证通过，确认期未改参数。",
        "",
        "## 9. 阶段三一次确认",
        "",
        markdown_table(confirm["confirmation_rows"]),
        "",
        markdown_table(confirm["confirmation_calendar"]),
        "",
        f"阶段三封存检验安慰剂n={placebo['reps']}：real={placebo['real_terminal_delta']:.6f}，q95={placebo['quantiles']['95%']:.6f}，经验p={placebo['empirical_p']:.6f}，硬门={'通过' if placebo['pass'] else '未通过'}。每个随机世界使用PREREG中由其2015-2022完整A→B选择得到的部署拟合，再固定应用2023+并逐年重算权重；绝不使用封存期重选。",
        "",
        markdown_table([{
            "period": "2023-01-01~2026-08-13", "reps": placebo["reps"],
            **placebo["quantiles"],
        }]),
        "",
        "## 10. reboard双跑与反时间幻觉八项",
        "",
        markdown_table([
            {"arm": "stage1", **stage1["reboard_equivalence"]},
            {"arm": "stage3", **confirm["reboard_equivalence"]},
        ]),
        "",
        markdown_table(confirm["factor_adjustment_audit"]),
        "",
        "- [x] 席位T日盘后确认；所有实际进场严格晚于signal_date，最早T+1。",
        "- [x] 事件阈值、dist60、netq、计分窗只向后滚动；未使用决策日之后的数据。",
        "- [x] 年度权重逐年重算，事件仍严格 `<上年12-01`；另要求fwd20收益终点在1月1日前已实现。",
        "- [x] 阶段一文件级硬截断至2022-12-31；没有构造2023+ DataFrame/特征/统计。",
        "- [x] reboard_visibility逐行验证D<R；严格输入与archive输入双臂均交给只读引擎，结构性0差异，未绕过clean_seat。",
        "- [x] `Trade.entry_px`先按 `entry_px*f_last/f_entry` 转为复权尺度；上表逐市场与引擎收益对拍0差。",
        "- [x] 金银比只读 `research/data/gold_silver_ratio.csv` 并原样调用引擎窗口函数；未联网、未自建映射。",
        "- [x] 2023封存边界不可被安慰剂事件跨越；确认参数只来自PREREG。",
        "",
        "## 11. 验收清单",
        "",
        f"- [{'x' if baseline_ok else ' '}] 基准+281.0%/125笔、2019后+268.4%/86笔。",
        "- [x] 高盛白银n=29/均值约3.74%，高盛黄金真实t约6.56。",
        "- [x] A部分315格全列；225个有效实现及90个W4结构重复如实披露。",
        "- [x] B部分6个候选的事前经济理由、参数与结果全列。",
        "- [x] 阶段一试过/计算前放弃的全部东西及次数已披露。",
        "- [x] PREREG本机mtime早于确认锁；hash与脚本hash均验证。",
        f"- [x] 封存期实际n={total['candidate_executed']}；{sample_warning}。",
        f"- [x] 阶段一和阶段三安慰剂均为n={sealed['placebos']}。",
        "- [x] 八项反时间幻觉纪律逐项自证。",
        "- [x] 本研究脚本未修改engine、未连接生产库、未使用外部数据；新增产物仅在research。",
        "",
        "完整复现命令（从不存在PREREG/REPORT的干净研究副本运行）：",
        "",
        f"`\"{sys.executable}\" research/run_weightspec.py --stage all --seed {sealed['seed']} --placebos {sealed['placebos']}`",
        "",
    ]
    return "\n".join(lines)


def _base_payload(
    inputs: BoundedInputs,
    suppress_windows: Sequence[tuple[pd.Timestamp, pd.Timestamp]],
    specs: Sequence[WeightSpec],
) -> dict[str, Any]:
    return {
        "schema_version": "weightspec-stage1-a/v1",
        "stage": "stage1_a",
        "window": {
            "input_history": "文件起点~2022-12-31（pre-2015 仅作 burn-in）",
            "replay_start": str(WINDOW_START.date()),
            "research_end": str(WINDOW_END.date()),
            "confirmation_period_included": False,
            "initial_training_and_state_warmup": "2015-01-01~2018-12-31",
            "walk_forward_oos": "2019-01-01~2022-12-31",
            "training_rule": "Y 年只使用 entry_date<Y-01-01 的全账本结果",
            "min_train": MIN_TRAIN,
            "initial_window_excluded_from_oos_but_included_in_later_training": True,
        },
        "guardrails": {
            "source_global_date_sort_required_runtime_contract": True,
            "source_global_date_sort_not_independently_provable_without_reading_holdout": True,
            "input_prefix_monotonic_verified": True,
            "input_reader_stops_at_first_2023_boundary_record": True,
            "input_upper_bound_before_engine": True,
            "derived_date_audit_before_features": True,
            "strict_fwd20_availability_before_decision": True,
            "post_2022_read_by_feature_or_statistics": False,
            "date_override_supported": False,
        },
        "frozen_rules": FROZEN_RULES,
        "grid": grid_metadata(specs),
        "inputs": inputs.manifest,
        "suppression_windows": [
            {"start": str(start.date()), "end": str(end.date())}
            for start, end in suppress_windows
        ],
    }


def _json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_clean(item) for item in value]
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(pd.Timestamp(value))
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        numeric = float(value)
        return numeric if np.isfinite(numeric) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, Path):
        return str(value)
    return value


def run_explore(args: argparse.Namespace) -> int:
    """阶段一只读2022前缀，完成后独占写PREREG并立即返回。"""
    configure_runtime_end(pd.Timestamp("2022-12-31"))
    if PREREG_PATH.exists() or REPORT_PATH.exists():
        raise FileExistsError("PREREG或REPORT已存在；拒绝覆盖/重跑探索")
    specs = stage1_a_grid()
    inputs = load_bounded_inputs(args.data_dir.resolve(), args.chunk_size)
    visibility = validate_reboard_visibility(inputs)
    strict_engines = build_bounded_engines(
        raw_for_visibility_mode(inputs, "strict_pit"),
    )
    archive_engines = build_bounded_engines(
        raw_for_visibility_mode(inputs, "archive_visible"),
    )
    equivalence = visibility_engine_equivalence(strict_engines, archive_engines)
    strict_suppress = sm.flee_suppress_windows(strict_engines["AG"], inputs.ratio)
    archive_suppress = sm.flee_suppress_windows(archive_engines["AG"], inputs.ratio)
    if strict_suppress != archive_suppress:
        raise AssertionError("reboard双臂压制窗口不一致")
    parity, _ = run_baseline_parity(archive_engines, archive_suppress)
    if not parity["passed"]:
        raise RuntimeError(f"阶段一基准逐笔对拍失败: {parity}")
    strict_parity = strict_baseline_parity(strict_engines, strict_suppress)
    if not strict_parity["passed"]:
        raise RuntimeError(
            f"阶段一严格时点BASE与生产账本不一致: {strict_parity}"
        )

    base = _base_payload(inputs, strict_suppress, specs)
    print("[explore] 315注册格/225有效实现 + 6个B变体", flush=True)
    search = run_stage1_a(strict_engines, strict_suppress, specs)
    print(f"[explore] {args.placebos}次嵌套安慰剂", flush=True)
    placebo = stage1_placebo_distribution(
        strict_engines, inputs.ratio, specs, args.placebos, args.seed,
    )
    tests = {
        criterion: placebo_test(
            placebo["values"][criterion],
            float(search["criteria"][criterion]["terminal_delta"]),
        )
        for criterion in CRITERIA
    }
    stage1 = {
        **base,
        "baseline_parity": parity,
        "strict_baseline_parity": strict_parity,
        "reboard_visibility": visibility,
        "reboard_equivalence": equivalence,
        "search": search,
        "placebo": placebo,
        "placebo_tests": tests,
    }
    discovery = stage1_discovery_gate(search, placebo)
    stage1["discovery_gate"] = discovery
    sealed = selected_deployment(search, discovery)
    payload: dict[str, Any] = {
        "schema_version": "weightspec-seal/v1",
        "sealed_at": now_shanghai(),
        "seed": int(args.seed),
        "placebos": int(args.placebos),
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "engine_sha256": sha256_file(ROOT / "engine" / "smart_money.py"),
        "blindness_disclosure": (
            "封存前没有把2023+记录纳入DataFrame、manifest、特征或统计；"
            "读取器会机械解析首个边界记录用于硬停，visibility会解析R作输入门，"
            "这不是物理字节零读取。主研究者在同一会话此前工单已见过该区间部分汇总，"
            "因此不是认知意义的纯盲试验。"
        ),
        "stage1": stage1,
        "sealed_deployment": sealed,
    }
    payload["seal_payload_sha256"] = canonical_payload_hash(payload)
    write_text_exclusive_atomic(PREREG_PATH, render_prereg(payload))
    print(
        f"[sealed] {PREREG_PATH} A={sealed['a_weight_spec']['key']} "
        f"B={sealed['b_rule']['key']} gate={discovery['pass']}",
        flush=True,
    )
    return 0


def assert_full_input_dates(inputs: BoundedInputs) -> None:
    expected = {
        "au_price": "2026-08-13", "ag_price": "2026-08-13",
        "au_seat": "2026-08-13", "ag_seat": "2026-08-13",
        "gold_silver_ratio": "2026-08-13",
        "reboard_visibility": "2026-08-12",
    }
    actual = {
        key: value["last_date"] for key, value in inputs.manifest.items()
    }
    mismatches = {
        key: (expected_value, actual.get(key))
        for key, expected_value in expected.items()
        if actual.get(key) != expected_value
    }
    if mismatches:
        raise AssertionError(f"完整数据截止日不符: {mismatches}")


def stage1_prefix_manifest_from_full(
    inputs: BoundedInputs, data_dir: Path,
) -> dict[str, Any]:
    cutoff = pd.Timestamp("2023-01-01")
    manifest: dict[str, Any] = {}
    visibility = inputs.visibility.loc[
        (inputs.visibility["trade_date"] < cutoff)
        & (inputs.visibility["reboard_date"] < cutoff)
    ].copy()
    visible_keys = {
        (
            str(row.instrument), str(row.contract), str(row.rank_type),
            str(row.member), pd.Timestamp(row.trade_date).strftime("%Y-%m-%d"),
            str(row.source),
        )
        for row in visibility.itertuples(index=False)
    }
    for code in ("AU", "AG"):
        price, seat = inputs.raw[code]
        prefix = code.lower()
        seat_prefix = seat.loc[seat["trade_date"] < cutoff].copy()
        if len(seat_prefix):
            inferred = seat_prefix["source"].eq("reboard_inferred")
            inferred_rows = seat_prefix.loc[
                inferred, list(REBOARD_KEYS)
            ]
            keys = pd.Series([
                tuple(
                    pd.Timestamp(value).strftime("%Y-%m-%d")
                    if column == "trade_date" else str(value)
                    for column, value in zip(REBOARD_KEYS, row)
                )
                for row in inferred_rows.itertuples(
                    index=False, name=None,
                )
            ], index=inferred_rows.index)
            keep = ~inferred
            keep.loc[inferred] = keys.isin(visible_keys)
            seat_prefix = seat_prefix.loc[keep].copy()
        manifest[f"{prefix}_price"] = _manifest_row(
            _pick_csv(data_dir, f"{prefix}_price"),
            price.loc[price["trade_date"] < cutoff].copy(), "trade_date",
        )
        manifest[f"{prefix}_seat"] = _manifest_row(
            _pick_csv(data_dir, f"{prefix}_seat"),
            seat_prefix, "trade_date",
        )
    ratio_frame = inputs.ratio.rename("ratio").reset_index()
    ratio_frame.columns = ["date", "ratio"]
    ratio_frame = ratio_frame.loc[ratio_frame["date"] < cutoff].copy()
    manifest["gold_silver_ratio"] = _manifest_row(
        _pick_csv(data_dir, "gold_silver_ratio"), ratio_frame, "date",
    )
    manifest["reboard_visibility"] = _manifest_row(
        _pick_csv(data_dir, "reboard_visibility"), visibility, "trade_date",
    )
    return manifest


def run_confirm(args: argparse.Namespace) -> int:
    """验证封存后先占用一次确认锁，再允许读取2023+。"""
    configure_runtime_end(pd.Timestamp("2022-12-31"))
    if not PREREG_PATH.exists():
        raise FileNotFoundError("缺少PREREG；必须先完成explore")
    if REPORT_PATH.exists():
        raise FileExistsError("REPORT已存在；确认机会已消费，拒绝重跑")
    sealed = read_sealed_payload(PREREG_PATH)
    current_code_hash = sha256_file(Path(__file__).resolve())
    if sealed["script_sha256"] != current_code_hash:
        raise AssertionError("PREREG后脚本发生变化，拒绝确认")
    if sealed["engine_sha256"] != sha256_file(ROOT / "engine" / "smart_money.py"):
        raise AssertionError("PREREG后engine/smart_money.py发生变化，拒绝确认")
    if int(sealed["seed"]) != args.seed or int(sealed["placebos"]) != args.placebos:
        raise AssertionError("确认seed/placebos必须与PREREG一致")
    prereg_hash = sha256_file(PREREG_PATH)
    prereg_mtime_ns = PREREG_PATH.stat().st_mtime_ns
    prereg_mtime = datetime.fromtimestamp(
        PREREG_PATH.stat().st_mtime, ZoneInfo("Asia/Shanghai"),
    ).isoformat(timespec="seconds")
    started = now_shanghai()
    with REPORT_PATH.open("x", encoding="utf-8", newline="\n") as lock:
        lock.write(
            f"<!-- CONFIRMATION_LOCK started={started} "
            f"prereg_sha256={prereg_hash} -->\n"
        )
        lock.flush()
        os.fsync(lock.fileno())
    report_lock_mtime_ns = REPORT_PATH.stat().st_mtime_ns
    if prereg_mtime_ns >= report_lock_mtime_ns:
        raise AssertionError("PREREG文件时间未严格早于确认锁")

    # 从这里开始任何失败都保留REPORT锁，确认不得重跑。
    configure_runtime_end(CONFIRM_END)
    inputs = load_bounded_inputs(args.data_dir.resolve(), args.chunk_size)
    assert_full_input_dates(inputs)
    reconstructed_prefix = stage1_prefix_manifest_from_full(
        inputs, args.data_dir.resolve(),
    )
    if reconstructed_prefix != sealed["stage1"]["inputs"]:
        raise AssertionError("确认时重建的2022前缀hash与PREREG不一致")
    visibility = validate_reboard_visibility(inputs)
    strict_engines = build_bounded_engines(
        raw_for_visibility_mode(inputs, "strict_pit"),
    )
    archive_engines = build_bounded_engines(
        raw_for_visibility_mode(inputs, "archive_visible"),
    )
    equivalence = visibility_engine_equivalence(strict_engines, archive_engines)
    strict_suppress = sm.flee_suppress_windows(strict_engines["AG"], inputs.ratio)
    archive_suppress = sm.flee_suppress_windows(archive_engines["AG"], inputs.ratio)
    if strict_suppress != archive_suppress:
        raise AssertionError("完整reboard双臂压制窗口不一致")

    production_parity, production_trades = run_baseline_parity(
        archive_engines, archive_suppress,
    )
    if not production_parity["passed"]:
        raise AssertionError(f"完整生产基准逐笔对拍失败: {production_parity}")
    strict_parity = strict_baseline_parity(strict_engines, strict_suppress)
    if not strict_parity["passed"]:
        raise AssertionError(
            f"完整严格时点BASE与生产账本不一致: {strict_parity}"
        )
    baseline_rows = baseline_report_rows(production_trades)
    total_base = next(row for row in baseline_rows if row["market"] == "TOTAL")
    baseline_pass = bool(
        total_base["full_closed"] == 125
        and np.isclose(total_base["full_closed_sum"], 280.9570680000034,
                       atol=1e-6, rtol=0.0)
        and total_base["closed_2019"] == 86
        and np.isclose(total_base["closed_sum_2019"], 268.3780177686515,
                       atol=1e-6, rtol=0.0)
    )
    if not baseline_pass:
        raise AssertionError(f"工单基准未对齐: {total_base}")

    a_spec = weight_spec_from_dict(
        sealed["sealed_deployment"]["a_weight_spec"],
    )
    b_spec = b_spec_from_dict(sealed["sealed_deployment"]["b_rule"])
    a_schedule, b_schedule = deployment_schedules(
        strict_engines, a_spec, b_spec,
    )
    strict_factory = FeatureFactory(
        strict_engines, strict_availability=True,
    )
    candidate = replay_schedule(
        strict_factory, a_schedule, strict_suppress, b_schedule,
    )
    strict_baseline = replay_schedule(
        strict_factory, fixed_schedule(strict_engines, BASE), strict_suppress,
        fixed_b_schedule(strict_engines, NONE),
    )
    archive_factory = FeatureFactory(
        archive_engines, strict_availability=True,
    )
    archive_a_schedule, archive_b_schedule = deployment_schedules(
        archive_engines, a_spec, b_spec,
    )
    archive_candidate = replay_schedule(
        archive_factory, archive_a_schedule, archive_suppress,
        archive_b_schedule,
    )
    ledger_mismatches = research_ledger_mismatches(candidate, archive_candidate)
    if ledger_mismatches:
        raise AssertionError(f"确认候选reboard双跑错配={ledger_mismatches}")
    confirmation_rows = confirmation_market_rows(
        candidate, strict_baseline, strict_engines,
    )
    confirmation_calendar = confirmation_calendar_slices(
        candidate, strict_baseline, strict_engines,
    )
    total_confirm = next(row for row in confirmation_rows if row["market"] == "TOTAL")

    print(f"[confirm] {args.placebos}次固定封存方案安慰剂", flush=True)
    confirm_placebo = confirmation_placebo_distribution(
        strict_engines, inputs.ratio, sealed["stage1"]["placebo"],
        args.placebos, args.seed,
    )
    confirm_test = placebo_test(
        confirm_placebo["values"], float(total_confirm["terminal_delta"]),
    )
    lag_violations = sum(
        trade.entry_date is not None and trade.entry_date <= trade.signal_date
        for trade in candidate
    )
    if lag_violations:
        raise AssertionError(f"发现{lag_violations}笔非T+1执行")
    completed = now_shanghai()
    confirm: dict[str, Any] = {
        "confirmation_started_at": started,
        "confirmation_completed_at": completed,
        "prereg_mtime": prereg_mtime,
        "prereg_mtime_ns": prereg_mtime_ns,
        "report_lock_mtime_ns": report_lock_mtime_ns,
        "prereg_sha256": prereg_hash,
        "file_manifest": full_file_manifest(args.data_dir.resolve()),
        "bounded_manifest": inputs.manifest,
        "reboard_visibility": visibility,
        "reboard_equivalence": {
            **equivalence,
            "candidate_ledger_mismatch_count": ledger_mismatches,
        },
        "production_parity": production_parity,
        "strict_baseline_parity": strict_parity,
        "baseline_rows": baseline_rows,
        "baseline_assertions": {"passed": baseline_pass},
        "goldman_diagnostics": goldman_diagnostics(archive_engines),
        "confirmation_rows": confirmation_rows,
        "confirmation_calendar": confirmation_calendar,
        "placebo": confirm_placebo,
        "placebo_test": confirm_test,
        "factor_adjustment_audit": factor_adjustment_audit(
            production_trades, archive_engines,
        ),
        "t_plus_one_violations": int(lag_violations),
    }
    report = render_report(sealed, confirm, PREREG_PATH)
    with REPORT_PATH.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(report)
        stream.flush()
        os.fsync(stream.fileno())
    print(f"[confirmed] {REPORT_PATH}", flush=True)
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="七席位权重机制探索、封存与一次确认",
    )
    parser.add_argument(
        "--stage", choices=("check-grid", "explore", "confirm", "all"),
        required=True,
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--placebos", type=int, default=DEFAULT_PLACEBOS)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--chunk-size", type=int, default=200_000)
    args = parser.parse_args(argv)
    if args.placebos < 100:
        parser.error("--placebos 必须>=100")
    if args.chunk_size <= 0:
        parser.error("--chunk-size 必须为正整数")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    assert_frozen_rules()
    if args.stage == "check-grid":
        print(json.dumps(grid_metadata(stage1_a_grid()), ensure_ascii=False, indent=2))
        return 0
    if args.stage == "explore":
        return run_explore(args)
    if args.stage == "confirm":
        return run_confirm(args)
    # all用两个独立子进程；explore子进程写完PREREG即停止，再启动confirm。
    common = [
        "--seed", str(args.seed), "--placebos", str(args.placebos),
        "--data-dir", str(args.data_dir.resolve()),
        "--chunk-size", str(args.chunk_size),
    ]
    subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--stage", "explore", *common],
        cwd=ROOT, check=True,
    )
    subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--stage", "confirm", *common],
        cwd=ROOT, check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
