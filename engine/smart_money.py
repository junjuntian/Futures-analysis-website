# -*- coding: utf-8 -*-
"""机构资金信号引擎(金银)。

设计原则:
- **无状态全量重放**。每次运行从历史起点重算,得出当前持仓与信号状态。
  幂等、可重跑、可断点补算;不依赖状态文件,不会因一次漏跑而漂移。
- 规则来自 research/REPORT_AU_v1.md 已验证结论,参数集中在 RULES,不散落代码。
- T+1 时序:席位数据 15:00 后可得,信号在收盘后产生,买卖执行均为次日开盘。

规则(2026-08-11 运营者拍板定稿):
  信号组 = 金银双强八家;权重逐年扩窗 t 值(截至上年 12-01 已实现 fwd20),clip[0,5],N<30 记 0
  事件   = 净多增加 且 多头腿主导 且 |ΔNet/OI| ≥ 该席位近 250 上榜日 80 分位(shift1)
  分数   = Σ 权重 × 近 5 日最强事件强度(强度 = |flow|/阈值,封顶 3)
  门槛   = 1.2 × 当年组内最大权重(黄金 2026=6.0,白银自动校准)
  买点   = 分数 ≥ 门槛 且 距 60 日最低收盘 <12% 且 八家合计净多 < 250 日 60 分位
           (国泰君安/东证的事件仅在距低点 <5% 时计分)
  进场   = 触发席位加权建仓成本 ±5 元区间,信号后 10 个交易日内触及则成交,
           否则放弃;成本不可得时走次日开盘市价
  卖点   = 八家连续 10 个交易日无有效事件(收盘确认,次日开盘卖)/ 进场价 -4% 盘中止损
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- 规则参数

RULES = {
    "group8": ["中财期货", "中信期货", "海通期货", "国泰君安",
               "高盛期货", "东证期货", "华泰期货", "国投期货"],
    "alias": {"浙江永安": "永安期货", "乾坤期货": "高盛期货",
              "上海东证": "东证期货", "国投安信": "国投期货"},
    "cond_seats": ["国泰君安", "东证期货"],   # 仅贴低点(<5%)计分
    "spread_seats": ["国泰君安", "华泰期货", "海通期货"],  # 比价腿警示对象
    "event_q": 0.80, "event_window": 250, "event_min_hist": 120,
    "score_window": 5, "strength_cap": 3.0,
    "weight_clip": 5.0, "weight_min_n": 30, "weight_horizon": 20,
    "theta_mult": 1.2,
    "dist_low_days": 60, "dist_low_max": 0.12,
    "netq_window": 250, "netq_max": 0.60,
    "zone_half_width": 5.0, "zone_valid_days": 10,
    "stop_loss": 0.04, "fade_days": 10,
    "replay_start": "2015-01-01",
    "ratio_extreme_low": 48.0,    # 银高估:禁买银 / 配对窗口
    "ratio_warn_low": 55.0,
    "ratio_normal": (55.0, 85.0),
    "ratio_extreme_high": 85.0,   # 银低估:银信号加倍
    "ratio_epic_high": 100.0,
}

MARKETS = {
    "AU": {"name": "黄金 AU", "unit": "元/克", "multiplier": 1000, "decimals": 2},
    "AG": {"name": "白银 AG", "unit": "元/千克", "multiplier": 15, "decimals": 0},
}

CN_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 数据加载

def load_from_pg(instrument: str, container: str, pg_user: str, pg_db: str,
                 tmp_dir: str = "/tmp") -> tuple[pd.DataFrame, pd.DataFrame]:
    """通过 docker exec psql \\copy 导出,避免引擎机额外数据库驱动依赖。"""
    out = {}
    for kind, cols, table in [
        ("price",
         "exchange,instrument,contract,trade_date,open_price,high_price,low_price,"
         "close_price,settlement_price,volume,open_interest,source",
         "price_history"),
        ("seat",
         "instrument,contract,is_variety_total,trade_date,rank_type,member,quantity,change,source",
         "seat_history"),
    ]:
        path = f"{tmp_dir}/_engine_{instrument}_{kind}.csv"
        sql = (f"\\copy (select {cols} from {table} where instrument='{instrument}') "
               f"to '{path}' with (format csv, header true)")
        subprocess.run(["docker", "exec", container, "psql", "-U", pg_user, "-d", pg_db,
                        "-c", sql], check=True, capture_output=True)
        subprocess.run(["docker", "cp", f"{container}:{path}", path],
                       check=True, capture_output=True)
        out[kind] = pd.read_csv(path, parse_dates=["trade_date"])
    return out["price"], out["seat"]


def load_from_csv(data_dir: Path, instrument: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """接受 .csv 或 .csv.gz(生产由宿主机 psql \\copy 导出明文 csv)。"""
    p = instrument.lower()

    def pick(kind: str) -> Path:
        for ext in (".csv.gz", ".csv"):
            f = data_dir / f"{p}_{kind}{ext}"
            if f.exists():
                return f
        raise FileNotFoundError(f"缺少 {data_dir}/{p}_{kind}.csv[.gz]")

    return (pd.read_csv(pick("price"), parse_dates=["trade_date"]),
            pd.read_csv(pick("seat"), parse_dates=["trade_date"]))


def clean_price(price: pd.DataFrame) -> pd.DataFrame:
    """同合约同日多来源去重:交易所官方优先。"""
    price = price.copy()
    price["_pri"] = (~price["source"].str.contains("official", na=False)).astype(int)
    price = (price.sort_values(["contract", "trade_date", "_pri"])
             .drop_duplicates(["contract", "trade_date"], keep="first")
             .drop(columns="_pri").reset_index(drop=True))
    return price


def clean_seat(seat: pd.DataFrame) -> pd.DataFrame:
    """注意:PG 的 boolean 经 csv 导出为 't'/'f';pandas 新版按 str dtype 读入,
    直接 astype(bool) 会把 'f' 也判成 True(非空字符串)。必须显式判定。"""
    seat = seat.copy()
    col = seat["is_variety_total"]
    if col.dtype == bool:
        seat["is_variety_total"] = col
    else:
        seat["is_variety_total"] = (col.astype(str).str.strip().str.lower()
                                    .isin(["t", "true", "1", "yes"]))
    seat["member"] = seat["member"].replace(RULES["alias"])
    # 多来源重复行必须按优先级去重:交易所官方 > 其它源,change 非空 > 空。
    # 实测 akshare_v1 自 2026-07-31 起写入与官方重复的席位行且 change 全为空,
    # 若保留了它,ΔNet 会变 0,当周信号会整体消失。
    is_official = seat["source"].astype(str).str.contains("official", na=False)
    seat["_pri"] = (~is_official).astype(int) * 2 + seat["change"].isna().astype(int)
    keys = ["trade_date", "contract", "is_variety_total", "rank_type", "member"]
    seat = (seat.sort_values(keys + ["_pri"])
            .drop_duplicates(keys, keep="first")
            .drop(columns="_pri")
            .reset_index(drop=True))
    return seat


# ---------------------------------------------------------------- 市场结构

def main_contract(price: pd.DataFrame) -> pd.DataFrame:
    """主力:当日 OI 最大者,次日生效,只向更远月切换(不回切)。"""
    p = price.dropna(subset=["open_interest"])
    idx = p.groupby("trade_date")["open_interest"].idxmax()
    cand = p.loc[idx, ["trade_date", "contract"]].sort_values("trade_date")
    dates, cands = cand["trade_date"].tolist(), cand["contract"].tolist()
    ym = lambda c: c[2:]
    main, cur = [], cands[0]
    for i in range(len(dates)):
        if i > 0 and ym(cands[i - 1]) > ym(cur):
            cur = cands[i - 1]
        main.append(cur)
    return pd.DataFrame({"trade_date": dates, "main": main})


def continuous(price: pd.DataFrame, mc: pd.DataFrame) -> pd.DataFrame:
    """主力连续序列:真实 OHLC + 比例复权价 + 全市场持仓。"""
    px = price.set_index(["contract", "trade_date"]).sort_index()
    fields = {"close": "close_price", "high": "high_price", "low": "low_price",
              "open": "open_price", "settle": "settlement_price"}
    wide = {k: px[v].unstack(0) for k, v in fields.items()}
    oi_total = price.groupby("trade_date")["open_interest"].sum()

    rows, prev_main, factor = [], None, 1.0
    for d, m in zip(mc["trade_date"], mc["main"]):
        get = lambda k: wide[k].at[d, m] if m in wide[k].columns else np.nan
        c, s = get("close"), get("settle")
        c_eff = c if not np.isnan(c) else s
        if prev_main is not None and m != prev_main:
            new_col = wide["close"][m].combine_first(wide["settle"][m])
            old_col = wide["close"][prev_main].combine_first(wide["settle"][prev_main])
            new_prev = new_col.iloc[:new_col.index.get_loc(d)].dropna()
            old_prev = old_col.iloc[:old_col.index.get_loc(d)].dropna()
            if len(new_prev) and len(old_prev):
                factor *= float(new_prev.iloc[-1]) / float(old_prev.iloc[-1])
        rows.append((d, m, c_eff, get("high"), get("low"), get("open"), s,
                     factor, oi_total.get(d, np.nan)))
        prev_main = m
    df = pd.DataFrame(rows, columns=["trade_date", "main", "close", "high", "low",
                                     "open", "settle", "factor", "oi_total"]
                      ).set_index("trade_date")
    f_last = df["factor"].iloc[-1]
    for k in ("close", "high", "low", "open"):
        df[f"adj_{k}"] = df[k] / df["factor"] * f_last
    return df


def member_day(seat: pd.DataFrame) -> pd.DataFrame:
    """(会员, 交易日) 的品种汇总持仓与增减。掉榜=无行(不可知),不补 0。"""
    sub = seat[(~seat["is_variety_total"]) & seat["rank_type"].isin(["long", "short"])]
    g = sub.pivot_table(index=["member", "trade_date"], columns="rank_type",
                        values=["quantity", "change"], aggfunc="sum")
    md = pd.DataFrame(index=g.index)
    for kind, leg, col in [("quantity", "long", "long_q"), ("quantity", "short", "short_q"),
                           ("change", "long", "dlong"), ("change", "short", "dshort")]:
        md[col] = g[kind][leg] if leg in g[kind].columns else np.nan
    md["net"] = md["long_q"].fillna(0) - md["short_q"].fillna(0)
    md["dnet"] = md["dlong"].fillna(0) - md["dshort"].fillna(0)
    return md.reset_index()


# ---------------------------------------------------------------- 事件与权重

def detect_events(md: pd.DataFrame, cont: pd.DataFrame, members: list[str],
                  direction: str = "long") -> pd.DataFrame:
    """direction='long' 增多事件;'short' 增空事件(用于参考标记)。"""
    oi = cont["oi_total"]
    out = []
    for m in members:
        s = md[md["member"] == m].set_index("trade_date").sort_index()
        if len(s) < RULES["event_min_hist"]:
            continue
        flow = (s["dnet"] / oi.reindex(s.index)).dropna()
        thr = (flow.abs().rolling(RULES["event_window"], min_periods=RULES["event_min_hist"])
               .quantile(RULES["event_q"]).shift(1))
        sign_ok = flow > 0 if direction == "long" else flow < 0
        hit = flow[(flow.abs() >= thr) & thr.notna() & sign_ok]
        if not len(hit):
            continue
        sub = s.loc[hit.index]
        dl, ds = sub["dlong"].abs().fillna(0), sub["dshort"].abs().fillna(0)
        dom = dl >= ds if direction == "long" else ds > dl
        idx = sub.index[dom]
        if not len(idx):
            continue
        out.append(pd.DataFrame({
            "member": m, "trade_date": idx,
            "strength": (flow.loc[idx].abs() / thr.loc[idx]).clip(upper=RULES["strength_cap"]).to_numpy(),
            "hands": sub.loc[idx, "dnet"].to_numpy(),
        }))
    return (pd.concat(out, ignore_index=True) if out
            else pd.DataFrame(columns=["member", "trade_date", "strength", "hands"]))


def forward_returns(cont: pd.DataFrame, horizon: int) -> pd.Series:
    """从 T+1 起持有 horizon 日的收益(用于权重评估,无未来函数:仅历史已实现)。"""
    logr = np.log(cont["adj_close"] / cont["adj_close"].shift(1)).fillna(0).to_numpy()
    cum = np.concatenate([[0.0], np.cumsum(logr)])
    n = len(logr)
    v = np.full(n, np.nan)
    hi = np.arange(n) + 1 + horizon
    ok = hi < n
    idx = np.arange(n)[ok]
    v[idx] = np.exp(cum[hi[ok] + 1] - cum[idx + 2]) - 1.0
    return pd.Series(v, index=cont.index)


def yearly_weights(ev: pd.DataFrame, cont: pd.DataFrame, years) -> dict:
    """每年 1 月 1 日重算:截至上年 12-01 已实现事件的 fwd20 t 值,clip[0,5]。"""
    fwd = forward_returns(cont, RULES["weight_horizon"])
    w = {}
    for y in years:
        cutoff = pd.Timestamp(f"{y - 1}-12-01")
        row = {}
        for m in RULES["group8"]:
            dr = fwd.reindex(ev[(ev["member"] == m) & (ev["trade_date"] < cutoff)]["trade_date"]).dropna()
            if len(dr) < RULES["weight_min_n"] or dr.std(ddof=1) == 0:
                row[m] = 0.0
            else:
                t = dr.mean() / dr.std(ddof=1) * np.sqrt(len(dr))
                row[m] = float(np.clip(t, 0, RULES["weight_clip"]))
        w[y] = row
    return w


def seat_cost(md: pd.DataFrame, member: str, settle: pd.Series) -> pd.Series:
    """多头建仓成本:增仓按结算价加权累积,减仓不改均价,翻空重置。
    掉榜期间冻结延续(运营者 2026-08-11 拍板),用 asof 取最后可见值。"""
    s = md[md["member"] == member].set_index("trade_date").sort_index()
    cost, net_prev, out = np.nan, 0.0, {}
    for d, row in s.iterrows():
        net, dnet, px = row["net"], row["dnet"], settle.get(d, np.nan)
        if not np.isnan(px):
            if net > 0 and net_prev <= 0:
                cost = px
            elif net > 0 and dnet > 0 and not np.isnan(cost):
                cost = (cost * (net - dnet) + px * dnet) / net
            if net <= 0:
                cost = np.nan
        out[d] = cost
        net_prev = net
    return pd.Series(out, dtype=float)


# ---------------------------------------------------------------- 信号引擎

@dataclass
class Trade:
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp | None
    entry_px: float | None
    zone: tuple[float, float] | None
    seats: list = field(default_factory=list)
    score: float = 0.0
    exit_date: pd.Timestamp | None = None
    exit_px: float | None = None
    result: str = ""
    ret_pct: float | None = None
    fade_count: int = 0
    stop_px_real: float | None = None
    is_relay: bool = False


class MarketEngine:
    def __init__(self, instrument: str, price: pd.DataFrame, seat: pd.DataFrame):
        self.instrument = instrument
        self.meta = MARKETS[instrument]
        price = clean_price(price)
        seat = clean_seat(seat)
        self.mc = main_contract(price)
        self.cont = continuous(price, self.mc)
        self.md = member_day(seat)
        self.dates = self.cont.index
        self.group = RULES["group8"]

        self.ev_long = detect_events(self.md, self.cont, self.group, "long")
        self.ev_short = detect_events(self.md, self.cont, self.group, "short")
        years = range(self.dates[0].year, self.dates[-1].year + 1)
        self.weights = yearly_weights(self.ev_long, self.cont, years)

        c = self.cont["adj_close"]
        self.dist60 = c / c.rolling(RULES["dist_low_days"]).min() - 1
        sub = self.md[self.md["member"].isin(self.group)]
        netsum = sub.groupby("trade_date")["net"].sum().reindex(self.dates).ffill(limit=10)
        self.netsum = netsum
        self.netq = netsum.rolling(RULES["netq_window"], min_periods=120).rank(pct=True)

        # 条件计分:国泰/东证仅贴低点计入
        ev = self.ev_long.copy()
        ev["dist"] = self.dist60.reindex(ev["trade_date"]).to_numpy()
        self.ev_eff = ev[~(ev["member"].isin(RULES["cond_seats"]) & (ev["dist"] >= 0.05))]

        strong = (self.ev_eff.pivot_table(index="trade_date", columns="member",
                                          values="strength", aggfunc="max")
                  .reindex(self.dates).reindex(columns=self.group))
        wmat = pd.DataFrame({m: [self.weights[d.year].get(m, 0.0) for d in self.dates]
                             for m in self.group}, index=self.dates)
        self.wmat = wmat
        self.score = (strong.fillna(0) * wmat).rolling(RULES["score_window"], min_periods=1).max().sum(axis=1)
        self.theta = RULES["theta_mult"] * wmat.max(axis=1)
        self.active = (strong.notna() & (wmat > 0)).any(axis=1)
        self.fade_run = self.active.rolling(RULES["fade_days"], min_periods=1).sum()

        settle_main = pd.Series(
            [self.cont["settle"].get(d, np.nan) for d in self.dates], index=self.dates)
        self.costs = {m: seat_cost(self.md, m, settle_main) for m in self.group}

    # -------- 触发席位与成本区间
    def trigger_seats(self, d: pd.Timestamp) -> pd.DataFrame:
        w = pd.Timedelta(days=RULES["score_window"] + 3)
        return self.ev_eff[(self.ev_eff["trade_date"] > d - w) & (self.ev_eff["trade_date"] <= d)]

    def quiet_streak(self) -> int:
        """截至最新交易日,八席位连续无有效增多事件的天数(卖出倒计时用)。"""
        n = 0
        for v in self.active.to_numpy()[::-1]:
            if v:
                break
            n += 1
        return n

    def cost_zone(self, d: pd.Timestamp) -> tuple[float, float] | None:
        recent = self.trigger_seats(d)
        num = den = 0.0
        for m in recent["member"].unique():
            wm = self.weights[d.year].get(m, 0.0)
            cv = self.costs[m].asof(d) if len(self.costs[m]) else np.nan
            if wm > 0 and cv == cv:
                num += wm * cv
                den += wm
        if not den:
            return None
        c = num / den
        h = RULES["zone_half_width"]
        # (区间下沿, 买入上限, 机构加权成本):价格 ≤ 上限即可买入,越低越好
        return (c - h, c + h, c)

    # -------- 全量重放
    def replay(self) -> list[Trade]:
        d0 = pd.Timestamp(RULES["replay_start"])
        pos = {d: i for i, d in enumerate(self.dates)}
        lo_r = self.cont["low"].to_numpy()
        op_r = self.cont["open"].to_numpy()
        cl_r = self.cont["close"].to_numpy()
        lo_a = self.cont["adj_low"].to_numpy()
        cl_a = self.cont["adj_close"].to_numpy()
        op_a = self.cont["adj_open"].to_numpy()
        f = self.cont["factor"].to_numpy()
        f_last = f[-1]
        fade = self.fade_run.to_numpy()

        # 首次进场:三条件(分数 + 贴低点 + 机构低仓)。
        full_mask = ((self.score >= self.theta) & (self.theta > 0)
                     & (self.dist60 < RULES["dist_low_max"])
                     & (self.netq < RULES["netq_max"]) & (self.dates >= d0))
        # 中继再进场(2026-08-11 运营者案例驱动,回测全期 +81%→+116%):
        # 消退卖出后,八家再度共振(仅分数门槛)即视为同一轮趋势的延续,
        # 免"贴低点/低仓"两个起点条件;止损出场则趋势被否,回到严格三条件。
        relay_mask = (self.score >= self.theta) & (self.theta > 0) & (self.dates >= d0)
        trades, busy = [], -1
        relay_armed = False
        for d in self.dates:
            i = pos[d]
            if i + 1 >= len(self.dates) or i < busy:
                continue
            is_full = bool(full_mask[d])
            is_relay = (not is_full) and relay_armed and bool(relay_mask[d])
            if not (is_full or is_relay):
                continue
            recent = self.trigger_seats(d)
            # 同一席位窗口内多日有事件时只保留最强的一次,避免重复列出
            agg = (recent.sort_values("strength", ascending=False)
                   .drop_duplicates("member"))
            seats = [{"member": r.member, "strength": round(float(r.strength), 2),
                      "hands": int(r.hands)} for r in agg.itertuples()]
            zone = None if is_relay else self.cost_zone(d)
            i0 = p0r = None
            if zone:
                for j in range(i + 1, min(i + 1 + RULES["zone_valid_days"], len(self.dates))):
                    if j <= busy:
                        break
                    if not np.isnan(lo_r[j]) and lo_r[j] <= zone[1]:
                        p0r = min(op_r[j], zone[1]) if not np.isnan(op_r[j]) else zone[1]
                        i0 = j
                        break
                if i0 is None:
                    trades.append(Trade(d, None, None, zone, seats,
                                        float(self.score[d]), result="未回踩放弃"))
                    continue
            else:
                i0 = i + 1
                p0r = op_r[i0] if not np.isnan(op_r[i0]) else cl_r[i0]
                if np.isnan(p0r):
                    continue
            p0a = p0r * f_last / f[i0]
            stop_a = p0a * (1 - RULES["stop_loss"])
            t = Trade(d, self.dates[i0], float(p0r), zone, seats, float(self.score[d]),
                      is_relay=is_relay)
            fade_from = None
            for j in range(i0, len(self.dates)):
                if np.isnan(lo_a[j]):
                    continue
                if fade_from is not None and j > fade_from:
                    px = op_a[j] if not np.isnan(op_a[j]) else cl_a[j]
                    t.exit_date, t.exit_px = self.dates[j], px * f[j] / f_last
                    t.result, t.ret_pct = "消退卖出", (px / p0a - 1) * 100
                    break
                if lo_a[j] <= stop_a:
                    t.exit_date = self.dates[j]
                    t.exit_px = stop_a * f[j] / f_last
                    t.result, t.ret_pct = "止损", -RULES["stop_loss"] * 100
                    break
                if fade_from is None and j > i0 + 2 and fade[j] == 0:
                    fade_from = j
            if t.exit_date is None:
                last = len(self.dates) - 1
                t.result = "持有中"
                t.ret_pct = (cl_a[last] / p0a - 1) * 100
                t.fade_count = self.quiet_streak()
                t.stop_px_real = stop_a * f[last] / f_last
                busy = last
            else:
                busy = pos[t.exit_date]
            if t.result == "消退卖出":
                relay_armed = True
            elif t.result in ("止损", "持有中"):
                relay_armed = False
            trades.append(t)
        return trades

    # -------- 当前状态
    def snapshot(self, trades: list[Trade]) -> dict:
        last = self.dates[-1]
        holding = next((t for t in trades if t.result == "持有中"), None)
        dec = self.meta["decimals"]
        r = lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), dec)
        conds = {
            "score": {"value": round(float(self.score[last]), 2),
                      "target": round(float(self.theta[last]), 2),
                      "pass": bool(self.score[last] >= self.theta[last])},
            "dist_low": {"value": round(float(self.dist60[last]) * 100, 1),
                         "target": RULES["dist_low_max"] * 100,
                         "pass": bool(self.dist60[last] < RULES["dist_low_max"])},
            "netq": {"value": round(float(self.netq[last]) * 100, 0),
                     "target": RULES["netq_max"] * 100,
                     "pass": bool(self.netq[last] < RULES["netq_max"])},
        }
        zone = self.cost_zone(last)
        return {
            "instrument": self.instrument,
            "name": self.meta["name"], "unit": self.meta["unit"],
            "state": "holding" if holding else "watching",
            "last_close": r(self.cont["close"].iloc[-1]),
            "main_contract": self.cont["main"].iloc[-1],
            "conditions": conds,
            "all_pass": all(c["pass"] for c in conds.values()),
            "prospective_zone": [r(zone[0]), r(zone[1])] if zone else None,
            "prospective_cost": r(zone[2]) if zone else None,
            "position": None if not holding else {
                "entry_date": holding.entry_date.strftime("%Y-%m-%d"),
                "entry_px": r(holding.entry_px),
                "zone": [r(holding.zone[0]), r(holding.zone[1])] if holding.zone else None,
                "inst_cost": r(holding.zone[2]) if holding.zone else None,
                "seats": holding.seats,
                "pnl_pct": round(holding.ret_pct, 2),
                "stop_px": r(holding.stop_px_real),
                "fade_days": holding.fade_count,
                "fade_target": RULES["fade_days"],
                "hold_days": int((self.dates <= last).sum() - (self.dates < holding.entry_date).sum()),
            },
            "weights": {m: round(self.weights[last.year].get(m, 0.0), 2) for m in self.group},
            "theta": round(float(self.theta[last]), 2),
        }


# ---------------------------------------------------------------- 金银比

def _fetch_comex_ratio(since_year: int = 2010) -> pd.Series:
    """COMEX 金/银连续收盘比值。与伦敦现货比值实质等价(实测 2026-01-29 两者同为 46.6),
    优点是可每日自动更新。网络失败时抛出,由调用方回退到缓存。"""
    import time
    import urllib.request

    p1 = int(time.mktime((since_year, 1, 1, 0, 0, 0, 0, 0, 0)))
    p2 = int(time.time())
    legs = {}
    for sym in ("GC=F", "SI=F"):
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
               f"?period1={p1}&period2={p2}&interval=1d")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)["chart"]["result"][0]
        s = pd.Series(d["indicators"]["quote"][0]["close"],
                      index=pd.to_datetime(d["timestamp"], unit="s").normalize()).dropna()
        legs[sym] = s[~s.index.duplicated(keep="last")]
    df = pd.DataFrame({"xau": legs["GC=F"], "xag": legs["SI=F"]}).dropna()
    return (df["xau"] / df["xag"]).sort_index()


def load_ratio(data_dir: Path) -> tuple[pd.Series, str]:
    """金银比序列 + 数据源说明。缓存优先保证可用性,每次运行尝试增量更新。

    阈值 48/55/85/100 来自伦敦现货 2000-2026 共 26 年统计(均值 68.8),
    是绝对水平锚,不随样本期变化。"""
    cache = data_dir / "gold_silver_ratio.csv"
    hist = pd.Series(dtype=float)
    if cache.exists():
        df = pd.read_csv(cache, parse_dates=["date"])
        hist = df.set_index("date")["ratio"].sort_index()
    # 手工导入的伦敦历史(若存在)作为更长基线
    lx, lg = data_dir / "london_xau.csv", data_dir / "london_xag.csv"
    if lx.exists() and lg.exists():
        a = pd.read_csv(lx, parse_dates=["Date"]).set_index("Date")["Close"]
        b = pd.read_csv(lg, parse_dates=["Date"]).set_index("Date")["Close"]
        hist = (a / b).dropna().combine_first(hist).sort_index()
    try:
        fresh = _fetch_comex_ratio()
        merged = fresh.combine_first(hist).sort_index()
        (merged.rename("ratio").rename_axis("date").reset_index()
         .to_csv(cache, index=False))
        return merged, f"COMEX 连续(自动更新至 {merged.index[-1].date()})"
    except Exception as e:                                   # 网络/上游异常
        if len(hist):
            return hist, f"缓存({hist.index[-1].date()},更新失败:{type(e).__name__})"
        raise RuntimeError(f"金银比数据不可用且无缓存:{e}")


def ratio_state(ratio: pd.Series, as_of: pd.Timestamp, source: str = "") -> dict:
    v = float(ratio.asof(as_of))
    lo, hi = RULES["ratio_normal"]
    if v < RULES["ratio_extreme_low"]:
        zone, note = "极端低(银高估)", "禁止白银新买点;配对窗口(多金空银)开启,人工决策"
    elif v < RULES["ratio_warn_low"]:
        zone, note = "偏低", "白银买点减半仓,回归压力"
    elif v <= hi:
        zone, note = "正常", "无环境限制"
    elif v < RULES["ratio_epic_high"]:
        zone, note = "极端高(银低估)", "白银买点仓位加倍(历史回归靠银上涨)"
    else:
        zone, note = "史诗区", "26 年仅两次,均为白银一年翻倍起点"
    pct = float((ratio <= v).mean() * 100)
    return {"value": round(v, 1), "zone": zone, "note": note, "source": source,
            "percentile": round(pct, 1),
            "as_of": str(ratio.index[ratio.index <= as_of][-1].date()),
            "mean": round(float(ratio.mean()), 1),
            "thresholds": {"extreme_low": RULES["ratio_extreme_low"],
                           "extreme_high": RULES["ratio_extreme_high"]}}


# ---------------------------------------------------------------- 参考标记与告警

def cross_marks(eng: MarketEngine, other: MarketEngine, d: pd.Timestamp,
                trigger_members: list[str]) -> dict:
    w = pd.Timedelta(days=RULES["score_window"] + 3)
    o_long = other.ev_eff[(other.ev_eff["trade_date"] > d - w) & (other.ev_eff["trade_date"] <= d)]
    o_short = other.ev_short[(other.ev_short["trade_date"] > d - w) & (other.ev_short["trade_date"] <= d)]
    spread = [m for m in trigger_members
              if m in RULES["spread_seats"] and m in set(o_short["member"])]
    gs = "高盛期货" in trigger_members and "高盛期货" in set(o_short["member"])
    return {
        "cross_resonance": bool(len(o_long)),
        "spread_legs": spread,
        "goldman_combo": bool(gs),
    }


def rare_flip_alerts(eng: MarketEngine, lookback_days: int = 60) -> list[dict]:
    """稀有翻空:席位净仓由长期净多翻净空,或空仓创自身历史极值。"""
    alerts = []
    last = eng.dates[-1]
    for m in eng.group:
        s = eng.md[eng.md["member"] == m].set_index("trade_date")["net"].sort_index()
        if len(s) < 250:
            continue
        recent = s[s.index > last - pd.Timedelta(days=lookback_days)]
        if recent.empty:
            continue
        hist = s[s.index <= last - pd.Timedelta(days=lookback_days)]
        if hist.empty:
            continue
        was_long = (hist.tail(250) > 0).mean() > 0.9
        now_short = recent.iloc[-1] < 0
        extreme = recent.min() <= hist.min()
        big = recent.iloc[-1] <= -8000   # 历史级规模才响:常规翻空对冲(2~5千手)是噪音
        if big and ((was_long and now_short) or (now_short and extreme)):
            alerts.append({
                "type": "rare_flip", "level": "warn",
                "market": eng.instrument,
                "member": m,
                "date": str(recent.idxmin().date()),
                "text": (f"{m} 在{eng.meta['name']}净仓由长期净多翻为净空"
                         f"(当前 {int(recent.iloc[-1]):,} 手,历史极值 {int(hist.min()):,} 手)。"
                         "顶级席位历史性翻向,顶部风险 + 配对窗口提示"),
            })
    return alerts


def flee_alert(au: MarketEngine, ag: MarketEngine, ratio: dict) -> dict | None:
    """主力跑路警报(运营者 2026-08-11 案例驱动,历史两次全中)。

    结构 = 金银比历史极端低(<48,26 年 ~3% 分位)+ 八家近 5 日白银增空共振(>=3 家)。
    可选确认 = 任一核心席位黄金多头大额消失(前日 >=2000 手、次日榜上无名)。
    历史:2011-04(比值 31.7,金榜集体减多增空)→ 两周白银 -28%、黄金 -2%;
         2026-01(比值 46.6,中财等全员空银 + 高盛清金多)→ 银 -30.6%、金 -12%。
    样本仅两次,是结构警报不是统计规则——但错报成本是踏空几天,漏报成本是 -20%+。
    """
    if ratio["value"] >= RULES["ratio_extreme_low"]:
        return None
    d = min(au.dates[-1], ag.dates[-1])
    w = pd.Timedelta(days=8)
    shorters = sorted(set(
        ag.ev_short[(ag.ev_short["trade_date"] > d - w) & (ag.ev_short["trade_date"] <= d)]["member"]))
    if len(shorters) < 3:
        return None
    # 黄金多头大额消失确认(可选,升级警报文案)
    vanished = []
    au_dates = au.dates
    for m in RULES["group8"]:
        s = au.md[au.md["member"] == m].set_index("trade_date")["long_q"].dropna()
        s = s[s.index.isin(au_dates)]
        if len(s) < 2:
            continue
        last_seen, prev_q = s.index[-1], s.iloc[-1]
        i = au_dates.get_loc(last_seen)
        if prev_q >= 2000 and i + 1 < len(au_dates):  # 之后的交易日榜上无名
            vanished.append(f"{m}({int(prev_q):,}手)")
    confirm = f";且 {'、'.join(vanished)} 黄金多头已从榜上消失" if vanished else ""
    return {"type": "flee", "level": "danger", "market": "AU+AG", "date": str(d.date()),
            "text": (f"⚠ 主力跑路警报 — 金银比 {ratio['value']}(历史 {ratio['percentile']}% 分位,"
                     f"极端区)且 {'、'.join(shorters)} 共 {len(shorters)} 家近 5 日集体增空白银{confirm}。"
                     "历史同构两次(2011-05 / 2026-02)白银两周 -28%/-31%:"
                     "**持有的金银多单建议立即离场或大幅收紧止损**;配对窗口(多金空银)开启")}



def historical_alerts(au: MarketEngine, ag: MarketEngine, ratio_s: pd.Series) -> list[dict]:
    """回算全历史的做空侧警报触发段,供历史页展示(运营者 2026-08-11 要求:
    警报响过要留痕,否则 2026-01 那次做空窗口在历史里不可见)。

    单边跟随机构空单已被三次检验否定(增空后银平均+1.25%涨,空单主体是产业
    套保),故做空侧只有环境+共振复合警报,不是逐笔交易信号。
    """
    out = []
    dates = ag.dates
    ratio = pd.Series([ratio_s.asof(d) for d in dates], index=dates)
    # 主力跑路:比值<48 且 5 日内 >=3 家增空白银(连续日合并为段)
    w = pd.Timedelta(days=8)
    hot = []
    for d in dates:
        if not (ratio[d] == ratio[d]) or ratio[d] >= RULES["ratio_extreme_low"]:
            hot.append(False)
            continue
        n = ag.ev_short[(ag.ev_short["trade_date"] > d - w)
                        & (ag.ev_short["trade_date"] <= d)]["member"].nunique()
        hot.append(n >= 3)
    hot = pd.Series(hot, index=dates)
    seg_start = None
    for d, v in hot.items():
        if v and seg_start is None:
            seg_start = d
        elif not v and seg_start is not None:
            out.append({"type": "flee", "label": "主力跑路", "market": "AU+AG",
                        "start": str(seg_start.date()), "end": str(d.date()),
                        "note": f"金银比 {ratio[seg_start]:.1f} 极端区 + ≥3 家集体空银;"
                                "多单离场 / 配对(多金空银)窗口"})
            seg_start = None
    if seg_start is not None:
        out.append({"type": "flee", "label": "主力跑路", "market": "AU+AG",
                    "start": str(seg_start.date()), "end": "至今",
                    "note": f"金银比 {ratio[seg_start]:.1f} 极端区 + ≥3 家集体空银"})
    # 稀有翻空不入历史:回算显示该形态 2015-2026 触发 38 次,大多不在顶部
    # (海通/华泰的翻空是常规对冲),与「机构空单被套保污染」的事件研究一致。
    # 可靠的做空窗口指示只有上面的复合警报(比值极端 + 集体空银)。
    out.sort(key=lambda x: x["start"], reverse=True)
    return out


def pair_alert(au: MarketEngine, ag: MarketEngine, ratio: dict) -> dict | None:
    """机构配对形成:比值极端 + ≥3 家同时呈现「一边增多、另一边增空」。"""
    d = min(au.dates[-1], ag.dates[-1])
    w = pd.Timedelta(days=8)
    if ratio["value"] < RULES["ratio_extreme_low"]:
        long_side, short_side, label = au, ag, "多金空银"
    elif ratio["value"] > RULES["ratio_extreme_high"]:
        long_side, short_side, label = ag, au, "多银空金"
    else:
        return None
    lm = set(long_side.ev_long[(long_side.ev_long["trade_date"] > d - w)
                               & (long_side.ev_long["trade_date"] <= d)]["member"])
    sm = set(short_side.ev_short[(short_side.ev_short["trade_date"] > d - w)
                                 & (short_side.ev_short["trade_date"] <= d)]["member"])
    both = sorted(lm & sm)
    if len(both) < 3:
        return None
    return {"type": "pair_window", "level": "info", "market": "AU+AG",
            "date": str(d.date()),
            "text": (f"金银比 {ratio['value']}(历史 {ratio['percentile']}% 分位)且 "
                     f"{'、'.join(both)} 共 {len(both)} 家同现「{label}」结构。"
                     "历史此形态后 60 日配对收益 +18~25%;系统不自动下单,建议分批+宽止损")}


# ---------------------------------------------------------------- 输出

def build_payload(engines: dict, ratio_s: pd.Series, data_date: pd.Timestamp,
                  ratio_source: str = "") -> dict:
    au, ag = engines["AU"], engines["AG"]
    trades = {k: e.replay() for k, e in engines.items()}
    ratio = ratio_state(ratio_s, data_date, ratio_source)

    markets, alerts, history = {}, [], []
    for key, eng in engines.items():
        other = ag if key == "AU" else au
        snap = eng.snapshot(trades[key])
        # 环境调制
        if key == "AG":
            if ratio["value"] < RULES["ratio_extreme_low"]:
                snap["env_block"] = "金银比极端低,白银新买点暂停"
            elif ratio["value"] > RULES["ratio_extreme_high"]:
                snap["env_boost"] = "金银比极端高,白银买点建议加倍仓位"
        markets[key] = snap

        done = [t for t in trades[key] if t.result not in ("未回踩放弃",)]
        for t in trades[key][-12:]:
            marks = cross_marks(eng, other, t.signal_date,
                                [s["member"] for s in t.seats])
            history.append({
                "market": key, "name": eng.meta["name"],
                "signal_date": str(t.signal_date.date()),
                "seats": t.seats, "score": round(t.score, 1),
                "zone": ([round(t.zone[0], eng.meta["decimals"]),
                          round(t.zone[1], eng.meta["decimals"])] if t.zone else None),
                "inst_cost": round(t.zone[2], eng.meta["decimals"]) if t.zone else None,
                "entry_date": str(t.entry_date.date()) if t.entry_date else None,
                "entry_px": round(t.entry_px, eng.meta["decimals"]) if t.entry_px else None,
                "exit_date": str(t.exit_date.date()) if t.exit_date else None,
                "exit_px": round(t.exit_px, eng.meta["decimals"]) if t.exit_px else None,
                "result": t.result,
                "relay": t.is_relay,
                "ret_pct": round(t.ret_pct, 2) if t.ret_pct is not None else None,
                "marks": marks,
            })
        # 当前持仓/待触发的告警
        snap_pos = snap["position"]
        if snap_pos:
            due = snap_pos["fade_days"] >= RULES["fade_days"]
            alerts.append({
                "type": "sell_now" if due else "sell_watch",
                "level": "danger" if due else "warn", "market": key,
                "date": str(data_date.date()),
                "text": (
                    (f"{eng.meta['name']} 多单 — 卖出条件已满足:八席位连续 "
                     f"{snap_pos['fade_days']} 日无增多事件,**下一交易日开盘卖出**")
                    if due else
                    (f"{eng.meta['name']} 多单持有中 — 八席位已连续 "
                     f"{snap_pos['fade_days']}/{RULES['fade_days']} 日无增多事件"
                     f"(满 {RULES['fade_days']} 日后次日开盘卖出);"
                     f"硬止损 {snap_pos['stop_px']} 盘中有效")),
            })
        elif snap["all_pass"]:
            z, c0 = snap["prospective_zone"], snap["prospective_cost"]
            where = (f"限价 ≤ {z[1]}(机构加权成本 {c0},参考区间 {z[0]}~{z[1]}),"
                     f"{RULES['zone_valid_days']} 个交易日内有效"
                     if z else "机构成本不可得,次日开盘市价买入")
            alerts.append({
                "type": "buy", "level": "danger", "market": key,
                "date": str(data_date.date()),
                "text": f"{eng.meta['name']} 买入触发 — {where};止损 -4%",
            })
        alerts.extend(rare_flip_alerts(eng))

    fa = flee_alert(au, ag, ratio)
    if fa:
        alerts.insert(0, fa)   # 最高优先级,置顶
    pa = pair_alert(au, ag, ratio)
    if pa:
        alerts.append(pa)

    # 近期席位动态
    activity = []
    for key, eng in engines.items():
        recent = eng.ev_eff[eng.ev_eff["trade_date"] > eng.dates[-1] - pd.Timedelta(days=21)]
        for r in recent.itertuples():
            activity.append({
                "date": str(r.trade_date.date()), "member": r.member,
                "market": key, "market_name": eng.meta["name"],
                "action": "增多", "strength": round(float(r.strength), 1),
                "weight": round(eng.weights[r.trade_date.year].get(r.member, 0.0), 1),
                "hands": int(r.hands),
            })
    activity.sort(key=lambda x: x["date"], reverse=True)

    stats = {}
    for key in engines:
        done = [t for t in trades[key] if t.ret_pct is not None and t.result != "持有中"]
        mature = [t for t in done if t.signal_date >= pd.Timestamp("2019-01-01")]
        if mature:
            wins = [t for t in mature if t.ret_pct > 0]
            stats[key] = {
                "count": len(mature),
                "win_rate": round(len(wins) / len(mature) * 100, 1),
                "avg": round(float(np.mean([t.ret_pct for t in mature])), 2),
                "total": round(float(np.sum([t.ret_pct for t in mature])), 1),
                "since": "2019",
            }

    return {
        "generated_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": str(data_date.date()),
        "markets": markets,
        "ratio": ratio,
        "alerts": alerts,
        "activity": activity[:20],
        "history": sorted(history, key=lambda x: x["signal_date"], reverse=True)[:20],
        "alert_history": historical_alerts(au, ag, ratio_s),
        "stats": stats,
        "rules": {
            "group": RULES["group8"],
            "buy": "加权增多分数 ≥ 门槛 + 距60日低点<12% + 八席位净仓<60分位 → 机构成本±5元区间挂单(10日有效)",
            "sell": f"八席位连续{RULES['fade_days']}日无增多事件(次日开盘) / 进场价-{int(RULES['stop_loss']*100)}%盘中止损",
            "cond_seats": RULES["cond_seats"],
        },
    }


def main():
    src = os.environ.get("ENGINE_SOURCE", "pg")
    out_path = Path(os.environ.get("ENGINE_OUT", "/opt/futures-platform/signals/signals.json"))
    data_dir = Path(os.environ.get("ENGINE_DATA", Path(__file__).parent / "data"))

    engines = {}
    for inst in ("AU", "AG"):
        if src == "csv":
            price, seat = load_from_csv(Path(os.environ.get("CSV_DIR", "../research/data")), inst)
        else:
            price, seat = load_from_pg(
                inst,
                os.environ.get("PG_CONTAINER", "futures-analysis-platform-postgres-1"),
                os.environ.get("PG_USER", "futures_app"),
                os.environ.get("PG_DB", "futures_platform"))
        engines[inst] = MarketEngine(inst, price, seat)

    ratio, ratio_src = load_ratio(data_dir)
    data_date = min(e.dates[-1] for e in engines.values())
    payload = build_payload(engines, ratio, data_date, ratio_src)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)   # 原子替换,避免前端读到半截文件
    print(f"[engine] {payload['data_date']} 写出 {out_path}")
    for k, m in payload["markets"].items():
        print(f"  {m['name']}: {m['state']} 分数 {m['conditions']['score']['value']}/"
              f"{m['conditions']['score']['target']} 条件 "
              f"{sum(c['pass'] for c in m['conditions'].values())}/3")
    print(f"  金银比 {payload['ratio']['value']} {payload['ratio']['zone']} | "
          f"告警 {len(payload['alerts'])} 条")


if __name__ == "__main__":
    main()
