# -*- coding: utf-8 -*-
"""黄金席位回测:数据地基。

口径(与 docs/SMART_MONEY_DESIGN.md 一致):
- 主力合约:当日 OI 最大,次日生效,不回切(新主力月份必须晚于现主力)。
- 品种日收益:主力合约自身 close/前一日 close,换月日用新主力自己的前收盘,不跨合约。
- 连续价:比例复权(换月日 factor = 新主力昨收/旧主力昨收),仅用于波段检测。
- 席位:别名合并;ΔNet 用交易所公布的 change 聚合,不用相邻日相减。
- 掉榜=不可知,不填 0。
"""
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parent / "data"
OUT = Path(__file__).resolve().parent / "out"

ALIAS = {"浙江永安": "永安期货", "乾坤期货": "高盛期货", "上海东证": "东证期货"}
FOCUS = ["国泰君安", "中信期货", "东证期货", "永安期货", "海通期货", "浙商期货", "中财期货", "高盛期货"]


def load_price() -> pd.DataFrame:
    df = pd.read_csv(DATA / "au_price.csv.gz", parse_dates=["trade_date"])
    df = df.sort_values(["trade_date", "contract"]).reset_index(drop=True)
    return df


def load_seat() -> pd.DataFrame:
    df = pd.read_csv(DATA / "au_seat.csv.gz", parse_dates=["trade_date"])
    for c in ("is_variety_total", "variety_total_is_computed"):
        df[c] = df[c].map({"t": True, "f": False, True: True, False: False}).astype(bool)
    df["member"] = df["member"].replace(ALIAS)
    return df


def main_contract(price: pd.DataFrame) -> pd.DataFrame:
    """主力合约序列:OI 最大 + 次日生效 + 不回切。返回 trade_date -> main 合约。"""
    p = price.dropna(subset=["open_interest"])
    idx = p.groupby("trade_date")["open_interest"].idxmax()
    cand = p.loc[idx, ["trade_date", "contract"]].sort_values("trade_date")
    dates = cand["trade_date"].tolist()
    cands = cand["contract"].tolist()

    def ym(c):  # 'AU2412' -> '2412',2008-2026 内字符串序即时间序
        return c[2:]

    main, cur = [], cands[0]
    for i, d in enumerate(dates):
        if i > 0 and ym(cands[i - 1]) > ym(cur):
            cur = cands[i - 1]  # 昨日候选今日生效,且只向更远月切
        main.append(cur)
    return pd.DataFrame({"trade_date": dates, "main": main})


def continuous_series(price: pd.DataFrame, mc: pd.DataFrame) -> pd.DataFrame:
    """主力连续序列:真实价 + 日收益(同合约) + 比例复权价(波段检测用)。"""
    px = price.set_index(["contract", "trade_date"]).sort_index()
    close = px["close_price"].unstack(0)  # date x contract
    high = px["high_price"].unstack(0)
    low = px["low_price"].unstack(0)
    setl = px["settlement_price"].unstack(0)
    oi_total = price.groupby("trade_date")["open_interest"].sum()

    rows = []
    prev_main = None
    factor = 1.0
    for d, m in zip(mc["trade_date"], mc["main"]):
        c = close.at[d, m] if m in close.columns else np.nan
        h = high.at[d, m] if m in high.columns else np.nan
        lo = low.at[d, m] if m in low.columns else np.nan
        s = setl.at[d, m] if m in setl.columns else np.nan
        # 同合约日收益;当日收盘缺失(无成交)用结算价代
        c_eff = c if not np.isnan(c) else s
        col = close[m].combine_first(setl[m])
        pos = col.index.get_loc(d)
        prev = col.iloc[:pos].dropna()
        r = np.nan
        if len(prev) and not np.isnan(c_eff):
            r = c_eff / prev.iloc[-1] - 1.0
        if prev_main is not None and m != prev_main:
            # 换月日:factor 链上乘 新主力昨收/旧主力昨收
            old_col = close[prev_main].combine_first(setl[prev_main])
            old_prev = old_col.iloc[: old_col.index.get_loc(d)].dropna()
            if len(prev) and len(old_prev):
                factor *= float(prev.iloc[-1]) / float(old_prev.iloc[-1])
        rows.append((d, m, c_eff, h, lo, r, factor, oi_total.get(d, np.nan)))
        prev_main = m
    df = pd.DataFrame(rows, columns=["trade_date", "main", "close", "high", "low", "ret", "factor", "oi_total"])
    # 复权价:除以截至当日的累计因子再乘末端因子,使最新价=真实价
    df["adj_close"] = df["close"] / df["factor"] * df["factor"].iloc[-1]
    df["adj_high"] = df["high"] / df["factor"] * df["factor"].iloc[-1]
    df["adj_low"] = df["low"] / df["factor"] * df["factor"].iloc[-1]
    return df.set_index("trade_date")


def zigzag(cont: pd.DataFrame, threshold: float = 0.10) -> pd.DataFrame:
    """±threshold 波段(峰用 adj_high、谷用 adj_low),与运营者 Excel 同逻辑。"""
    h = cont["adj_high"].to_numpy()
    lo = cont["adj_low"].to_numpy()
    dates = cont.index.to_numpy()
    legs = []
    mode = None  # 'seek_trough' 峰已定找谷 / 'seek_peak' 谷已定找峰
    ext_i = 0
    start_i = 0
    for i in range(1, len(h)):
        if np.isnan(h[i]) or np.isnan(lo[i]):
            continue
        if mode in (None, "seek_trough"):
            if h[i] >= h[ext_i]:
                ext_i = i
            if lo[i] <= h[ext_i] * (1 - threshold):
                legs.append(("down", dates[ext_i], h[ext_i], None, None))
                mode, start_i, ext_i = "seek_peak", ext_i, i
        else:
            if lo[i] <= lo[ext_i]:
                ext_i = i
            if h[i] >= lo[ext_i] * (1 + threshold):
                legs.append(("up", dates[ext_i], lo[ext_i], None, None))
                mode, start_i, ext_i = "seek_trough", ext_i, i
    # 拼成 峰->谷->峰 轮次表
    out = []
    for j in range(len(legs) - 1):
        kind, d0, p0, _, _ = legs[j]
        _, d1, p1, _, _ = legs[j + 1]
        out.append({"type": "下跌" if kind == "down" else "上涨", "from": pd.Timestamp(d0), "to": pd.Timestamp(d1),
                    "from_px": p0, "to_px": p1, "pct": p1 / p0 - 1.0,
                    "days": int((pd.Timestamp(d1) - pd.Timestamp(d0)).days)})
    if legs:
        kind, d0, p0, _, _ = legs[-1]
        out.append({"type": "下跌" if kind == "down" else "上涨", "from": pd.Timestamp(d0), "to": pd.NaT,
                    "from_px": p0, "to_px": np.nan, "pct": np.nan, "days": -1})
    return pd.DataFrame(out)


def member_variety_series(seat: pd.DataFrame, member: str) -> pd.DataFrame:
    """某席位的品种汇总 Net/ΔNet 日序列。

    库内 SHFE 尚无自算汇总行(is_variety_total 全 false,平台侧待补灌),
    这里按 TWO_TABLE_DESIGN 的自算口径直接从逐合约行聚合:
    quantity/change 按 (trade_date, rank_type) 求和;change 用交易所公布值。
    当天买卖两榜都没上 => 无行(掉榜=不可知),不补 0。
    """
    sub = seat[(~seat["is_variety_total"]) & (seat["member"] == member) & seat["rank_type"].isin(["long", "short"])]
    if sub.empty:
        return pd.DataFrame(columns=["long_q", "short_q", "net", "dnet"])
    g = sub.pivot_table(index="trade_date", columns="rank_type", values=["quantity", "change"], aggfunc="sum")
    df = pd.DataFrame(index=g.index)

    def col(name, kind):
        s = g[kind][name] if (kind in g.columns.get_level_values(0) and name in g[kind].columns) else None
        return s if s is not None else pd.Series(0.0, index=g.index)

    df["long_q"] = col("long", "quantity")
    df["short_q"] = col("short", "quantity")
    df["net"] = df["long_q"].fillna(0) - df["short_q"].fillna(0)
    df["dnet"] = col("long", "change").fillna(0) - col("short", "change").fillna(0)
    return df
