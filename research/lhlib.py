"""合计流向类品种(生猪 LH、玻璃 FG…)的共用装载与口径。

与黄金那套(aulib/smart_money)刻意分开写,因为**这些品种的合约不能当成一个东西**:
生猪各合约相对主力偏离平均 4~12%、最大 49%,玻璃 6.6%/25.8%(黄金不到 1%),
把跨合约手数相加或者用一条复权连续价代表整个品种,算出来的成本与收益都是错的。

所以这里一律**逐合约**:席位持仓按 (member, contract, date) 组织,收益按各合约
自己的结算价算。

**品种参数化**(2026-08-19 加玻璃时改的):原来生猪专用,文件名和点值写死。
加第二个品种时若复制一份 fglib.py,两边就会各自演化——同一套口径两处维护,
必然分叉。改成传 `code`,默认仍是 LH,老脚本一行不用改。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"

# 与 Rust MEMBER_ALIASES / engine RULES["alias"] 同集。不归一会把一家算成两家。
ALIAS = {
    "浙江永安": "永安期货", "乾坤期货": "高盛期货", "上海东证": "东证期货",
    "国投安信": "国投期货", "国投安信期货": "国投期货", "申银万国": "申万期货",
    "格林大华期货": "格林大华", "格林期货": "格林大华",
}

# 席位来源可信度,与 SEAT_SOURCE_RANK 同序。同一天同一合约多源并存时按此收敛,
# 不收敛会把同一家的持仓加两遍(PITFALLS 四·多源重复行灌水)。
SEAT_SOURCE_RANK = {"akshare_v1": 1, "eastmoney_seats_v1": 2, "sanhe": 3}
PRICE_SOURCE_RANK = {"akshare_v1": 1, "eastmoney_seats_v1": 2, "sina_v1": 3}

# 各品种点值,与库里 instruments.price_multiplier 一致。加品种时在这里补一行。
MULTIPLIERS = {"LH": 16.0, "FG": 20.0, "SA": 20.0, "JD": 10.0, "JM": 60.0}
MULTIPLIER = MULTIPLIERS["LH"]   # 兼容老脚本(run_lh_* 直接引它)


def multiplier(code: str = "LH") -> float:
    return MULTIPLIERS[code]


def _rank(series: pd.Series, table: dict) -> pd.Series:
    out = series.map(table)
    # official 一律 0：来源名里带 _official 的都算官方(与 SQL 侧 like '%_official%' 同义)
    out = out.where(~series.str.contains("_official", na=False), 0)
    return out.fillna(4)


def load_price(code: str = "LH") -> pd.DataFrame:
    """逐合约日行情。多源按可信度收敛成一行。

    取价口径与生产一致:`close` 用 `coalesce(nullif(close_price,0), settlement_price)`
    ——郑商所对无成交合约写收盘价 0(DEC-073),直接用会造出假极值。大商所未见此病,
    但口径统一没有坏处。
    """
    df = pd.read_csv(DATA / f"{code.lower()}_price.csv.gz", parse_dates=["trade_date"])
    df["_r"] = _rank(df["source"], PRICE_SOURCE_RANK)
    df = (df.sort_values(["contract", "trade_date", "_r", "source"])
            .drop_duplicates(["contract", "trade_date"], keep="first")
            .drop(columns="_r"))
    close = df["close_price"].replace(0, np.nan)
    df["px"] = close.fillna(df["settlement_price"])
    df["settle"] = df["settlement_price"].replace(0, np.nan)
    return df[df["settle"].notna()].reset_index(drop=True)


def load_seat(code: str = "LH") -> pd.DataFrame:
    """逐 (会员, 合约, 日) 的多空持仓与增减。

    - 会员名归一(剥「(代客)」括号 + 别名表),否则一家算成两家。
    - 只取 long/short 榜,**丢掉成交量榜**(它不是持仓)。
    - 丢掉 `is_variety_total` 行:本研究逐合约做,汇总行既推不出合约也分不出腿。
    - 掉榜 = 没有这一行 = **未知**,这里不补零(补零会让成本与事件全错)。
    """
    df = pd.read_csv(DATA / f"{code.lower()}_seat.csv.gz", parse_dates=["trade_date"])
    # PG boolean 经 CSV 是 't'/'f';astype(bool) 会把 'f' 判成 True(PITFALLS 四)
    df["is_variety_total"] = df["is_variety_total"].isin(["t", "true", "1", True])
    df = df[(~df["is_variety_total"]) & df["rank_type"].isin(["long", "short"])
            & df["contract"].notna()].copy()

    key = df["member"].str.replace(r"[（(][^）)]*[）)]$", "", regex=True)
    df["member_key"] = key.map(lambda m: ALIAS.get(m, m))

    df["_r"] = _rank(df["source"], SEAT_SOURCE_RANK)
    df = (df.sort_values(["trade_date", "contract", "rank_type", "member_key", "_r", "source"])
            .drop_duplicates(["trade_date", "contract", "rank_type", "member_key"], keep="first"))

    wide = df.pivot_table(index=["member_key", "contract", "trade_date"],
                          columns="rank_type", values=["quantity", "change"],
                          aggfunc="sum")
    out = pd.DataFrame(index=wide.index)
    for field, leg, col in [("quantity", "long", "long_q"), ("quantity", "short", "short_q"),
                            ("change", "long", "dlong"), ("change", "short", "dshort")]:
        out[col] = wide[field][leg] if leg in wide[field].columns else np.nan
    out["net"] = out["long_q"].fillna(0) - out["short_q"].fillna(0)
    # 增减必须用交易所公布的 change 聚合，不能用相邻两日 net 相减——
    # 掉榜再回榜时相减会造出假跳变(SMART_MONEY_DESIGN §2)。
    out["dnet"] = out["dlong"].fillna(0) - out["dshort"].fillna(0)
    return out.reset_index()


def main_contract(price: pd.DataFrame) -> pd.DataFrame:
    """当日 OI 最大者为主力,次日生效,只向更远月切换(不回切)。与引擎同规则。"""
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


def forward_returns(price: pd.DataFrame, horizons=(5, 10, 20)) -> pd.DataFrame:
    """**逐合约**的前向收益率,按各合约自己的结算价。

    绝不跨合约算:LH 各合约偏离主力最大 49%,跨合约相除得到的不是收益,
    是价差。这是生猪与黄金最根本的差别。
    """
    p = price.sort_values(["contract", "trade_date"]).copy()
    g = p.groupby("contract")["settle"]
    for h in horizons:
        p[f"fwd{h}"] = g.shift(-h) / p["settle"] - 1.0
    return p
