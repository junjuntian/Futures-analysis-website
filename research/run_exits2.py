# -*- coding: utf-8 -*-
"""机构资金离场规则预注册搜索（AU/AG，冻结生产引擎进场账本）。

边界：
- 只读调用 ``engine/smart_money.py`` 生成进场账本；不修改进场或生产代码。
- 所有离场候选逐笔重放同一份账本，候选离场绝不反向改变中继进场。
- 主结论使用 reboard 可见日门控的严格点时口径；最终可见（archive-inclusive）
  仅作诊断双跑。
- 预注册网格在常量中锁死，并用断言核对 645 个候选规格。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from itertools import combinations, product
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESEARCH = Path(__file__).resolve().parent
DATA = RESEARCH / "data"
REPORT = RESEARCH / "REPORT_EXITS_v2.md"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import smart_money as sm  # noqa: E402 只读调用冻结引擎


SEED = 20260814
MIN_TRAIN = 30
MIN_OOS = 30
TIE_EPS = 0.05  # 训练保守分相差不足 0.05pct，取更简单/更靠近基线者
PLACEBO_REPS = 200
REPORT_END = pd.Timestamp("2026-08-13")
END_EXCLUSIVE = REPORT_END + pd.Timedelta(days=1)
GROUP = tuple(sm.RULES["group8"])
SPREAD_SEATS = frozenset(sm.RULES["spread_seats"])
ALIAS = sm.RULES["alias"]
PIT_STRICT = "strict_pit"
PIT_INCLUSIVE = "archive_inclusive"


# ------------------------------ 预注册网格：禁止运行时扩维
A_N = (5, 8, 12, 15, 20)
B_PHI = (0.5, 1.0, 2.0)
B_N = (5, 10, 15)
C_K = (2, 3)
C_M = (5, 10)
D_X = (0.25, 0.40)
E_MODE = ("hot_day", "hot_plus_40d")
F_K = (2, 3)
F_M = (5, 10)
G_X = (0.04, 0.06, 0.08)
J_STOP = (0.03, 0.04, 0.05)
J_BE = (None, 0.03, 0.05)


@dataclass(frozen=True, order=True)
class Component:
    family: str
    params: tuple[tuple[str, Any], ...]

    @classmethod
    def make(cls, family: str, **params: Any) -> "Component":
        return cls(family, tuple(sorted(params.items())))

    def get(self, key: str) -> Any:
        return dict(self.params)[key]

    @property
    def key(self) -> str:
        bits = ",".join(f"{k}={v}" for k, v in self.params)
        return f"{self.family}({bits})"


@dataclass(frozen=True)
class RuleSpec:
    template: str
    components: tuple[Component, ...]

    @property
    def key(self) -> str:
        return "+".join(c.key for c in self.components)

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(c.family for c in self.components)


@dataclass
class Market:
    code: str
    engine: sm.MarketEngine
    dates: pd.DatetimeIndex
    pos: dict[pd.Timestamp, int]
    entries: pd.DataFrame
    reduce_long: pd.DataFrame
    increase_long: pd.DataFrame
    increase_short: pd.DataFrame
    net_inclusive: np.ndarray
    net_strict_peak60: np.ndarray
    net_strict_current: np.ndarray


def family_grid() -> dict[str, list[Component]]:
    grids = {
        "A": [Component.make("A", n=n) for n in A_N],
        "B": [Component.make("B", phi=p, n=n) for p, n in product(B_PHI, B_N)],
        "C": [Component.make("C", k=k, m=m) for k, m in product(C_K, C_M)],
        "D": [Component.make("D", x=x) for x in D_X],
        "E": [Component.make("E", mode=mode) for mode in E_MODE],
        "F": [Component.make("F", k=k, m=m) for k, m in product(F_K, F_M)],
        "G": [Component.make("G", x=x) for x in G_X],
        "J": [Component.make("J", stop=s, be=be) for s, be in product(J_STOP, J_BE)],
        # H 是预注册的“现行10日消退 + 追踪”两族特例，已经占满两族，不能再叠加。
        "H": [Component.make("H", x=x) for x in G_X],
    }
    expected = {"A": 5, "B": 9, "C": 4, "D": 2, "E": 2,
                "F": 4, "G": 3, "H": 3, "J": 9}
    assert {k: len(v) for k, v in grids.items()} == expected
    return grids


def enumerate_specs() -> tuple[dict[str, list[RuleSpec]], list[RuleSpec]]:
    grids = family_grid()
    composable = ("A", "B", "C", "D", "E", "F", "G", "J")
    templates: dict[str, list[RuleSpec]] = {}
    for fam in composable:
        templates[fam] = [RuleSpec(fam, (c,)) for c in grids[fam]]
    templates["H"] = [RuleSpec("H", (c,)) for c in grids["H"]]
    for left, right in combinations(composable, 2):
        name = f"{left}+{right}"
        templates[name] = [RuleSpec(name, (a, b)) for a, b in product(grids[left], grids[right])]
    specs = [s for values in templates.values() for s in values]
    # 38 个可组合单族 + 604 个两族笛卡尔积 + H 三档 = 645。
    assert len(specs) == 645, len(specs)
    assert len({s.key for s in specs}) == 645
    assert len(templates) == 37
    return templates, specs


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_ratio() -> pd.Series:
    """只读生产缓存；禁止调用会联网并覆写缓存的 sm.load_ratio()."""
    df = pd.read_csv(DATA / "gold_silver_ratio.csv", parse_dates=["date"])
    if df["date"].duplicated().any() or df["ratio"].isna().any():
        raise ValueError("gold_silver_ratio.csv 日期重复或 ratio 缺失")
    return df.set_index("date")["ratio"].sort_index()


def strict_flow_events(engine: sm.MarketEngine, direction: str) -> pd.DataFrame:
    """新离场特征使用的双腿可见净流事件。

    冻结进场继续原样调用生产引擎；C/F 是新特征，不能继承 ``member_day``
    把缺失腿填0的口径。只有 dlong、dshort 同时可见才计算 dnet。
    """
    out: list[pd.DataFrame] = []
    oi = engine.cont["oi_total"]
    for member in GROUP:
        s = engine.md[engine.md["member"] == member].set_index("trade_date").sort_index()
        s = s[s["dlong"].notna() & s["dshort"].notna()].copy()
        if len(s) < sm.RULES["event_min_hist"]:
            continue
        dnet = s["dlong"] - s["dshort"]
        flow = (dnet / oi.reindex(s.index)).dropna()
        thr = (flow.abs().rolling(sm.RULES["event_window"],
                                  min_periods=sm.RULES["event_min_hist"])
               .quantile(sm.RULES["event_q"]).shift(1))
        sign = flow > 0 if direction == "increase_long" else flow < 0
        hit = flow[sign & (flow.abs() >= thr) & thr.notna()]
        if hit.empty:
            continue
        sub = s.loc[hit.index]
        dominant = (sub["dlong"].abs() >= sub["dshort"].abs()
                    if direction in {"increase_long", "reduce_long"}
                    else sub["dshort"].abs() > sub["dlong"].abs())
        leg_sign = (sub["dlong"] > 0 if direction == "increase_long"
                    else (sub["dlong"] < 0 if direction == "reduce_long"
                          else sub["dshort"] > 0))
        idx = sub.index[dominant & leg_sign]
        if len(idx):
            out.append(pd.DataFrame({
                "trade_date": idx, "member": member,
                "strength": (flow.loc[idx].abs() / thr.loc[idx])
                .clip(upper=sm.RULES["strength_cap"]).to_numpy(),
            }))
    return (pd.concat(out, ignore_index=True) if out
            else pd.DataFrame(columns=["trade_date", "member", "strength"]))


def _bool_column(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series
    return series.astype(str).str.strip().str.lower().isin(["t", "true", "1", "yes"])


def build_net_views(code: str, dates: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """构造D族的60交易日净多峰值（最终可见/严格点时）。

    八家任一席位任一多空腿缺失时，总净仓就是未知而不是0；该日不参与峰值。
    严格版在决策日T只注入 ``reboard_date<=T`` 的历史事实，再在当时可见的
    60日窗口（至少20个完整观测）上算峰值。D沿用前次60日口径，不改成
    “自进场以来峰值”。
    """
    seat = pd.read_csv(DATA / f"{code.lower()}_seat.csv.gz", parse_dates=["trade_date"])
    seat["is_variety_total"] = _bool_column(seat["is_variety_total"])
    seat = seat[(~seat["is_variety_total"]) & seat["rank_type"].isin(["long", "short"])]

    visibility = pd.read_csv(DATA / "reboard_visibility.csv.gz",
                             parse_dates=["trade_date", "reboard_date"])
    visibility = visibility[visibility["instrument"].astype(str).str.upper() == code]
    keys = ["instrument", "contract", "rank_type", "member", "trade_date", "source"]
    inf = seat[seat["source"].astype(str) == "reboard_inferred"].copy()
    inf["instrument"] = inf["instrument"].astype(str).str.upper()
    vis = visibility.copy()
    vis["instrument"] = vis["instrument"].astype(str).str.upper()
    before = len(inf)
    inf = inf.merge(vis[keys + ["reboard_date"]], on=keys, how="left", validate="one_to_one")
    if len(inf) != before or inf["reboard_date"].isna().any():
        raise AssertionError(f"{code} inferred 与 visibility 非一一映射")

    official = seat[seat["source"].astype(str) != "reboard_inferred"].copy()
    official["member"] = official["member"].replace(ALIAS)
    inf["member"] = inf["member"].replace(ALIAS)
    official = official[official["member"].isin(GROUP)]
    inf = inf[inf["member"].isin(GROUP)]

    def aggregate(rows: pd.DataFrame, extra: list[str] | None = None) -> pd.DataFrame:
        by = (extra or []) + ["trade_date", "member", "rank_type"]
        return (rows.groupby(by, observed=True)["quantity"].sum(min_count=1)
                .reset_index())

    off = aggregate(official)
    inferred = aggregate(inf, ["reboard_date"])
    member_i = {m: i for i, m in enumerate(GROUP)}
    side_i = {"long": 0, "short": 1}
    date_i = {d: i for i, d in enumerate(dates)}
    n = len(dates)

    off_by_day: dict[int, list[tuple[int, int, float]]] = {}
    for row in off.itertuples(index=False):
        d = date_i.get(pd.Timestamp(row.trade_date))
        if d is not None:
            off_by_day.setdefault(d, []).append(
                (member_i[row.member], side_i[row.rank_type], float(row.quantity)))
    inf_by_avail: dict[int, list[tuple[int, int, int, float]]] = {}
    for row in inferred.itertuples(index=False):
        d = date_i.get(pd.Timestamp(row.trade_date))
        a = date_i.get(pd.Timestamp(row.reboard_date))
        if d is not None and a is not None:
            inf_by_avail.setdefault(a, []).append(
                (d, member_i[row.member], side_i[row.rank_type], float(row.quantity)))

    qty = np.zeros((len(GROUP), 2, n), dtype=np.float64)
    known = np.zeros((len(GROUP), 2, n), dtype=bool)
    current_history = np.full(n, np.nan, dtype=np.float64)
    snapshots = np.full((n, n), np.nan, dtype=np.float32)

    def recalc(d: int) -> None:
        if not known[:, :, d].all():
            current_history[d] = np.nan
            return
        nets = qty[:, 0, d] - qty[:, 1, d]
        current_history[d] = float(nets.sum())

    for t in range(n):
        affected = {t}
        for m, side, value in off_by_day.get(t, []):
            qty[m, side, t] += value
            known[m, side, t] = True
        for d, m, side, value in inf_by_avail.get(t, []):
            qty[m, side, d] += value
            known[m, side, d] = True
            affected.add(d)
        for d in affected:
            recalc(d)
        snapshots[t, :t + 1] = current_history[:t + 1]

    strict_current = np.diag(snapshots).astype(np.float64)
    strict_peak60 = np.full(n, np.nan, dtype=np.float64)
    for t in range(n):
        window = snapshots[t, max(0, t - 59):t + 1].astype(float)
        good = window[np.isfinite(window)]
        if len(good) >= 20:
            strict_peak60[t] = float(good.max())
    del snapshots

    all_rows = pd.concat([official, inf], ignore_index=True)
    final = aggregate(all_rows)
    wide = final.pivot_table(index=["trade_date", "member"], columns="rank_type",
                             values="quantity", aggfunc="sum")
    full_index = pd.MultiIndex.from_product([dates, GROUP], names=["trade_date", "member"])
    wide = wide.reindex(full_index).reindex(columns=["long", "short"])
    complete = wide.notna().all(axis=1).groupby(level="trade_date").all()
    net = wide["long"] - wide["short"]
    total = net.groupby(level="trade_date").sum(min_count=len(GROUP)).reindex(dates)
    inclusive = total.where(complete.reindex(dates).fillna(False)).to_numpy(float)
    return inclusive, strict_peak60, strict_current


def build_entry_ledger(code: str, engine: sm.MarketEngine,
                       trades: list[sm.Trade]) -> pd.DataFrame:
    rows = []
    factors = (engine.cont["factor"].iloc[-1] / engine.cont["factor"]).to_numpy()
    pos = {d: i for i, d in enumerate(engine.dates)}
    executed = [t for t in trades if t.entry_date is not None]
    for num, t in enumerate(executed, 1):
        entry_i = pos[pd.Timestamp(t.entry_date)]
        exit_i = pos[pd.Timestamp(t.exit_date)] if t.exit_date is not None else None
        rows.append({
            "entry_id": f"{code}-{num:03d}", "market": code,
            "signal_date": pd.Timestamp(t.signal_date),
            "entry_date": pd.Timestamp(t.entry_date), "entry_i": entry_i,
            "entry_px_real": float(t.entry_px),
            "entry_px_adj": float(t.entry_px) * factors[entry_i],
            "relay": bool(t.is_relay), "engine_result": t.result,
            "engine_exit_date": pd.Timestamp(t.exit_date) if t.exit_date is not None else pd.NaT,
            "engine_exit_i": exit_i,
            "engine_ret": float(t.ret_pct),
        })
    return pd.DataFrame(rows)


def event_matrix(events: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    mat = pd.DataFrame(False, index=dates, columns=GROUP)
    for row in events.itertuples(index=False):
        d = pd.Timestamp(row.trade_date)
        if d in mat.index and row.member in mat.columns:
            mat.at[d, row.member] = True
    return mat


class TriggerBook:
    def __init__(self, markets: dict[str, Market], ratio: pd.Series):
        self.markets = markets
        self.ratio = ratio
        self.cache: dict[tuple[str, str], np.ndarray] = {}
        self.event_mats: dict[tuple[str, str], pd.DataFrame] = {}
        for code, market in markets.items():
            self.event_mats[(code, "long")] = event_matrix(market.increase_long, market.dates)
            self.event_mats[(code, "short")] = event_matrix(market.increase_short, market.dates)
            self.event_mats[(code, "reduce")] = event_matrix(market.reduce_long, market.dates)

    @staticmethod
    def _rolling_distinct(mat: pd.DataFrame, window: int) -> pd.DataFrame:
        return mat.astype(int).rolling(window, min_periods=1).max().astype(bool)

    def global_mask(self, code: str, component: Component) -> np.ndarray:
        key = (code, component.key)
        if key in self.cache:
            return self.cache[key]
        market = self.markets[code]
        fam = component.family
        if fam == "A":
            n = int(component.get("n"))
            mask = (market.engine.active.astype(int).rolling(n, min_periods=n).sum() == 0).to_numpy()
        elif fam == "B":
            n, phi = int(component.get("n")), float(component.get("phi"))
            low = market.engine.score < phi
            mask = (low.astype(int).rolling(n, min_periods=n).sum() == n).to_numpy()
        elif fam == "C":
            k, m = int(component.get("k")), int(component.get("m"))
            recent = self._rolling_distinct(self.event_mats[(code, "reduce")], m)
            mask = (recent.sum(axis=1) >= k).to_numpy()
        elif fam == "E":
            hot_ag = sm.flee_hot_days(self.markets["AG"].engine, self.ratio)
            hot = hot_ag.reindex(market.dates).fillna(False).to_numpy(bool)
            if component.get("mode") == "hot_day":
                mask = hot
            else:
                mask = np.zeros(len(market.dates), dtype=bool)
                for start, end in sm.flee_suppress_windows(self.markets["AG"].engine, self.ratio):
                    mask |= ((market.dates >= start) & (market.dates <= end))
        elif fam == "F":
            k, m = int(component.get("k")), int(component.get("m"))
            own = self._rolling_distinct(self.event_mats[(code, "short")], m)
            other_code = "AG" if code == "AU" else "AU"
            other_raw = self.event_mats[(other_code, "long")].reindex(market.dates).fillna(False)
            other = self._rolling_distinct(other_raw, m)
            count = pd.Series(0.0, index=market.dates)
            for member in GROUP:
                contribution = own[member].astype(float)
                # v52识别比价腿结构；REPORT_AU_v1与生产spread_seats固定既有折半口径。
                if member in SPREAD_SEATS:
                    contribution = contribution.where(~other[member], contribution * 0.5)
                count = count.add(contribution, fill_value=0)
            mask = (count >= k).to_numpy()
        elif fam == "H":
            base = Component.make("A", n=10)
            mask = self.global_mask(code, base)
        else:
            raise KeyError(f"{fam} 没有全局席位触发掩码")
        self.cache[key] = np.asarray(mask, dtype=bool)
        return self.cache[key]

    def d_mask(self, market: Market, entry_i: int, x: float, pit: str) -> np.ndarray:
        n = len(market.dates)
        mask = np.zeros(n, dtype=bool)
        if pit == PIT_INCLUSIVE:
            s = pd.Series(market.net_inclusive, index=market.dates)
            peak = s.rolling(60, min_periods=20).max().to_numpy(float)
            cur = market.net_inclusive
        else:
            peak = market.net_strict_peak60
            cur = market.net_strict_current
        ok = np.isfinite(peak) & np.isfinite(cur) & (peak > 0) & (cur <= peak * (1 - x))
        mask[:] = ok
        mask[:entry_i] = False
        return mask


def shift_true_segments(mask: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """每段席位触发日期整体随机偏移 ±5~10 个交易日，保持段长。"""
    out = np.zeros_like(mask, dtype=bool)
    x = np.r_[False, mask, False].astype(int)
    starts = np.flatnonzero(np.diff(x) == 1)
    ends = np.flatnonzero(np.diff(x) == -1) - 1
    for start, end in zip(starts, ends):
        offset = int(rng.integers(5, 11)) * (-1 if rng.integers(0, 2) == 0 else 1)
        a, b = start + offset, end + offset
        aa, bb = max(0, a), min(len(mask) - 1, b)
        if aa <= bb:
            out[aa:bb + 1] = True
    return out


def shift_event_rows(events: pd.DataFrame, dates: pd.DatetimeIndex,
                     offsets: dict[tuple, int], code: str,
                     event_kind: str) -> pd.DataFrame:
    """按预生成的底层事件偏移表平移；同一事实跨规则族共用同一偏移。"""
    if events.empty:
        return events.copy()
    pos = {d: i for i, d in enumerate(dates)}
    rows = []
    for row in events.to_dict("records"):
        trade_date = pd.Timestamp(row["trade_date"])
        i = pos.get(trade_date)
        if i is None:
            continue
        key = (code, event_kind, str(row["member"]), trade_date)
        offset = offsets[key]
        j = i + offset
        if 0 <= j < len(dates):
            shifted = dict(row)
            shifted["trade_date"] = dates[j]
            rows.append(shifted)
    return pd.DataFrame(rows, columns=events.columns)


class PlaceboBook:
    """由平移后的原始席位事件重建A/B/C/E/F，D平移其状态触发段。"""

    def __init__(self, base: TriggerBook, rng: np.random.Generator):
        self.base = base
        self.rng = rng
        self.markets = base.markets
        self.ratio = base.ratio
        self.cache: dict[tuple[str, str], np.ndarray] = {}
        self.d_cache: dict[tuple[str, int, float, str], np.ndarray] = {}
        self.event_mats: dict[tuple[str, str], pd.DataFrame] = {}
        self.score: dict[str, pd.Series] = {}
        self.active: dict[str, pd.Series] = {}
        self.prod_short: dict[str, pd.DataFrame] = {}

        # 先对底层经济事实的并集抽一次偏移。生产ev_long/ev_short与严格C/F
        # 若指向同一(code,方向,席位,日期)，必须保持跨规则族相关性。
        sources: list[tuple[str, str, pd.DataFrame]] = []
        for code, market in sorted(self.markets.items()):
            sources.extend([
                (code, "increase_long", market.engine.ev_long),
                (code, "increase_long", market.increase_long),
                (code, "increase_short", market.engine.ev_short),
                (code, "increase_short", market.increase_short),
                (code, "reduce_long", market.reduce_long),
            ])
        event_keys = {
            (code, kind, str(row.member), pd.Timestamp(row.trade_date))
            for code, kind, events in sources
            for row in events.itertuples(index=False)
        }
        offsets: dict[tuple, int] = {}
        for key in sorted(event_keys, key=lambda x: (x[0], x[1], x[3], x[2])):
            offsets[key] = (int(rng.integers(5, 11))
                            * (-1 if rng.integers(0, 2) == 0 else 1))

        for code, market in self.markets.items():
            # A/B 从生产增多事件的原始行重建，并重新计算逐年权重与条件席位门。
            ev_long = shift_event_rows(
                market.engine.ev_long, market.dates, offsets, code, "increase_long")
            years = range(market.dates[0].year, market.dates[-1].year + 1)
            weights = sm.yearly_weights(ev_long, market.engine.cont, years)
            ev_eff = ev_long.copy()
            if len(ev_eff):
                ev_eff["dist"] = market.engine.dist60.reindex(ev_eff["trade_date"]).to_numpy()
                ev_eff = ev_eff[~(
                    ev_eff["member"].isin(sm.RULES["cond_seats"])
                    & (ev_eff["dist"] >= 0.05))]
            strong = (ev_eff.pivot_table(index="trade_date", columns="member",
                                         values="strength", aggfunc="max")
                      .reindex(market.dates).reindex(columns=GROUP))
            wmat = pd.DataFrame(
                {m: [weights[d.year].get(m, 0.0) for d in market.dates] for m in GROUP},
                index=market.dates)
            self.score[code] = ((strong.fillna(0) * wmat)
                                .rolling(sm.RULES["score_window"], min_periods=1)
                                .max().sum(axis=1))
            self.active[code] = (strong.notna() & (wmat > 0)).any(axis=1)

            strict_long = shift_event_rows(
                market.increase_long, market.dates, offsets, code, "increase_long")
            strict_short = shift_event_rows(
                market.increase_short, market.dates, offsets, code, "increase_short")
            strict_reduce = shift_event_rows(
                market.reduce_long, market.dates, offsets, code, "reduce_long")
            self.event_mats[(code, "long")] = event_matrix(strict_long, market.dates)
            self.event_mats[(code, "short")] = event_matrix(strict_short, market.dates)
            self.event_mats[(code, "reduce")] = event_matrix(strict_reduce, market.dates)
            self.prod_short[code] = shift_event_rows(
                market.engine.ev_short, market.dates, offsets, code, "increase_short")

        # 同一份AG底层事件重建一个全局E警报，再共同映射到AU/AG，保留跨市场相关性。
        ag_copy = copy.copy(self.markets["AG"].engine)
        ag_copy.ev_short = self.prod_short["AG"]
        self.e_hot = sm.flee_hot_days(ag_copy, self.ratio)
        self.e_windows = sm.flee_suppress_windows(ag_copy, self.ratio)

    @staticmethod
    def _rolling_distinct(mat: pd.DataFrame, window: int) -> pd.DataFrame:
        return mat.astype(int).rolling(window, min_periods=1).max().astype(bool)

    def global_mask(self, code: str, component: Component) -> np.ndarray:
        key = (code, component.key)
        if key in self.cache:
            return self.cache[key]
        market = self.markets[code]
        fam = component.family
        if fam == "A":
            n = int(component.get("n"))
            mask = (self.active[code].astype(int).rolling(n, min_periods=n).sum() == 0).to_numpy()
        elif fam == "B":
            n, phi = int(component.get("n")), float(component.get("phi"))
            low = self.score[code] < phi
            mask = (low.astype(int).rolling(n, min_periods=n).sum() == n).to_numpy()
        elif fam == "C":
            k, m = int(component.get("k")), int(component.get("m"))
            recent = self._rolling_distinct(self.event_mats[(code, "reduce")], m)
            mask = (recent.sum(axis=1) >= k).to_numpy()
        elif fam == "E":
            if component.get("mode") == "hot_day":
                mask = self.e_hot.reindex(market.dates).fillna(False).to_numpy(bool)
            else:
                mask = np.zeros(len(market.dates), dtype=bool)
                for start, end in self.e_windows:
                    mask |= ((market.dates >= start) & (market.dates <= end))
        elif fam == "F":
            k, m = int(component.get("k")), int(component.get("m"))
            own = self._rolling_distinct(self.event_mats[(code, "short")], m)
            other_code = "AG" if code == "AU" else "AU"
            other_raw = self.event_mats[(other_code, "long")].reindex(market.dates).fillna(False)
            other = self._rolling_distinct(other_raw, m)
            count = pd.Series(0.0, index=market.dates)
            for member in GROUP:
                contribution = own[member].astype(float)
                if member in SPREAD_SEATS:
                    contribution = contribution.where(~other[member], contribution * 0.5)
                count = count.add(contribution, fill_value=0)
            mask = (count >= k).to_numpy()
        elif fam == "H":
            mask = self.global_mask(code, Component.make("A", n=10))
        else:
            raise KeyError(f"{fam} 没有安慰剂全局掩码")
        self.cache[key] = np.asarray(mask, dtype=bool)
        return self.cache[key]

    def d_mask(self, market: Market, entry_i: int, x: float, pit: str) -> np.ndarray:
        key = (market.code, entry_i, float(x), pit)
        if key not in self.d_cache:
            raw = self.base.d_mask(market, entry_i, x, pit)
            # D是持仓状态而非离散change事件；按预注册的“事件日期偏移”平移触发段。
            self.d_cache[key] = shift_true_segments(raw, self.rng)
        return self.d_cache[key]


def component_masks(book: TriggerBook, market: Market, entry_i: int, spec: RuleSpec,
                    pit: str, rng: np.random.Generator | None = None,
                    shifted_cache: dict[tuple, np.ndarray] | None = None) -> list[tuple[str, np.ndarray]]:
    masks: list[tuple[str, np.ndarray]] = []
    for component in spec.components:
        fam = component.family
        if fam in {"G", "J"}:
            continue
        if fam == "D":
            raw = book.d_mask(market, entry_i, float(component.get("x")), pit)
            cache_key = (market.code, component.key, entry_i)
        else:
            raw = book.global_mask(market.code, component)
            cache_key = (market.code, component.key)
        if rng is not None:
            assert shifted_cache is not None
            if cache_key not in shifted_cache:
                shifted_cache[cache_key] = shift_true_segments(raw, rng)
            raw = shifted_cache[cache_key]
        masks.append((fam, raw))
    # J是止损/保本维度，不得在单族测试中顺带删除现行A10消退。
    # E直接离场单族（以及E+J）同样是在现行退出上新增警报动作。
    fams = set(spec.families)
    if fams == {"J"} or ("E" in fams and fams.issubset({"E", "J"})):
        base = Component.make("A", n=10)
        raw = book.global_mask(market.code, base)
        if rng is not None:
            assert shifted_cache is not None
            key = (market.code, base.key)
            if key not in shifted_cache:
                shifted_cache[key] = shift_true_segments(raw, rng)
            raw = shifted_cache[key]
        masks.append(("A", raw))
    return masks


def simulate_entry(book: TriggerBook, market: Market, entry: pd.Series, spec: RuleSpec,
                   pit: str, rng: np.random.Generator | None = None,
                   shifted_cache: dict[tuple, np.ndarray] | None = None) -> dict[str, Any]:
    cont = market.engine.cont
    dates = market.dates
    i0 = int(entry["entry_i"])
    p0 = float(entry["entry_px_adj"])
    stop = 0.04
    breakeven = None
    trail = None
    for c in spec.components:
        if c.family == "J":
            stop, breakeven = float(c.get("stop")), c.get("be")
        elif c.family in {"G", "H"}:
            trail = float(c.get("x"))
    masks = component_masks(book, market, i0, spec, pit, rng, shifted_cache)

    low = cont["adj_low"].to_numpy()
    high = cont["adj_high"].to_numpy()
    close = cont["adj_close"].to_numpy()
    open_ = cont["adj_open"].to_numpy()
    stop_px = p0 * (1 - stop)
    peak = p0
    be_from: int | None = None
    scheduled: int | None = None
    scheduled_reason = ""
    exit_i: int | None = None
    exit_px: float | None = None
    reason = ""

    for i in range(i0, len(dates)):
        if scheduled == i:
            exit_i = i
            exit_px = open_[i] if np.isfinite(open_[i]) else close[i]
            reason = scheduled_reason
            break
        if not (np.isfinite(low[i]) and np.isfinite(high[i])):
            continue
        effective_stop = stop_px
        stop_reason = f"止损{stop * 100:.0f}%"
        if be_from is not None and i >= be_from:
            effective_stop = max(effective_stop, p0)
            if effective_stop >= p0:
                stop_reason = "保本"
        if trail is not None:
            trail_px = peak * (1 - trail)
            if trail_px > effective_stop:
                effective_stop = trail_px
                stop_reason = f"追踪{trail * 100:.0f}%"
        if low[i] <= effective_stop:
            exit_i, exit_px, reason = i, effective_stop, stop_reason
            break
        # 当日新高只从下一交易日抬高追踪/保本线，避免臆测日内 high-low 顺序。
        # 区间回踩进场的成交时刻未知，故进场日整根K线的high不用于抬线。
        if i > i0:
            peak = max(peak, high[i])
            if (breakeven is not None and be_from is None
                    and high[i] >= p0 * (1 + float(breakeven))):
                be_from = i + 1
        triggers = []
        for fam, mask in masks:
            if fam in {"A", "B", "H"}:
                ready = i > i0 + 2
            else:
                ready = i >= i0
            if ready and mask[i]:
                triggers.append(fam)
        if triggers and scheduled is None:
            if i + 1 < len(dates):
                scheduled = i + 1
                scheduled_reason = "+".join(triggers) + "信号T+1"
            else:
                scheduled_reason = "+".join(triggers) + "待T+1"

    completed = exit_i is not None
    if completed:
        ret = (float(exit_px) / p0 - 1) * 100
        exit_date = dates[exit_i]
        status = reason
    else:
        ret = (float(close[-1]) / p0 - 1) * 100
        exit_date = pd.NaT
        status = scheduled_reason or "持有中"
    return {
        "entry_id": entry["entry_id"], "market": market.code,
        "entry_date": entry["entry_date"], "entry_year": int(entry["entry_date"].year),
        "exit_date": exit_date, "completed": completed, "ret": ret,
        "status": status, "spec": spec.key,
    }


def simulate_spec(book: TriggerBook, markets: dict[str, Market], ledger: pd.DataFrame,
                  spec: RuleSpec, pit: str) -> pd.DataFrame:
    rows = []
    for entry in ledger.to_dict("records"):
        market = markets[entry["market"]]
        rows.append(simulate_entry(book, market, pd.Series(entry), spec, pit))
    return pd.DataFrame(rows).set_index("entry_id", drop=False)


def _risk_only_exit(market: Market, entry: dict[str, Any], stop: float,
                    breakeven: float | None) -> tuple[int | None, float | None, str]:
    """纯席位安慰剂的价格风控路径；与 ``simulate_entry`` 的日序完全一致。"""
    cont = market.engine.cont
    low = cont["adj_low"].to_numpy()
    high = cont["adj_high"].to_numpy()
    i0 = int(entry["entry_i"])
    p0 = float(entry["entry_px_adj"])
    stop_px = p0 * (1 - stop)
    be_from: int | None = None
    for i in range(i0, len(market.dates)):
        if not (np.isfinite(low[i]) and np.isfinite(high[i])):
            continue
        effective = stop_px
        reason = f"止损{stop * 100:.0f}%"
        if be_from is not None and i >= be_from:
            effective = max(effective, p0)
            if effective >= p0:
                reason = "保本"
        if low[i] <= effective:
            return i, float(effective), reason
        if (i > i0 and breakeven is not None and be_from is None
                and high[i] >= p0 * (1 + float(breakeven))):
            be_from = i + 1
    return None, None, ""


def simulate_spec_fast_pure(book: TriggerBook | PlaceboBook,
                            markets: dict[str, Market], ledger: pd.DataFrame,
                            spec: RuleSpec, pit: str,
                            risk_cache: dict[tuple, tuple[int | None, float | None, str]],
                            signal_cache: dict[tuple, int | None]) -> pd.DataFrame:
    """A/B/C/F/J宇宙的等价快速重放，用于嵌套安慰剂。"""
    if any(f not in {"A", "B", "C", "F", "J"} for f in spec.families):
        raise ValueError(f"fast pure不支持 {spec.key}")
    stop, breakeven = 0.04, None
    for component in spec.components:
        if component.family == "J":
            stop, breakeven = float(component.get("stop")), component.get("be")
    rows = []
    for entry in ledger.to_dict("records"):
        market = markets[entry["market"]]
        i0 = int(entry["entry_i"])
        p0 = float(entry["entry_px_adj"])
        risk_key = (entry["entry_id"], stop, breakeven)
        if risk_key not in risk_cache:
            risk_cache[risk_key] = _risk_only_exit(market, entry, stop, breakeven)
        risk_i, risk_px, risk_reason = risk_cache[risk_key]

        signal_components = [c for c in spec.components if c.family != "J"]
        if not signal_components:  # J单族保留现行A10。
            signal_components = [Component.make("A", n=10)]
        decision_i: int | None = None
        decision_fams: list[str] = []
        for component in signal_components:
            fam = component.family
            cache_key = (entry["entry_id"], component.key, pit)
            if cache_key not in signal_cache:
                mask = (book.d_mask(market, i0, float(component.get("x")), pit)
                        if fam == "D" else book.global_mask(market.code, component))
                start = i0 + 3 if fam in {"A", "B", "H"} else i0
                hits = np.flatnonzero(mask[start:])
                signal_cache[cache_key] = (start + int(hits[0])) if len(hits) else None
            hit = signal_cache[cache_key]
            if hit is None:
                continue
            if decision_i is None or hit < decision_i:
                decision_i, decision_fams = hit, [fam]
            elif hit == decision_i:
                decision_fams.append(fam)
        signal_i = (decision_i + 1 if decision_i is not None
                    and decision_i + 1 < len(market.dates) else None)

        # 开盘排队卖单先于同日盘中风控；否则先发生的价格风控退出。
        if signal_i is not None and (risk_i is None or signal_i <= risk_i):
            exit_i = signal_i
            open_ = float(market.engine.cont["adj_open"].iloc[exit_i])
            close = float(market.engine.cont["adj_close"].iloc[exit_i])
            exit_px = open_ if np.isfinite(open_) else close
            reason = "+".join(decision_fams) + "信号T+1"
        elif risk_i is not None:
            exit_i, exit_px, reason = risk_i, float(risk_px), risk_reason
        else:
            exit_i, exit_px = None, None
            reason = ("+".join(decision_fams) + "待T+1"
                      if decision_i is not None else "持有中")

        if exit_i is None:
            terminal = float(market.engine.cont["adj_close"].iloc[-1])
            ret, exit_date, completed = (terminal / p0 - 1) * 100, pd.NaT, False
        else:
            ret = (float(exit_px) / p0 - 1) * 100
            exit_date, completed = market.dates[exit_i], True
        rows.append({
            "entry_id": entry["entry_id"], "market": market.code,
            "entry_date": entry["entry_date"],
            "entry_year": int(pd.Timestamp(entry["entry_date"]).year),
            "exit_date": exit_date, "completed": completed, "ret": ret,
            "status": reason, "spec": spec.key,
        })
    return pd.DataFrame(rows).set_index("entry_id", drop=False)


def baseline_spec() -> RuleSpec:
    return RuleSpec("BASE", (Component.make("A", n=10),))


def asof_returns(results: pd.DataFrame, ledger: pd.DataFrame,
                 markets: dict[str, Market], cutoff: pd.Timestamp,
                 ids: Iterable[str] | None = None) -> pd.DataFrame:
    """对固定进场ID在共同截止日前做已实现/期末盯市估值。

    候选尚未退出不能被删掉；若截止日前未退出，就用该市场截止日前最后一个
    已完成交易日的复权收盘价盯市。这样训练与OOS都不借用截止日之后的退出。
    """
    led = ledger.set_index("entry_id", drop=False)
    eligible = led.index[led["entry_date"] < cutoff]
    if ids is not None:
        eligible = eligible.intersection(pd.Index(list(ids)))
    rows: list[dict[str, Any]] = []
    for code in ("AU", "AG"):
        market_ids = eligible[led.loc[eligible, "market"].to_numpy() == code]
        if not len(market_ids):
            continue
        market = markets[code]
        before = np.flatnonzero(market.dates < cutoff)
        if not len(before):
            continue
        value_i = int(before[-1])
        value_date = market.dates[value_i]
        terminal = float(market.engine.cont["adj_close"].iloc[value_i])
        r = results.loc[market_ids]
        l = led.loc[market_ids]
        realized = r["completed"].astype(bool) & (pd.to_datetime(r["exit_date"]) < cutoff)
        ret = (terminal / l["entry_px_adj"].astype(float) - 1) * 100
        ret.loc[realized] = r.loc[realized, "ret"].astype(float)
        for entry_id in market_ids:
            rows.append({
                "entry_id": entry_id,
                "market": code,
                "entry_date": l.at[entry_id, "entry_date"],
                "entry_year": int(pd.Timestamp(l.at[entry_id, "entry_date"]).year),
                "ret": float(ret.at[entry_id]),
                "realized": bool(realized.at[entry_id]),
                "valuation_date": value_date,
            })
    if not rows:
        return pd.DataFrame(columns=["entry_id", "market", "entry_date", "entry_year",
                                     "ret", "realized", "valuation_date"]).set_index("entry_id")
    return pd.DataFrame(rows).set_index("entry_id", drop=False)


def paired_asof(candidate: pd.DataFrame, baseline: pd.DataFrame, ledger: pd.DataFrame,
                markets: dict[str, Market], cutoff: pd.Timestamp,
                ids: Iterable[str] | None = None) -> pd.DataFrame:
    c = asof_returns(candidate, ledger, markets, cutoff, ids)
    b = asof_returns(baseline, ledger, markets, cutoff, ids)
    common = c.index.intersection(b.index)
    if not len(common):
        return pd.DataFrame()
    out = c.loc[common, ["entry_id", "market", "entry_date", "entry_year",
                         "valuation_date"]].copy()
    out["candidate_ret"] = c.loc[common, "ret"].to_numpy(float)
    out["baseline_ret"] = b.loc[common, "ret"].to_numpy(float)
    out["delta"] = out["candidate_ret"] - out["baseline_ret"]
    out["candidate_realized"] = c.loc[common, "realized"].to_numpy(bool)
    out["baseline_realized"] = b.loc[common, "realized"].to_numpy(bool)
    return out


def conservative_score(results: pd.DataFrame, baseline: pd.DataFrame,
                       ledger: pd.DataFrame, markets: dict[str, Market],
                       cutoff: pd.Timestamp) -> tuple[float, int, float]:
    paired = paired_asof(results, baseline, ledger, markets, cutoff)
    if len(paired) < MIN_TRAIN:
        return -np.inf, len(paired), np.nan
    delta = paired["delta"].to_numpy(float)
    std = np.std(delta, ddof=1)
    if not np.isfinite(std):
        return -np.inf, len(delta), float(np.mean(delta))
    mean = float(np.mean(delta))
    return mean - float(std / math.sqrt(len(delta))), len(delta), mean


def parameter_distance(spec: RuleSpec) -> tuple:
    distance = 0.0
    for c in spec.components:
        p = dict(c.params)
        if c.family == "A":
            distance += abs(p["n"] - 10) / 5
        elif c.family == "B":
            distance += abs(p["phi"] - 1) + abs(p["n"] - 10) / 5
        elif c.family in {"C", "F"}:
            distance += abs(p["k"] - 2) + abs(p["m"] - 5) / 5
        elif c.family in {"D", "G", "H"}:
            distance += float(p["x"])
        elif c.family == "E":
            distance += 0 if p["mode"] == "hot_day" else 1
        elif c.family == "J":
            distance += abs(p["stop"] - 0.04) * 100 + (0 if p["be"] is None else p["be"] * 100)
    return (len(spec.components), distance, spec.key)


def select_yearly(specs: list[RuleSpec], cache: dict[str, pd.DataFrame],
                  baseline: pd.DataFrame, ledger: pd.DataFrame,
                  markets: dict[str, Market], years: Iterable[int],
                  base: RuleSpec) -> tuple[dict[int, RuleSpec], pd.DataFrame]:
    """每年在完整候选宇宙中只用此前信息全局选一次，BASE是零差回退项。"""
    selected: dict[int, RuleSpec] = {}
    rows = []
    led = ledger.set_index("entry_id", drop=False)
    for year in years:
        cutoff = pd.Timestamp(f"{year}-01-01")
        ids = led.index[led["entry_date"] < cutoff]
        if len(ids) < MIN_TRAIN:
            rows.append({"year": year, "status": f"训练固定ID不足{MIN_TRAIN}",
                         "train_n": len(ids), "train_delta": np.nan,
                         "score": np.nan, "spec": ""})
            continue

        # 同一年度的共同MTM与BASE只算一次；每个候选只需覆盖截止前已实现退出。
        terminal = np.empty(len(ids), dtype=float)
        for code in ("AU", "AG"):
            loc = np.flatnonzero(led.loc[ids, "market"].to_numpy() == code)
            if not len(loc):
                continue
            market = markets[code]
            value_i = int(np.flatnonzero(market.dates < cutoff)[-1])
            close = float(market.engine.cont["adj_close"].iloc[value_i])
            terminal[loc] = (close / led.loc[ids[loc], "entry_px_adj"].to_numpy(float) - 1) * 100
        base_rows = baseline.loc[ids]
        base_ret = terminal.copy()
        base_realized = (base_rows["completed"].to_numpy(bool)
                         & (pd.to_datetime(base_rows["exit_date"]).to_numpy() < np.datetime64(cutoff)))
        base_ret[base_realized] = base_rows["ret"].to_numpy(float)[base_realized]

        scored = []
        for spec in specs:
            result = cache[spec.key].loc[ids]
            candidate_ret = terminal.copy()
            realized = (result["completed"].to_numpy(bool)
                        & (pd.to_datetime(result["exit_date"]).to_numpy() < np.datetime64(cutoff)))
            candidate_ret[realized] = result["ret"].to_numpy(float)[realized]
            delta = candidate_ret - base_ret
            std = float(np.std(delta, ddof=1))
            mean = float(np.mean(delta))
            score = mean - std / math.sqrt(len(delta)) if np.isfinite(std) else -np.inf
            if np.isfinite(score):
                scored.append((score, len(delta), mean, spec))
        best_score = max(score for score, _, _, _ in scored)
        # 近似打平或未明显超过现行时，按硬纪律回退更简单的BASE。
        if best_score <= TIE_EPS:
            picked, picked_score, picked_n, picked_mean = base, 0.0, scored[0][1], 0.0
            status = "BASE回退"
        else:
            near = [(score, n, mean, spec) for score, n, mean, spec in scored
                    if best_score - score <= TIE_EPS]
            picked_score, picked_n, picked_mean, picked = min(
                near, key=lambda row: parameter_distance(row[3]))
            status = "selected"
        selected[year] = picked
        rows.append({"year": year, "status": status, "train_n": picked_n,
                     "train_delta": picked_mean, "score": picked_score,
                     "spec": "BASE" if picked.template == "BASE" else picked.key})
    return selected, pd.DataFrame(rows)


def adaptive_results(selected: dict[int, RuleSpec], cache: dict[str, pd.DataFrame],
                     ledger: pd.DataFrame, years: set[int] | None = None) -> pd.DataFrame:
    rows = []
    for entry in ledger.to_dict("records"):
        year = int(pd.Timestamp(entry["entry_date"]).year)
        if year not in selected or (years is not None and year not in years):
            continue
        rows.append(cache[selected[year].key].loc[entry["entry_id"]].to_dict())
    return pd.DataFrame(rows).set_index("entry_id", drop=False) if rows else pd.DataFrame()


def oos_outcomes(selected: dict[int, RuleSpec], cache: dict[str, pd.DataFrame],
                 baseline: pd.DataFrame, ledger: pd.DataFrame,
                 markets: dict[str, Market], years: Iterable[int]) -> pd.DataFrame:
    """年度Y参数冻结后，只用Y内进场ID并在Y末（末年为样本末）统一盯市。"""
    rows = []
    for year in sorted(years):
        if year not in selected:
            continue
        ids = ledger.index[ledger["entry_date"].dt.year == year]
        ids = ledger.loc[ids, "entry_id"]
        if not len(ids):
            continue
        cutoff = min(pd.Timestamp(f"{year + 1}-01-01"), END_EXCLUSIVE)
        spec = selected[year]
        paired = paired_asof(cache[spec.key], baseline, ledger, markets, cutoff, ids)
        if paired.empty:
            continue
        paired["selected_spec"] = "BASE" if spec.template == "BASE" else spec.key
        paired["evaluation_cutoff"] = cutoff - pd.Timedelta(days=1)
        rows.append(paired)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows).set_index("entry_id", drop=False)


def outcome_metrics(outcomes: pd.DataFrame) -> dict[str, Any]:
    if outcomes.empty:
        return {"n": 0, "candidate_mean": np.nan, "baseline_mean": np.nan,
                "delta": np.nan, "candidate_sum": 0.0, "win_rate": np.nan,
                "candidate_realized": 0, "baseline_realized": 0}
    return {
        "n": len(outcomes),
        "candidate_mean": outcomes["candidate_ret"].mean(),
        "baseline_mean": outcomes["baseline_ret"].mean(),
        "delta": outcomes["delta"].mean(),
        "candidate_sum": outcomes["candidate_ret"].sum(),
        "win_rate": (outcomes["candidate_ret"] > 0).mean() * 100,
        "candidate_realized": int(outcomes["candidate_realized"].sum()),
        "baseline_realized": int(outcomes["baseline_realized"].sum()),
    }


def annual_slices(outcomes: pd.DataFrame, years: Iterable[int]) -> pd.DataFrame:
    rows = []
    for year in sorted(years):
        y = outcomes[outcomes["entry_year"] == year]
        if y.empty:
            continue
        m = outcome_metrics(y)
        rows.append({"year": int(year), "period": f"{year}-01-01~{y['evaluation_cutoff'].iloc[0].date()}",
                     "n": m["n"], "candidate_realized": m["candidate_realized"],
                     "baseline_realized": m["baseline_realized"],
                     "candidate_mean": m["candidate_mean"],
                     "baseline_mean": m["baseline_mean"], "delta": m["delta"],
                     "non_loss": m["delta"] >= -1e-12,
                     "eligibility": ("可采信" if m["n"] >= MIN_OOS
                                     else "仅用于年度方向门；样本不足，不采信（单年）")})
    return pd.DataFrame(rows)


def market_slices(outcomes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for code in ("AU", "AG"):
        m = outcome_metrics(outcomes[outcomes["market"] == code])
        rows.append({"market": code, "n": m["n"], "candidate_mean": m["candidate_mean"],
                     "baseline_mean": m["baseline_mean"], "delta": m["delta"],
                     "eligibility": ("可采信" if m["n"] >= MIN_OOS
                                     else "样本不足，不采信")})
    return pd.DataFrame(rows)


def bootstrap_delta(outcomes: pd.DataFrame, rng: np.random.Generator,
                    reps: int = 10000) -> tuple[float, float]:
    """按OOS年度整块重采样，避免把同年重叠持仓当IID。"""
    years = sorted(outcomes["entry_year"].unique()) if len(outcomes) else []
    if len(years) < 2:
        return np.nan, np.nan
    values = np.empty(reps)
    for i in range(reps):
        sampled = rng.choice(years, size=len(years), replace=True)
        blocks = [outcomes.loc[outcomes["entry_year"] == year, "delta"].to_numpy(float)
                  for year in sampled]
        values[i] = np.concatenate(blocks).mean()
    return tuple(np.quantile(values, [0.025, 0.975]))


def neighbor_value(value: Any, grid: tuple, direction: int) -> Any | None:
    idx = grid.index(value)
    j = idx + direction
    return grid[j] if 0 <= j < len(grid) else None


def perturb_component(component: Component, dimension: str, direction: int) -> Component | None:
    grids = {
        ("A", "n"): A_N, ("B", "phi"): B_PHI, ("B", "n"): B_N,
        ("C", "k"): C_K, ("C", "m"): C_M, ("D", "x"): D_X,
        ("E", "mode"): E_MODE, ("F", "k"): F_K, ("F", "m"): F_M,
        ("G", "x"): G_X, ("H", "x"): G_X,
        ("J", "stop"): J_STOP, ("J", "be"): J_BE,
    }
    key = (component.family, dimension)
    if key not in grids:
        return None
    params = dict(component.params)
    nxt = neighbor_value(params[dimension], grids[key], direction)
    if nxt is None:
        return None
    params[dimension] = nxt
    return Component.make(component.family, **params)


def sensitivity(selected: dict[int, RuleSpec], all_specs: list[RuleSpec],
                cache: dict[str, pd.DataFrame], ledger: pd.DataFrame,
                markets: dict[str, Market], years: set[int],
                baseline: pd.DataFrame, base: RuleSpec,
                reference_delta: float) -> pd.DataFrame:
    lookup = {s.key: s for s in [*all_specs, base]}
    dims = sorted({(c.family, k) for s in selected.values() for c in s.components for k, _ in c.params})
    rows = []
    for family, dim in dims:
        for direction in (-1, 1):
            changed_years = 0
            alt: dict[int, RuleSpec] = {}
            for year, spec in selected.items():
                if spec.template == "BASE":
                    alt[year] = spec
                    continue
                comps = list(spec.components)
                for i, comp in enumerate(comps):
                    if comp.family == family:
                        new = perturb_component(comp, dim, direction)
                        if new is not None:
                            comps[i] = new
                            changed_years += 1
                key = "+".join(c.key for c in comps)
                alt[year] = lookup.get(key, spec)
            if not changed_years:
                rows.append({"parameter": f"{family}.{dim}",
                             "direction": "-1档" if direction < 0 else "+1档",
                             "changed_years": 0, "n": 0, "delta": np.nan,
                             "direction_kept": np.nan, "status": "边界，无相邻档"})
                continue
            outcomes = oos_outcomes(alt, cache, baseline, ledger, markets, years)
            m = outcome_metrics(outcomes)
            kept = (m["delta"] >= 0 if reference_delta >= 0 else m["delta"] <= 0)
            rows.append({"parameter": f"{family}.{dim}",
                         "direction": "-1档" if direction < 0 else "+1档",
                         "changed_years": changed_years,
                         "n": m["n"], "delta": m["delta"],
                         "direction_kept": bool(m["n"] >= MIN_OOS and kept),
                         "status": "已重放"})
    return pd.DataFrame(rows)


def placebo_distribution(book: TriggerBook, markets: dict[str, Market], ledger: pd.DataFrame,
                         eligible_specs: list[RuleSpec], years: Iterable[int],
                         baseline: pd.DataFrame, base: RuleSpec,
                         reps: int, seed: int) -> np.ndarray:
    """逐次平移底层事件、重建全候选并重做年度全局选择（含BASE回退）。"""
    values = np.full(reps, np.nan)
    risk_cache: dict[tuple, tuple[int | None, float | None, str]] = {}
    for rep in range(reps):
        rng = np.random.default_rng(seed + rep + 1)
        pbook = PlaceboBook(book, rng)
        pcache = {base.key: baseline}
        signal_cache: dict[tuple, int | None] = {}
        for spec in eligible_specs:
            pcache[spec.key] = simulate_spec_fast_pure(
                pbook, markets, ledger, spec, PIT_STRICT, risk_cache, signal_cache)
        selected, _ = select_yearly(
            eligible_specs, pcache, baseline, ledger, markets, years, base)
        used_years = set(selected)
        outcomes = oos_outcomes(selected, pcache, baseline, ledger, markets, used_years)
        m = outcome_metrics(outcomes)
        if m["n"] >= MIN_OOS:
            values[rep] = m["delta"]
        if (rep + 1) % 5 == 0:
            print(f"[placebo nested] {rep + 1}/{reps}", flush=True)
    return values[np.isfinite(values)]


def fmt_value(v: Any) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    if isinstance(v, (np.bool_, bool)):
        return "是" if bool(v) else "否"
    if isinstance(v, (float, np.floating)):
        return f"{float(v):.3f}".rstrip("0").rstrip(".")
    return str(v)


def md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "（无）"
    cols = list(df.columns)
    lines = ["| " + " | ".join(map(str, cols)) + " |",
             "|" + "|".join(["---"] * len(cols)) + "|"]
    for row in df.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(fmt_value(v).replace("|", "\\|") for v in row) + " |")
    return "\n".join(lines)


def family_diagnostics(templates: dict[str, list[RuleSpec]], cache: dict[str, pd.DataFrame],
                       baseline: pd.DataFrame, ledger: pd.DataFrame,
                       markets: dict[str, Market], years: Iterable[int],
                       base: RuleSpec, families: Iterable[str]) -> pd.DataFrame:
    """各单族独立WF诊断；只按注册顺序呈报，不用其OOS结果二次选冠军。"""
    rows = []
    for family in families:
        if family == "E":
            rows.append({"family": family, "specs": len(templates[family]),
                         "period": "回溯自洽", "n": 0, "candidate_mean": np.nan,
                         "baseline_mean": np.nan, "delta": np.nan,
                         "non_loss_years": 0, "year_slices": 0,
                         "selected_nonbase_years": 0,
                         "eligibility": "警报规则2026案例驱动；episode=1，样本不足不采信"})
            continue
        if family == "D":
            rows.append({"family": family, "specs": len(templates[family]),
                         "period": "严格点时全期", "n": 0,
                         "candidate_mean": np.nan, "baseline_mean": np.nan,
                         "delta": np.nan, "non_loss_years": 0, "year_slices": 0,
                         "selected_nonbase_years": 0,
                         "eligibility": "60日完整净仓峰值观测n=0，样本不足不采信"})
            continue
        selected, _ = select_yearly(
            templates[family], cache, baseline, ledger, markets, years, base)
        used_years = set(selected)
        outcomes = oos_outcomes(selected, cache, baseline, ledger, markets, used_years)
        metrics = outcome_metrics(outcomes)
        annual = annual_slices(outcomes, used_years)
        rows.append({
            "family": family, "specs": len(templates[family]),
            "period": f"{min(used_years)}-{max(used_years)}" if used_years else "—",
            "n": metrics["n"], "candidate_mean": metrics["candidate_mean"],
            "baseline_mean": metrics["baseline_mean"], "delta": metrics["delta"],
            "non_loss_years": int(annual["non_loss"].sum()) if len(annual) else 0,
            "year_slices": len(annual),
            "selected_nonbase_years": sum(s.template != "BASE" for s in selected.values()),
            "eligibility": "诊断；不参与OOS后挑选",
        })
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--placebos", type=int, default=PLACEBO_REPS)
    args = parser.parse_args()
    if args.placebos < 100:
        raise ValueError("安慰剂次数硬门要求 >=100")

    required = ["au_price.csv.gz", "ag_price.csv.gz", "au_seat.csv.gz", "ag_seat.csv.gz",
                "reboard_visibility.csv.gz", "gold_silver_ratio.csv"]
    for name in required:
        if not (DATA / name).exists():
            raise FileNotFoundError(DATA / name)

    ratio_raw = load_ratio()
    engines: dict[str, sm.MarketEngine] = {}
    for code in ("AU", "AG"):
        price, seat = sm.load_from_csv(DATA, code)
        engines[code] = sm.MarketEngine(code, price, seat)
    suppress_engine = sm.flee_suppress_windows(engines["AG"], ratio_raw)
    engine_trades = {code: eng.replay(suppress_engine) for code, eng in engines.items()}

    markets: dict[str, Market] = {}
    ledgers = []
    for code, engine in engines.items():
        ledger = build_entry_ledger(code, engine, engine_trades[code])
        ledgers.append(ledger)
        inclusive, strict_peak, strict_current = build_net_views(code, engine.dates)
        markets[code] = Market(
            code, engine, engine.dates, {d: i for i, d in enumerate(engine.dates)}, ledger,
            strict_flow_events(engine, "reduce_long"),
            strict_flow_events(engine, "increase_long"),
            strict_flow_events(engine, "increase_short"),
            inclusive, strict_peak, strict_current)
    ledger = pd.concat(ledgers, ignore_index=True).sort_values(["entry_date", "market"]).reset_index(drop=True)
    # 冻结账本与所有复合警报机械回放只调用生产函数及原缓存。源码中并不存在
    # 回执所述的美东→沪市可见日映射；本研究不擅自重写，E因此不具备时间准入资格。
    book = TriggerBook(markets, ratio_raw)
    templates, specs = enumerate_specs()

    # 第一步硬门：独立实现的现行离场必须逐笔对上冻结引擎。
    base_spec = baseline_spec()
    baseline = simulate_spec(book, markets, ledger, base_spec, PIT_STRICT)
    audit = ledger.set_index("entry_id").join(baseline[["exit_date", "completed", "ret", "status"]])
    date_match = ((audit["engine_exit_date"].isna() & audit["exit_date"].isna())
                  | (audit["engine_exit_date"] == audit["exit_date"]))
    ret_match = np.isclose(audit["engine_ret"], audit["ret"], atol=1e-9, rtol=0)
    mismatch = audit[~(date_match & ret_match)]
    if len(mismatch):
        raise AssertionError(f"基线逐笔对拍失败 {len(mismatch)} 笔:\n{mismatch.head()}")

    # 645 格双跑。没有 D 的规格不受 inferred quantity 影响，复用严格结果。
    cache_strict: dict[str, pd.DataFrame] = {}
    cache_inclusive: dict[str, pd.DataFrame] = {}
    for i, spec in enumerate(specs, 1):
        strict = simulate_spec(book, markets, ledger, spec, PIT_STRICT)
        cache_strict[spec.key] = strict
        cache_inclusive[spec.key] = (simulate_spec(book, markets, ledger, spec, PIT_INCLUSIVE)
                                     if "D" in spec.families else strict)
        if i % 100 == 0:
            print(f"[grid] {i}/{len(specs)}", flush=True)
    cache_strict[base_spec.key] = baseline
    cache_inclusive[base_spec.key] = baseline

    all_years = range(int(ledger["entry_date"].dt.year.min()),
                      int(ledger["entry_date"].dt.year.max()) + 1)
    pure_all = [s for s in specs if not any(f in {"G", "H"} for f in s.families)]
    price_all = [s for s in specs if any(f in {"G", "H"} for f in s.families)]
    # E 是 2026-08 才由案例形成的规则；回溯照测，但硬纪律禁止其进入统计冠军。
    # D在“不把掉榜/缺腿补0”的严格数据上没有任何可计算60日峰值，不能让
    # D+J退化成“仅止损长期持有”后借壳进入冠军；照测但按有效触发n=0不准入。
    pure_eligible = [s for s in pure_all if not ({"D", "E"} & set(s.families))]
    price_eligible = [s for s in price_all if not ({"D", "E"} & set(s.families))]
    assert (len(pure_all), len(price_all), len(pure_eligible), len(price_eligible)) == (534, 111, 402, 99)

    # 嵌套安慰剂使用快速纯席位重放；先在真实掩码上逐规格证明与通用实现等价。
    fast_risk_cache: dict[tuple, tuple[int | None, float | None, str]] = {}
    fast_signal_cache: dict[tuple, int | None] = {}
    for spec in pure_eligible:
        fast = simulate_spec_fast_pure(
            book, markets, ledger, spec, PIT_STRICT, fast_risk_cache, fast_signal_cache)
        slow = cache_strict[spec.key]
        same_date = ((fast["exit_date"].isna() & slow["exit_date"].isna())
                     | (fast["exit_date"] == slow["exit_date"]))
        same_ret = np.isclose(fast["ret"], slow["ret"], atol=1e-9, rtol=0)
        if not bool((same_date & same_ret).all()):
            raise AssertionError(f"安慰剂快速重放与通用实现不一致: {spec.key}")

    champion_selected, pure_picks = select_yearly(
        pure_eligible, cache_strict, baseline, ledger, markets, all_years, base_spec)
    pure_years = set(champion_selected)
    champion = oos_outcomes(
        champion_selected, cache_strict, baseline, ledger, markets, pure_years)
    champ_m = outcome_metrics(champion)
    annual = annual_slices(champion, pure_years)
    markets_table = market_slices(champion)
    ci_lo, ci_hi = bootstrap_delta(champion, np.random.default_rng(args.seed), reps=10000)

    # 同一年度冻结参数的全生命周期诊断：固定相同OOS ID，统一估值到研究截止日。
    # 它不替代年度共同截止主检验，仅验证阴性方向不依赖一年期估值窗口。
    lifetime_candidate = adaptive_results(
        champion_selected, cache_strict, ledger, pure_years)
    lifetime_ids = ledger.loc[
        ledger["entry_date"].dt.year.isin(pure_years), "entry_id"]
    lifetime_outcomes = paired_asof(
        lifetime_candidate, baseline, ledger, markets, END_EXCLUSIVE, lifetime_ids)
    lifetime_outcomes["evaluation_cutoff"] = REPORT_END
    lifetime_m = outcome_metrics(lifetime_outcomes)
    lifetime_annual = annual_slices(lifetime_outcomes, pure_years)
    lifetime_summary = pd.DataFrame([{
        "path": "同一年度冻结参数",
        "entry_period": f"{min(pure_years)}-{max(pure_years)}",
        "valuation_date": str(REPORT_END.date()),
        "n_fixed_ids": lifetime_m["n"],
        "candidate_realized": lifetime_m["candidate_realized"],
        "candidate_open_mtm": lifetime_m["n"] - lifetime_m["candidate_realized"],
        "baseline_realized": lifetime_m["baseline_realized"],
        "baseline_open_mtm": lifetime_m["n"] - lifetime_m["baseline_realized"],
        "candidate_mean": lifetime_m["candidate_mean"],
        "baseline_mean": lifetime_m["baseline_mean"],
        "delta": lifetime_m["delta"],
        "non_loss_years": int(lifetime_annual["non_loss"].sum()),
        "year_slices": len(lifetime_annual),
    }])

    # 同一年度参数路径下，仅切换 inferred 可见口径，隔离点时影响。
    champion_inclusive_same = oos_outcomes(
        champion_selected, cache_inclusive, baseline, ledger, markets, pure_years)
    incl_same_m = outcome_metrics(champion_inclusive_same)
    pit_ids = champion.index.intersection(champion_inclusive_same.index)
    pit_changed = int((~np.isclose(
        champion.loc[pit_ids, "candidate_ret"],
        champion_inclusive_same.loc[pit_ids, "candidate_ret"], atol=1e-9)).sum())

    incl_selected, incl_picks = select_yearly(
        pure_eligible, cache_inclusive, baseline, ledger, markets, all_years, base_spec)
    incl_years = set(incl_selected)
    incl_outcomes = oos_outcomes(
        incl_selected, cache_inclusive, baseline, ledger, markets, incl_years)
    incl_m = outcome_metrics(incl_outcomes)
    path_changed = sum(
        champion_selected.get(y, base_spec).key != incl_selected.get(y, base_spec).key
        for y in pure_years | incl_years)

    price_selected, price_picks = select_yearly(
        price_eligible, cache_strict, baseline, ledger, markets, all_years, base_spec)
    price_years = set(price_selected)
    price_outcomes = oos_outcomes(
        price_selected, cache_strict, baseline, ledger, markets, price_years)
    price_m = outcome_metrics(price_outcomes)
    price_annual = annual_slices(price_outcomes, price_years)

    family_table = family_diagnostics(
        templates, cache_strict, baseline, ledger, markets, all_years, base_spec,
        ("A", "B", "C", "D", "E", "F", "J"))
    j_diag = family_table.loc[family_table["family"] == "J"].iloc[0]
    f_selected, f_picks = select_yearly(
        templates["F"], cache_strict, baseline, ledger, markets, all_years, base_spec)
    f_years = set(f_selected)
    f_outcomes = oos_outcomes(f_selected, cache_strict, baseline, ledger, markets, f_years)
    f_market_table = market_slices(f_outcomes)
    f_market_table.insert(1, "period", f"{min(f_years)}-{max(f_years)}")
    f_market_table.insert(2, "selected_nonbase_years",
                          sum(s.template != "BASE" for s in f_selected.values()))

    sens = sensitivity(champion_selected, pure_eligible, cache_strict,
                       ledger, markets, pure_years, baseline, base_spec, champ_m["delta"])
    if len(sens):
        sens.insert(0, "period", f"{min(pure_years)}-{max(pure_years)}")
    placebo = placebo_distribution(book, markets, ledger, pure_eligible, all_years,
                                    baseline, base_spec, args.placebos, args.seed)
    placebo_q = np.quantile(placebo, [0, .05, .25, .5, .75, .95, 1]) if len(placebo) else np.full(7, np.nan)
    placebo_p = ((1 + int((placebo >= champ_m["delta"]).sum())) / (len(placebo) + 1)
                  if len(placebo) else np.nan)
    placebo_pass = bool(len(placebo) >= 100 and champ_m["delta"] > placebo_q[5]
                        and placebo_p < 0.05)
    applicable_sens = sens["direction_kept"].dropna() if len(sens) else pd.Series(dtype=bool)
    sensitivity_pass = bool(len(applicable_sens) and applicable_sens.astype(bool).all())
    year_pass = bool(len(annual) and annual["non_loss"].mean() >= 2 / 3)
    sample_pass = champ_m["n"] >= MIN_OOS
    oos_win = bool(champ_m["delta"] > 0)
    selected_candidate_years = sum(s.template != "BASE" for s in champion_selected.values())
    significant = bool(selected_candidate_years and oos_win and sample_pass and year_pass and placebo_pass
                       and sensitivity_pass and ci_lo > 0)
    if significant:
        verdict = "①有显著更优规则"
        verdict_text = "全部硬门通过，可采用严格点时全网格 walk-forward 路径。"
    elif oos_win:
        verdict = "②有边际改进但不显著（建议维持现行）"
        verdict_text = "OOS 点估计为正，但至少一项显著性/稳健性硬门未过，维持现行规则。"
    else:
        verdict = "③现行规则已达信息集上限"
        verdict_text = ("唯一全网格WF路径没有在严格点时OOS战胜基线；"
                        "单族近零正差按预注册近似打平纪律回退现行。")

    # 基线摘要与逐年分布。
    base_rows = []
    year_rows = []
    for code in ("AU", "AG"):
        ts = engine_trades[code]
        executed = [t for t in ts if t.entry_date is not None]
        closed = [t for t in executed if t.exit_date is not None]
        open_ = [t for t in executed if t.exit_date is None]
        abandoned = [t for t in ts if t.entry_date is None]
        first_entry = min(pd.Timestamp(t.entry_date) for t in executed)
        base_rows.append({"market": code, "period": f"{first_entry.date()}~2026-08-13",
                          "replay_records": len(ts), "executed": len(executed),
                          "closed": len(closed), "open": len(open_), "abandoned": len(abandoned),
                          "relay_executed": sum(t.is_relay for t in executed),
                          "closed_mean": np.mean([t.ret_pct for t in closed]),
                          "closed_sum": np.sum([t.ret_pct for t in closed])})
        counts = pd.Series([pd.Timestamp(t.entry_date).year for t in executed]).value_counts().sort_index()
        for year, count in counts.items():
            year_rows.append({"market": code, "year": int(year), "executed": int(count)})
    base_table = pd.DataFrame(base_rows)
    base_years = pd.DataFrame(year_rows)

    current_rows = []
    saved_path = ROOT / "engine" / "web" / "signals.json"
    saved = json.loads(saved_path.read_text(encoding="utf-8")) if saved_path.exists() else {}
    for code in ("AU", "AG"):
        snap = engines[code].snapshot(engine_trades[code])
        pos = snap["position"] or {}
        current_rows.append({"market": code, "local_data_date": "2026-08-13",
                             "state": snap["state"], "entry_date": pos.get("entry_date"),
                             "entry_px": pos.get("entry_px"), "pnl_pct": pos.get("pnl_pct"),
                             "stop_px": pos.get("stop_px"), "fade": pos.get("fade_days")})
    current_table = pd.DataFrame(current_rows)

    grid_rows = []
    descriptions = {
        "A": "消退N={5,8,12,15,20}",
        "B": "score连续N<{φ}; φ={0.5,1,2}, N={5,10,15}",
        "C": "K={2,3}, M={5,10}日双腿可见净减多",
        "D": "60日净多峰值回落X={25%,40%}（min20）",
        "E": "复合警报直接离场={热日,热日+40日窗}",
        "F": "K={2,3}, M={5,10}日双腿可见增空；固定比价腿折半",
        "G": "追踪回撤X={4%,6%,8%}",
        "H": "现行10日消退与追踪{4%,6%,8%}先到先出",
        "J": "止损={3%,4%,5%} × 保本={不用,浮盈3%,浮盈5%}",
    }
    for fam, values in family_grid().items():
        grid_rows.append({"family": fam, "registered": descriptions[fam],
                          "expected": len(values), "implemented": len(values), "extra_dimension": "否"})
    grid_rows.append({"family": "两族组合", "registered": "A/B/C/D/E/F/G/J 任取两族；H不得再叠加",
                      "expected": 604, "implemented": 604, "extra_dimension": "否"})
    grid_rows.append({"family": "总候选", "registered": "单族38 + 两族604 + H三档",
                      "expected": 645, "implemented": len(specs), "extra_dimension": "否"})
    grid_rows.append({"family": "结论准入", "registered": "纯534；价格111；E相关74事件n=1；D相关74有效峰值n=0",
                      "expected": "纯402/价格99", "implemented": f"纯{len(pure_eligible)}/价格{len(price_eligible)}",
                      "extra_dimension": "E/D照测；按既有样本硬门不准入，不改网格"})
    grid_table = pd.DataFrame(grid_rows)

    # 唯一、无OOS后选模的全网格年度路径。
    picks = pure_picks[pure_picks["year"].isin(sorted(pure_years))].copy()
    picks["train_period"] = picks["year"].map(
        lambda y: f"{ledger['entry_date'].min().date()}~{y - 1}-12-31")
    picks["oos_period"] = picks["year"].map(
        lambda y: f"{y}-01-01~{min(pd.Timestamp(f'{y + 1}-01-01'), END_EXCLUSIVE) - pd.Timedelta(days=1):%Y-%m-%d}")
    picks["oos_entries"] = picks["year"].map(
        ledger["entry_date"].dt.year.value_counts()).fillna(0).astype(int)
    picks = picks[["year", "train_period", "train_n", "train_delta", "score",
                   "status", "spec", "oos_period", "oos_entries"]]

    pit_table = pd.DataFrame([
        {"run": "严格点时（主）", "period": f"{min(pure_years)}-{max(pure_years)}",
         "n": champ_m["n"], "path": "全网格年度全局WF", "candidate_mean": champ_m["candidate_mean"],
         "baseline_mean": champ_m["baseline_mean"], "delta": champ_m["delta"],
         "changed_vs_strict": 0},
        {"run": "最终可见（同一年度参数路径）", "period": f"{min(pure_years)}-{max(pure_years)}",
         "n": incl_same_m["n"], "path": "沿用严格版年度参数",
         "candidate_mean": incl_same_m["candidate_mean"],
         "baseline_mean": incl_same_m["baseline_mean"], "delta": incl_same_m["delta"],
         "changed_vs_strict": pit_changed},
        {"run": "最终可见（独立全局WF）", "period": (f"{min(incl_years)}-{max(incl_years)}" if incl_years else "—"),
         "n": incl_m["n"], "path": "全网格年度全局WF",
         "candidate_mean": incl_m["candidate_mean"],
         "baseline_mean": incl_m["baseline_mean"],
         "delta": incl_m["delta"], "changed_vs_strict": f"年度路径变化{path_changed}年"},
    ])

    placebo_table = pd.DataFrame({
        "period": [f"{min(pure_years)}-{max(pure_years)}"] * 7,
        "nested_reps": [len(placebo)] * 7,
        "quantile": ["min", "5%", "25%", "50%", "75%", "95%", "max"],
        "delta_vs_baseline": placebo_q,
    })

    # E按锁定网格回溯，但其规则由2026-08案例形成且有效警报episode仅1，不准入冠军。
    hot_raw = sm.flee_hot_days(engines["AG"], ratio_raw)
    e_windows = sm.flee_suppress_windows(engines["AG"], ratio_raw)
    merged_windows: list[list[pd.Timestamp]] = []
    for start, end in sorted(e_windows):
        if not merged_windows or start > merged_windows[-1][1]:
            merged_windows.append([start, end])
        else:
            merged_windows[-1][1] = max(merged_windows[-1][1], end)
    e_episode_n = len(merged_windows)
    e_rows = []
    for spec in templates["E"]:
        fixed = {y: spec for y in pure_years}
        outcomes = oos_outcomes(fixed, cache_strict, baseline, ledger, markets, pure_years)
        m = outcome_metrics(outcomes)
        a = annual_slices(outcomes, pure_years)
        lifetime = cache_strict[spec.key]
        direct = lifetime[lifetime["status"].str.contains("E信号", na=False)]
        e_rows.append({"spec": spec.key, "period": f"{min(pure_years)}-{max(pure_years)}",
                       "n_fixed_ids": m["n"], "delta": m["delta"],
                       "non_loss_years": int(a["non_loss"].sum()), "year_slices": len(a),
                       "full_lifetime_direct_exits": len(direct),
                       "direct_exit_dates": direct["exit_date"].nunique(),
                       "effective_episodes": e_episode_n,
                       "eligibility": "episode=1且金银比时点映射未通过；不采信"})
    e_table = pd.DataFrame(e_rows)

    first_hot_i = engines["AG"].dates.get_loc(hot_raw[hot_raw].index[0])
    ratio_time_table = pd.DataFrame([
        {"run": "冻结引擎原样调用", "mapping": "美东/沪市同名日asof（源码实际）",
         "period": f"{engines['AG'].dates[0].date()}~{REPORT_END.date()}",
         "hot_dates": ",".join(map(str, hot_raw[hot_raw].index.date)),
         "first_T_plus_1_exit": str(engines["AG"].dates[first_hot_i + 1].date()),
         "time_gate": "未通过；引擎无回执所称映射函数，E不准入"},
    ])

    d_coverage_table = pd.DataFrame([
        {"market": code, "period": f"{markets[code].dates[0].date()}~{REPORT_END.date()}",
         "days": len(markets[code].dates),
         "strict_complete_net_days": int(np.isfinite(markets[code].net_strict_current).sum()),
         "strict_peak60_days": int(np.isfinite(markets[code].net_strict_peak60).sum()),
         "archive_complete_net_days": int(np.isfinite(markets[code].net_inclusive).sum())}
        for code in ("AU", "AG")
    ])

    price_summary = pd.DataFrame([{
        "path": "含G/H的99个准入规格全局WF（E/D组合另作不足样本诊断）",
        "period": f"{min(price_years)}-{max(price_years)}", "n": price_m["n"],
        "candidate_mean": price_m["candidate_mean"], "baseline_mean": price_m["baseline_mean"],
        "delta": price_m["delta"], "non_loss_years": int(price_annual["non_loss"].sum()),
        "year_slices": len(price_annual), "recommendation": "仅呈报，不推荐",
    }])

    # 准入纯席位规格的全样本top10：固定116个ID、共同08-13盯市，仅附录披露。
    full_rows = []
    for spec in pure_eligible:
        paired = paired_asof(cache_strict[spec.key], baseline, ledger, markets, END_EXCLUSIVE)
        m = outcome_metrics(paired)
        full_rows.append({"spec": spec.key, "period": f"{ledger['entry_date'].min().date()}~{REPORT_END.date()}",
                          "n": m["n"], "candidate_mean": m["candidate_mean"],
                          "baseline_mean": m["baseline_mean"], "delta": m["delta"],
                          "warning": "全样本后验，仅附录；不参与选模"})
    full_top = pd.DataFrame(full_rows).sort_values("delta", ascending=False).head(10)

    data_rows = []
    inferred_counts: dict[str, int] = {}
    for name in required:
        path = DATA / name
        df = pd.read_csv(path)
        date_col = "trade_date" if "trade_date" in df else "date"
        if name in {"au_seat.csv.gz", "ag_seat.csv.gz"}:
            inferred_counts[name[:2].upper()] = int(
                (df["source"].astype(str) == "reboard_inferred").sum())
        data_rows.append({"file": name, "period": f"{df[date_col].min()}~{df[date_col].max()}",
                          "rows": len(df), "sha256_prefix16": sha256(path)[:16]})
    data_table = pd.DataFrame(data_rows)

    vis = pd.read_csv(DATA / "reboard_visibility.csv.gz",
                      parse_dates=["trade_date", "reboard_date"])
    vis_keys = ["instrument", "contract", "rank_type", "member",
                "trade_date", "source"]
    visibility_rows = []
    for code in ("AU", "AG"):
        v = vis[vis["instrument"].astype(str).str.upper() == code]
        visibility_rows.append({
            "market": code,
            "period_D": f"{v['trade_date'].min().date()}~{v['trade_date'].max().date()}",
            "inferred_seat_rows": inferred_counts[code],
            "visibility_rows": len(v),
            "null_R": int(v["reboard_date"].isna().sum()),
            "duplicate_keys": int(v.duplicated(vis_keys).sum()),
            "D_before_R": int((v["trade_date"] < v["reboard_date"]).sum()),
            "one_to_one": bool(len(v) == inferred_counts[code]
                               and not v["reboard_date"].isna().any()
                               and not v.duplicated(vis_keys).any()),
        })
    visibility_table = pd.DataFrame(visibility_rows)
    if not (visibility_table["one_to_one"].all()
            and (visibility_table["D_before_R"] == visibility_table["visibility_rows"]).all()):
        raise AssertionError("reboard_visibility 不是完整的一一D<R映射")

    gate_table = pd.DataFrame([
        {"gate": "OOS赢基线", "period": f"{min(pure_years)}-{max(pure_years)}",
         "n": champ_m["n"], "value": champ_m["delta"], "pass": oos_win},
        {"gate": "OOS笔数>=30", "period": f"{min(pure_years)}-{max(pure_years)}",
         "n": champ_m["n"], "value": champ_m["n"], "pass": sample_pass},
        {"gate": ">=2/3年度不输", "period": f"{min(pure_years)}-{max(pure_years)}",
         "n": len(annual), "value": annual["non_loss"].mean() if len(annual) else np.nan, "pass": year_pass},
        {"gate": "超过嵌套安慰剂95分位且p<0.05", "period": f"{min(pure_years)}-{max(pure_years)}",
         "n": len(placebo), "value": placebo_p, "pass": placebo_pass},
        {"gate": "参数±1档方向不反转", "period": f"{min(pure_years)}-{max(pure_years)}",
         "n": len(applicable_sens), "value": applicable_sens.astype(bool).mean() if len(applicable_sens) else np.nan,
         "pass": sensitivity_pass},
        {"gate": "年度块bootstrap 95%下界>0", "period": f"{min(pure_years)}-{max(pure_years)}",
         "n": 10000, "value": ci_lo, "pass": ci_lo > 0},
        {"gate": "E单案例未进入冠军", "period": f"{min(pure_years)}-{max(pure_years)}",
         "n": e_episode_n, "value": e_episode_n,
         "pass": all("E" not in s.families for s in champion_selected.values())},
    ])

    needed_years = math.ceil(len(annual) * 2 / 3) if len(annual) else 0
    lines = [
        "# 机构资金离场规则寻优报告 v2（AU、AG）",
        "",
        f"**结论：{verdict}。** {verdict_text}",
        "",
        f"样本截止 2026-08-13；固定进场账本116笔；严格OOS期 {min(pure_years)}-{max(pure_years)}、"
        f"n={champ_m['n']}；随机种子 `{args.seed}`；嵌套安慰剂 n={len(placebo)}。",
        "",
        f"J单族因果WF诊断为 `{fmt_value(j_diag['delta'])}` 个百分点，"
        f"仅 `{int(j_diag['selected_nonbase_years'])}/{int(j_diag['year_slices'])}` 年选择非BASE，"
        f"且低于 `{TIE_EPS}` 个百分点近似打平带；依“近似打平取更简单规则”不判为边际改进。",
        "",
        "> 纠错说明：首轮程序曾把同一个2026警报对多年重叠持仓的平仓重复当作独立证据。"
        "本报告已撤销该结果，改用固定ID、年度共同截止盯市、全网格年度选择和年度块统计；"
        "E只作episode=1且时间门未通过的机械检查。",
        "",
        "## 1. 边界、数据与可复核性",
        "",
        "- 冻结进场账本只由 `engine/smart_money.py` 对本地CSV只读重放生成；候选离场不反向改变主信号、中继或警报压制后的进场。",
        "- 未连接生产库，未修改 `engine/`；本工单新增文件只在 `research/`。",
        "- 输入文件、覆盖期、行数和哈希如下；08-14尚未完成的数据未等待、未外找。",
        "",
        md_table(data_table),
        "",
        "## 2. 预注册网格 vs 实测",
        "",
        "所有单族和两族笛卡尔积均实际重放；两族按先触发先退出。BASE只作零差回退，不计为第646个候选。"
        "未加入最大持有期、目标收益、MA、额外阈值或案例驱动维度。",
        "",
        md_table(grid_table),
        "",
        "C/F的底层事件定义固定继承生产事件口径：250交易日滚动绝对流量80分位、至少120个历史观测、阈值shift(1)、"
        "强度上限3，并要求目标腿方向及主导腿一致；这些是冻结事件定义，不参与搜索。F的比价腿识别来自 `v52_pair.py`，"
        "国泰/华泰/海通固定0.5计数则来自既有 `REPORT_AU_v1.md` 结论与引擎 `spread_seats`，并非声称由v52单文件给出。",
        "",
        "## 3. 基线复现与冻结账本",
        "",
        f"基线独立重放总账本116笔：114/114已平交易的出场日与收益逐笔一致，另2/2持仓的NaT状态及08-13 MTM一致；"
        f"总错配 `{len(mismatch)}` 笔。收益沿用引擎原口径，未另扣手续费。",
        "",
        md_table(base_table),
        "",
        "逐年执行笔数（year即样本切片，均截至2026-08-13）：",
        "",
        md_table(base_years),
        "",
        "08-13本地只读重放持仓核：",
        "",
        md_table(current_table),
        "",
        f"生产状态对账限制：仓库现有 `engine/web/signals.json` 的 `data_date={saved.get('data_date', '缺失')}`，"
        "不是08-13。因此本地08-13状态已复算；现有文件仅核对到日期，未做状态身份比较，"
        "未提供的生产08-13文件不能被伪称逐字段一致；此验收点保持未勾选。",
        "",
        "## 4. 纯席位主赛道：严格点时逐年 walk-forward",
        "",
        f"每年1月1日在402个准入纯席位规格上只选择一次；训练固定此前全部进场ID，已退出取实现收益、未退出取截止日前末收盯市。"
        f"训练分=`mean(候选-BASE)-SE`，至少{MIN_TRAIN}笔；若最好保守分不超过BASE `{TIE_EPS}` 个百分点则回退BASE。"
        "年度Y只评价Y年进场ID，年末统一盯市（2026截至08-13）；因此主OOS是一年期共同截止快照，"
        "不是把早年持仓拿到2026才评价，也没有按OOS再选模板。",
        "",
        "年度冻结路径（每个训练数字均带训练期和n）：",
        "",
        md_table(picks),
        "",
        "单族WF诊断按预注册顺序列示，仅用于看方向，不据其OOS排序挑冠军：",
        "",
        md_table(family_table),
        "",
        "唯一全局WF路径的逐年OOS切片：",
        "",
        md_table(annual),
        "",
        "各年度行仅用于“至少2/3年度方向不输”硬门；单年n均小于30，不能作为独立年度结论。",
        "",
        "分市场核验（同一OOS期、同一固定ID口径）：",
        "",
        md_table(markets_table),
        "",
        "同一年度冻结参数另作全生命周期诊断：同一74个OOS进场ID统一估值到08-13。"
        "这项右删失诊断不替代年度共同截止硬门，只核验阴性方向是否依赖一年期窗口：",
        "",
        md_table(lifetime_summary),
        "",
        md_table(lifetime_annual),
        "",
        f"年度门要求至少 {needed_years}/{len(annual)} 年不输；年度块bootstrap 95% CI="
        f"[{fmt_value(ci_lo)}, {fmt_value(ci_hi)}]个百分点（期={min(pure_years)}-{max(pure_years)}，n={champ_m['n']}，10000次）。",
        "",
        md_table(gate_table),
        "",
        "## 5. E警报：仅历史自洽，禁止统计准入",
        "",
        "E及其组合共74个规格均完成回放，但该规则在生产源码中明确是2026-08案例驱动；冻结账本期内只有一个结构警报episode。"
        "把多年重叠持仓在同一警报日的退出数当作独立样本，会制造伪重复。且生产源码没有回执所述的"
        "美东日→沪市可见日映射，E还未通过时间有效性门。本表保留引擎原样机械结果，但有效事件n=1，一律标样本不足、不采信。",
        "",
        md_table(e_table),
        "",
        "## 6. 严格点时双跑与时间映射审计",
        "",
        "`reboard_inferred`严格版在决策日T只允许 `reboard_date<=T` 的历史D事实进入当时快照；最终可见版把全部推断行放回D。"
        "冻结引擎本身过滤推断行，所以BASE双跑相同。C/F只在dlong与dshort双腿均可见时形成新事件；D任一席位任一腿缺失即把总净仓保留为未知。",
        "",
        md_table(visibility_table),
        "",
        md_table(pit_table),
        "",
        "D族沿用前次60交易日峰值（min20）口径。完整观测覆盖如下；覆盖不足时不把未知补0，也不触发D：",
        "",
        md_table(d_coverage_table),
        "",
        "金银比源码审计发现：引擎函数只做同名日 `asof`，并没有回执所述的美东日→下一沪市交易日索引转换。"
        "遵守“不重写映射”的裁定，本研究只原样调用引擎与缓存，不自建替代函数；因此E的时间门明确未通过并从冠军宇宙排除。"
        "冻结进场账本仍严格按用户裁定取引擎只读结果；主结论的402个准入规格均不消费金银比。",
        "",
        md_table(ratio_time_table),
        "",
        "## 7. 嵌套安慰剂检验",
        "",
        "每次重复对底层逐席位事件平移±5~10个交易日；同一市场/方向/席位/日期的经济事实跨A/B/C/F共用一个偏移，"
        "随后重新计算A/B的权重与score、C/F的M日家数，重放402个准入纯席位规格，并完整重做年度全局选择与BASE回退。"
        "E虽可机械重建，但因episode=1且时间门未过，不进入这402个规格或冠军安慰剂分布。"
        "因此分布同时校正事件日期、参数搜索和规则族选择。",
        "",
        md_table(placebo_table),
        "",
        f"真实OOS差值 `{fmt_value(champ_m['delta'])}`；安慰剂95分位 `{fmt_value(placebo_q[5])}`；"
        f"经验p=`{fmt_value(placebo_p)}`（n={len(placebo)}，种子={args.seed}）；通过={fmt_value(placebo_pass)}。",
        "",
        "## 8. 参数敏感性",
        "",
        "对真实年度路径中实际被选中的参数逐维整体移动到相邻预注册档；处在网格边界且无相邻档的方向明确列N/A，不创造新数值。",
        "",
        md_table(sens),
        "",
        "## 9. F分市场与含价格族",
        "",
        "F使用双腿change均可见的净流分类；v52识别跨品种比价腿，既有 `REPORT_AU_v1.md` 与引擎 `spread_seats`"
        "固定国泰、华泰、海通折半，高盛不折半。"
        "以下是F单族四格自己的因果WF路径，AU与AG分开列示，不能用合并成绩遮蔽结构差异：",
        "",
        md_table(f_market_table),
        "",
        "含G/H的价格族单独全局WF，只呈报、不推荐：",
        "",
        md_table(price_summary),
        "",
        md_table(price_annual),
        "",
        "价格族逐年行同样只用于年度方向门；单年n<30，均标作样本不足，不形成单年结论。",
        "",
        "## 10. 反时间幻觉逐条自证",
        "",
        "- [x] 席位T日盘后才可得；席位离场在T收盘确认，最早T+1开盘执行。C/D/E/F允许进场日收盘确认、下一开盘卖出，不偷加冷静期。",
        "- [x] 事件阈值使用历史滚动并 `shift(1)`；年度权重只看既往已实现事件；训练/盯市均截在当年决策日，没有全期分位参与当时决策。",
        "- [x] `reboard_inferred` 完成最终可见与严格R日可见双跑；R到来只更新当时视图，不回写过去已发决策。",
        "- [x] 新离场C/F要求双腿change可见，D要求八家双腿持仓完整；掉榜与缺腿保持未知。冻结进场内部旧口径不改。",
        "- [ ] 金银比映射：回执称引擎已有美东日→沪市可见日映射，但源码实际只有同名日asof。按裁定不重写；E因此不准入，未用于主结论。",
        "- [x] 高盛2023-08前仅成交量榜的缺席不生成持仓事件；未解释成减多、增空或离场。",
        "",
        "## 11. 反过拟合纪律与验收清单",
        "",
        "- [x] 年度选择只用Y-1年末前可见的固定ID收益；OOS不参与规则族/模板选择。",
        f"- [x] 披露静态规格645个（纯534、价格111）；E相关74按episode=1、D相关74按有效峰值n=0照测但不准入。年度不输={int(annual['non_loss'].sum())}/{len(annual)}。",
        f"- [{'x' if sample_pass else ' '}] 严格OOS固定ID笔数={champ_m['n']}（期={min(pure_years)}-{max(pure_years)}；硬门30）。",
        f"- [{'x' if placebo_pass else ' '}] 固定种子嵌套安慰剂n={len(placebo)}；真实值严格超过95分位且经验p<0.05。",
        f"- [{'x' if sensitivity_pass else ' '}] 所有可用的参数±1相邻档方向不反转；边界方向列N/A。",
        "- [x] 2026警报案例未用于调参、显著性或推荐，只保留时间门未通过的机械诊断。",
        "- [x] 固定随机种子与一条完整复现命令见下。",
        "- [x] 未修改 `engine/`，未连接生产库，产物仅在 `research/`。",
        "- [ ] 生产08-13 `signals.json` 未在工作区提供；只完成本地08-13状态核，现有08-07文件仅核对到日期。",
        "",
        "## 12. 复现命令",
        "",
        "```powershell",
        ("& 'C:\\Users\\a6366\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' "
         f"-B research/run_exits2.py --seed {args.seed} --placebos {args.placebos}"),
        "```",
        "",
        "## 附录A：准入纯席位规格全样本Top10（后验，仅披露）",
        "",
        "此表只在402个准入纯席位规格中排序，对116个固定ID统一估值到2026-08-13；它不参与主结论。"
        "D/E不足样本机械结果不混入排名，分别见第5、6节。",
        "",
        md_table(full_top),
        "",
        "## 附录B：执行语义",
        "",
        "- 除J改写硬止损外，各候选默认保留4%硬止损。单族就是该族退出+硬止损；J是纯风控维度，J单族保留现行A10；E单族及E+J也在A10上新增直接警报动作。H按注册定义显式包含A10+追踪。",
        "- A/B/H沿用进场后三根K线消退宽限；C/D/E/F可在进场日盘后确认并于下一交易日开盘执行。两族信号先到先出。",
        "- G追踪与J保本使用前一日已知峰值；进场日整根high不用于抬线，避免区间回踩成交前后的日内顺序幻觉。保护价统一取硬止损、保本线、前日追踪线的最大值。",
        "- 日线止损成交沿用生产引擎约定：一旦low穿线即按保护线成交，未模拟开盘跳空滑点。该约定会对J/G/H形成偏乐观上界；价格族只呈报不推荐，纯席位主路径在此上界下仍未赢基线。",
        "- 主比较不删除未退出笔：每个年度切片对候选与BASE使用完全相同的进场ID和估值日；未退出按共同截止收盘MTM。",
        "- D=八家完整可见总净仓相对60交易日峰值回落；C/F的双腿change有一腿缺失就不分类，未知永远不补0。",
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"[done] {REPORT}")
    print(f"[verdict] {verdict}: {verdict_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
