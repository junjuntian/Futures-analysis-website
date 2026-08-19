#!/usr/bin/env python3
"""生猪(LH)机构资金引擎:合计流向跟随。

**与金银引擎(smart_money.py)刻意分开的两个文件**,不是重复造轮子——两套信号
形态根本不同,研究阶段用数据判过(research/REPORT_LH_PHASE1_v1.md):

  - 金银:逐家席位算权重、多席位共振。生猪照搬这套会失败——单家席位的加减仓
    事件整体胜率只有 50%(2026 年 44.7%),只有东证一家 t≈2.8 显著。
  - 生猪:**八家合计的流向**,控制动量后偏相关 t=5.4~7.5,与金银核心因子同量级。
  - 生猪还有两条金银没有的坑:各合约相对主力偏离最大 49%(金银不到 1%),
    以及 84.6% 的交易日里同日不同合约的持仓变化方向相反(移仓换月)。

所以口径上有两条铁律,改动时不要想当然:

  1. **信号用品种合计**。拆到合约层面会被移仓撕成相反的两半(实测 IC_t 仅 0.58,
     品种合计 t=5.22)。
  2. **收益一律逐合约算,换月日用新合约自己的前一日结算价**。跨合约相除得到的
     不是收益,是价差。

席位组**滚动重选**(每年按截至当时的历史 alpha 取前 5),不硬编码名单:
生猪只有三年样本、且只有一种市况,焊死名单等于把这一段行情的偏好写死。
金银敢硬编码七家是有 17 年样本兜底。

**只做空**——做多支路默认关闭,理由见 RULES["long_enabled"]。

回测证据(2023-08~2026-08,一年选人 + 只做空 + **次日开盘成交**,DEC-090):
  恒定满仓做空基准 +99.2%/夏普 1.65/回撤 −14.8%
  本引擎            +79.8%/夏普 2.23/回撤  −6.8%,18 笔胜率 61.1%

**必须正视的一件事:绝对收益没跑赢基准**(+79.8% vs +99.2%)。这三年是单边熊市,
躺着满仓做空本身就有 +99% 复利。策略赢的是夏普(2.23 vs 1.65)与回撤(−6.8% vs
−14.8%),以及**趋势反转时会退出而不是硬扛**——而后者在样本内无法验证(没有牛市)。
界面上必须摆出这个对比(payload 的 `compare`),不然看的人会把熊市 beta 当成本事。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

CN_TZ = timezone(timedelta(hours=8))

# 当前正在跑的品种(由 run_one 设置)。build_payload 要用它填品种名与单位。
CURRENT: dict = {}

# 各品种算好的信号表,供 FG-SA 配对信号复用(别再重算一遍)。
SIG_CACHE: dict = {}

COST = 0.0005            # 单边手续费+滑点。逐日净值在成交那两天各扣一次。
SPLIT: dict = {}         # 本轮品种的「跳空占比」,build_payload 算好给 _caveats 用

RULES = {
    # —— 席位组 ——
    "group_k": 5,            # Phase 1:3/5/8 里 5 在三个训练截点上都最好
    # 2026-08-19 运营者拍板由 3 改 12:「3 个月太短,会有很多噪音」。数据支持这个
    # 判断的一半——换组次数 9→2、胜率 58.3%→63.9%、最大回撤 −10.2%→−8.6%;
    # **代价要说清**:离散版累计从 +104.5% 掉到 +86.5%,低于「恒定满仓做空」的 +99.2%。
    # 另外 3/6/12 月三档是 94.2/67.7/77.1,**不单调**,所以这三个数之间的差异
    # 多半是噪音,不能反过来论证 3 个月更好。
    "reselect_months": 12,
    "warmup_days": 250,      # 首次选组前的最少历史
    "member_min_days": 120,  # 一家至少在榜这么多天才参与排名
    # —— 信号 ——
    "sig_win": 5,            # 合计净持仓的变化窗口。20 日窗会混入动量(相关 +0.317)
    "z_win": 120,            # 无量纲化的滚动窗。2026 年机构净空是 2024 年的四倍,
                             # 绝对手数不能直接当阈值
    # —— 进出场 ——
    "enter": 1.0,            # 0.8/1.0/1.2 平稳(94/94/110%),取中不取峰值
    # exit_z=0 意味着「消退出场」要求 z 恰好为 0,**实测三年 36 笔里一次都没触发过**
    # (出场全部是 反向 32 / 持满 2 / 止损 2)。留着是当安全网,不要误以为它在起作用;
    # 真想让信号衰减就出场,得把它调成 0.3 这类值,而那是个**新参数,必须先回测**。
    "exit_z": 0.0,
    "stop": 0.06,            # 4/6/8/10% 相邻档同向,不敏感
    "max_hold": 40,          # 20/30/40/60 相邻档同向
    # **散户交割纪律**(2026-08-19 运营者要求):主力合约进入「窗口止点前 10 个
    # 交易日」就强制平仓,并且**这段时间也不许进场**——不然平了立刻又开,天天空转。
    # 窗口止点 = 交割月前月最后一个非周末日,与套利监控 `days_to_window_end` 同口径。
    # 运营者的原话与算例:「我是散户,玻璃 2609 合约 8.31 之前需要离场,
    # 要提前 10 个交易日,8.18 之前要离场」——FG2609 止点 2026-08-31,
    # 倒数第 10 个交易日(含当日)正是 08-18。
    # 这不是调出来的参数,是纪律,别拿回测去优化它。
    "exit_before_delivery": 10,
    # **做多支路默认关闭**(2026-08-19 运营者拍板)。三条依据:
    #   ① 一年选人口径下多头 15 笔逐笔累计 −1.5%、均值 −0.02%(抛硬币),
    #      还贡献了全表最差的 −7.4%;关掉后夏普 1.96 → 2.39。
    #   ② 运营者本来想跟的是「机构真转多」。样本里它出现过 14 天(集中在
    #      2025-07,最高 +4,046 手),但之后 20 日主力仍平均跌 1.18%,
    #      14 次里最好一次只有 +0.61%——转多意味着跌得慢,不意味着会涨。
    #   ③ 关掉不等于牛市被埋:机构减空时策略平仓观望,不是继续扛空单。
    # **页面仍然显示机构转多状态**(见 payload 的 institution),只是不进场。
    # 那 14 天全挤在一个窗口里,实质是 1 个事件,只能说「没有证据支持」,
    # 不能说「证明必亏」——生猪真走出熊市、样本攒够了再议。
    # long_enabled / multiplier / replay_start 由 use() 按品种注入,见 VARIETIES。
    # 做多开启时才用得上:Phase 0 双分档里「跌 × 机构减空」是全表唯一
    # 正格子(+0.30%),「涨 × 减空」是 −1.97%。
    "long_needs_dip": True,
    "dip_win": 20,
    # 与 Rust MEMBER_ALIASES / smart_money RULES["alias"] 保持同集。
    # 不归一会把一家算成两家。「大华期货」不许加(与格林期货同日同合约并存 266 次,
    # 是 2013 年被吸收合并的另一家公司)。
    "alias": {"浙江永安": "永安期货", "乾坤期货": "高盛期货",
              "上海东证": "东证期货", "国投安信": "国投期货",
              "国投安信期货": "国投期货", "申银万国": "申万期货",
              "格林大华期货": "格林大华", "格林期货": "格林大华"},
    # —— 散户反向维度(2026-08-19 加,DEC-085)——
    # 这三家是运营者定的:在多个品种上长期站多头、长期亏钱的席位。
    # 判据是**散户天然站多头**——一致净空的是套保席位(为交割锁价、不在乎盈亏),
    # 运营者据此点名剔除了格林大华(生猪上净空 2,499 手)。
    # 名单**跨品种固定、不逐品种重选**:这正是它相对「找聪明钱」的优势所在——
    # 没有挑人的过拟合,新品种可直接套用。加人反而变差(实测四家不如三家)。
    "retail_seed": ["东方财富", "平安期货", "徽商期货"],
    # 共振 = 聪明钱流向与散户反向流向同号。
    #
    # **不参与进出场,理由是「两者表现相当」而不是「新信号不够好」**——这两个说法
    # 差很多,别再搞混(2026-08-19 我先用错口径算出「共振碾压现有信号」,又据此
    # 反过来主张切换,是运营者从界面数字对不上把错误揪出来的)。
    #
    # 正确对比(同一时间轴 2023-08 起、各用各的信号进出场,与生产页面对拍过):
    #   现有主信号 21 笔 净 +66.7%/胜率 61.9%/回撤 −12.0%/夏普 1.79
    #   散户反向   21 笔 净 +73.8%/胜率 57.1%/回撤  −8.0%/夏普 1.74
    #   共振进场   18 笔 净 +79.8%/胜率 61.1%/回撤  −6.8%/夏普 2.23
    # **单笔均值差的 t 只有 0.22 / 0.49,在 21 笔样本上分不出高下。**
    #
    # 所以它的定位是**独立的第二意见**(与主信号相关 0.59),不是「更好的信号」:
    # 看两者一致还是背离,比看它自己的方向更有用。共振时回撤明显小一半
    # (−4.1% vs −9.4%),方向一致但尚不显著,值得继续观察。
    # 2026-08-19 运营者拍板改用**方案 C:共振进场 / 散户出场**(只做空)。
    #   进场:聪明钱流向与散户反向流向同号(共振)且散户反向 z ≤ −enter
    #   出场:散户反向信号翻到 +enter(反向)/ 硬止损 / 持满
    # 同一时间轴(2023-08 起)三个方案:
    #   现有主信号 21 笔 净 +66.7%/胜率 61.9%/回撤 −12.0%/夏普 1.79
    #   散户反向   21 笔 净 +73.8%/胜率 57.1%/回撤  −8.0%/夏普 1.74
    #   **方案 C** 18 笔 净 +79.8%/胜率 61.1%/回撤 **−6.8%**/夏普 **2.23**
    # **必须如实记住:三者单笔均值差的 t 只有 0.22~0.49,统计上分不出高下。**
    # 选 C 是运营者的判断(回撤最小、夏普最高),不是数据证明它更优——
    # 别在后续文档里把它写成「实测最优」。
    "signal_source": "resonance",   # "flow"=原聪明钱单信号;"resonance"=方案 C
}

# 品种参数。**每加一个品种,规则要重新验一遍,不许照抄**——
# 生猪只做空(它样本里只有单边熊市,做多支路逐笔累计 −1.5%),而玻璃纯碱双向明显
# 更好(FG 双向夏普 0.58 vs 只做空 0.21;SA 0.78 vs 0.56),因为它们跨了完整周期、
# 做多支路有真实机会。这条差异是实测出来的,不是设计出来的。
VARIETIES = {
    "LH": {
        "name": "生猪 LH", "unit": "元/吨", "multiplier": 16.0,
        "replay_start": "2023-08-11",   # 大商所席位数据起点
        "long_enabled": False,          # DEC-084:多头 15 笔逐笔累计 −1.5%,关掉
        "long_needs_dip": True,   # 做多已关,这条用不上;留 True 是生猪原口径
        "out": "hog_signals.json",
        "backtest": "18 笔 净 +79.8%/胜率 61.1%/回撤 −6.8%/夏普 2.23(2023-08 起)",
    },
    "FG": {
        "name": "玻璃 FG", "unit": "元/吨", "multiplier": 20.0,
        "replay_start": "2013-01-01",   # 郑商所席位 2012-12 起,留一个月预热
        "long_enabled": True,
        # 实测带 dip 反而差:207 笔 夏普 0.58 → 158 笔 0.40(DEC-090 新口径)
        "long_needs_dip": False,
        "out": "fg_signals.json",
        "backtest": "207 笔 净 +385%/胜率 51.7%/回撤 −43.1%/夏普 0.61(2013-01 起)",
    },
    "SA": {
        "name": "纯碱 SA", "unit": "元/吨", "multiplier": 20.0,
        "replay_start": "2020-06-01",   # 席位 2019-12 起,留半年预热
        "long_enabled": True,
        # 带 dip 的夏普几乎一样(0.78 vs 0.75)但**回撤好很多**(−40.1% vs −53.3%),
        # DEC-090 换成次日开盘成交之后才显出来。这一条值得复议,现状是维持不带 dip。
        "long_needs_dip": False,
        "out": "sa_signals.json",
        "backtest": "101 笔 净 +244%/胜率 46.5%/回撤 −53.3%/夏普 0.80(2020-06 起)",
    },
}


def use(code: str) -> dict:
    """把某个品种的参数并进 RULES 供本轮使用。返回该品种的配置。

    引擎按品种逐个跑,每次跑之前调一次——RULES 里那些与品种相关的键
    (点值、回放起点、做多开关)由它覆盖,其余规则三个品种共用。
    """
    v = VARIETIES[code]
    RULES["multiplier"] = v["multiplier"]
    RULES["replay_start"] = v["replay_start"]
    RULES["long_enabled"] = v["long_enabled"]
    RULES["long_needs_dip"] = v["long_needs_dip"]
    return v

SEAT_RANK = {"akshare_v1": 1, "eastmoney_seats_v1": 2, "sanhe": 3}
PRICE_RANK = {"akshare_v1": 1, "eastmoney_seats_v1": 2, "sina_v1": 3}


# ---------------------------------------------------------------- 数据

def _rank(src: pd.Series, table: dict) -> pd.Series:
    out = src.map(table)
    return out.where(~src.str.contains("_official", na=False), 0).fillna(4)


def load_from_pg(code: str, container: str, pg_user: str,
                 pg_db: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    def q(sql: str) -> pd.DataFrame:
        cmd = ["docker", "exec", "-i", container, "psql", "-U", pg_user, "-d", pg_db,
               "-A", "-F", "\t", "--no-align", "-c", sql]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True,
                             encoding="utf-8").stdout
        lines = [l for l in out.splitlines() if l and not l.startswith("(")]
        from io import StringIO
        return pd.read_csv(StringIO("\n".join(lines)), sep="\t")

    price = q("select exchange,instrument,contract,trade_date,open_price,high_price,"
              "low_price,close_price,settlement_price,volume,open_interest,source "
              f"from price_history where instrument='{code}'")
    seat = q("select instrument,contract,is_variety_total,trade_date,rank_type,member,"
             f"quantity,change,source from seat_history where instrument='{code}'")
    return price, seat


def load_from_csv(code: str, csv_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    def one(stem: str) -> pd.DataFrame:
        # 生产链路落的是 .csv(run-smart-money.sh 导出);研究目录存的是 .csv.gz。
        for name in (f"{stem}.csv", f"{stem}.csv.gz"):
            p = csv_dir / name
            if p.exists():
                return pd.read_csv(p)
        raise FileNotFoundError(f"{csv_dir}/{stem}.csv[.gz] 都不存在")
    low = code.lower()
    return one(f"{low}_price"), one(f"{low}_seat")


def clean_price(price: pd.DataFrame) -> pd.DataFrame:
    df = price.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["_r"] = _rank(df["source"].astype(str), PRICE_RANK)
    df = (df.sort_values(["contract", "trade_date", "_r", "source"])
            .drop_duplicates(["contract", "trade_date"], keep="first"))
    # 收盘价 0 是「当天无成交」不是价格(DEC-073),用结算价兜底
    df["px"] = df["close_price"].replace(0, np.nan).fillna(df["settlement_price"])
    df["settle"] = df["settlement_price"].replace(0, np.nan)
    return df[df["settle"].notna()].reset_index(drop=True)


def clean_seat(seat: pd.DataFrame) -> pd.DataFrame:
    df = seat.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    # PG boolean 经 CSV 是 't'/'f';astype(bool) 会把 'f' 判成 True
    df["is_variety_total"] = df["is_variety_total"].astype(str).isin(["t", "true", "True", "1"])
    df = df[(~df["is_variety_total"]) & df["rank_type"].isin(["long", "short"])
            & df["contract"].notna()].copy()
    key = df["member"].astype(str).str.replace(r"[（(][^）)]*[）)]$", "", regex=True)
    df["member_key"] = key.map(lambda m: RULES["alias"].get(m, m))
    df["_r"] = _rank(df["source"].astype(str), SEAT_RANK)
    df = (df.sort_values(["trade_date", "contract", "rank_type", "member_key", "_r", "source"])
            .drop_duplicates(["trade_date", "contract", "rank_type", "member_key"], keep="first"))
    wide = df.pivot_table(index=["member_key", "contract", "trade_date"], columns="rank_type",
                          values="quantity", aggfunc="sum")
    out = pd.DataFrame(index=wide.index)
    out["long_q"] = wide["long"] if "long" in wide.columns else np.nan
    out["short_q"] = wide["short"] if "short" in wide.columns else np.nan
    out["net"] = out["long_q"].fillna(0) - out["short_q"].fillna(0)
    return out.reset_index()


def window_end(contract: str) -> pd.Timestamp:
    """散户可交易窗口的止点 = **交割月前月最后一个非周末日**。

    与套利监控 `last_weekday_before_delivery` / `days_to_window_end` 同口径,
    两个模块对「散户还能拿多久」必须给同一个答案。
    节假日不查表:止点只用来卡纪律,±1~2 天的误差不影响「提前 10 个交易日走」。
    """
    raw = "".join(ch for ch in str(contract) if ch.isdigit())
    yy, mm = 2000 + int(raw[:2]), int(raw[2:])
    d = pd.Timestamp(year=yy, month=mm, day=1) - pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d


def days_to_window_end(contract: str, today: pd.Timestamp) -> int:
    """从**次日**起到窗口止点(含)的工作日数。已过止点给 0。

    从次日起算是因为信号是盘后出的:今天判「剩 10 天」,平仓动作发生在明天。
    """
    end = window_end(contract)
    if end <= today:
        return 0
    return int(np.busday_count((today + pd.Timedelta(days=1)).date(),
                               (end + pd.Timedelta(days=1)).date()))


def main_series(price: pd.DataFrame) -> pd.DataFrame:
    """主力合约与它的逐日收益。**换月日用新主力自己的前一日结算价。**

    这是计价的地基:一旦跨合约相除,换月那天会凭空多出几个百分点的假收益,
    而且不报错。
    """
    p = price.dropna(subset=["open_interest"])
    idx = p.groupby("trade_date")["open_interest"].idxmax()
    cand = p.loc[idx, ["trade_date", "contract"]].sort_values("trade_date")
    dates, cands = cand["trade_date"].tolist(), cand["contract"].tolist()
    ym = lambda c: str(c)[2:]
    main, cur = [], cands[0]
    for i in range(len(dates)):
        if i > 0 and ym(cands[i - 1]) > ym(cur):
            cur = cands[i - 1]
        main.append(cur)

    px = price.set_index(["contract", "trade_date"])["settle"].sort_index()
    # 开盘价:成交口径改成「次日开盘」之后它才是真正的成交价(DEC-090)。
    # 郑商所对无成交合约会写 0(DEC-073),按缺失处理,别当成真价格。
    po = (price.assign(_o=price["open_price"].replace(0, np.nan))
               .set_index(["contract", "trade_date"])["_o"].sort_index())
    rows = []
    for d, c in zip(dates, main):
        s = px.get((c, d), np.nan)
        hist = px.loc[c] if c in px.index.get_level_values(0) else pd.Series(dtype=float)
        earlier = hist[hist.index < d]
        prev = earlier.iloc[-1] if len(earlier) else np.nan
        ret = s / prev - 1.0 if (np.isfinite(s) and np.isfinite(prev) and prev > 0) else np.nan
        o = po.get((c, d), np.nan)
        oh = po.loc[c] if c in po.index.get_level_values(0) else pd.Series(dtype=float)
        oe = oh[oh.index < d]
        oprev = oe.iloc[-1] if len(oe) else np.nan
        # 开→开:换月日照样用**新合约自己的**前一日开盘价,与结算价那条同一纪律。
        ret_o = o / oprev - 1.0 if (np.isfinite(o) and np.isfinite(oprev) and oprev > 0) else np.nan
        # 开→结算:同日同合约,天然安全。用来算「持仓到今天收盘的浮盈」。
        o2c = s / o - 1.0 if (np.isfinite(s) and np.isfinite(o) and o > 0) else np.nan
        rows.append((d, c, s, ret, o, ret_o, o2c))
    out = pd.DataFrame(rows, columns=["trade_date", "main", "settle", "ret",
                                      "open", "ret_open", "o2c"]).set_index("trade_date")
    out["past"] = out["settle"].pct_change(RULES["dip_win"])
    # 每天的主力离自己的窗口止点还有几个交易日 —— 散户交割纪律靠它卡。
    out["dleft"] = [days_to_window_end(c, d) for c, d in zip(out["main"], out.index)]
    return out


# ---------------------------------------------------------------- 席位组

def alpha_upto(seat: pd.DataFrame, price: pd.DataFrame, hi: pd.Timestamp) -> pd.Series:
    """截至 hi(不含)每家的择时收益 alpha = 实际盈亏 − 恒定仓位能赚到的钱。

    **绝不许看 hi 之后的数据**——滚动重选的全部意义就在这里。
    """
    d = seat[seat["trade_date"] < hi].merge(
        price[["contract", "trade_date", "settle"]], on=["contract", "trade_date"], how="inner")
    if d.empty:
        return pd.Series(dtype=float)
    d = d.sort_values(["member_key", "contract", "trade_date"])
    g = d.groupby(["member_key", "contract"])
    d["prev_net"] = g["net"].shift()
    d["prev_settle"] = g["settle"].shift()
    gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
    d = d[d["prev_net"].notna() & (gap <= 5)]
    if d.empty:
        return pd.Series(dtype=float)
    d = d.assign(dpx=(d["settle"] - d["prev_settle"]) * RULES["multiplier"])
    grp = d.groupby("member_key")
    pnl = grp.apply(lambda s: (s["dpx"] * s["prev_net"]).sum(), include_groups=False)
    beta = grp.apply(lambda s: (s["dpx"] * s["prev_net"].mean()).sum(), include_groups=False)
    days = grp["trade_date"].nunique()
    return (pnl - beta)[days >= RULES["member_min_days"]].sort_values(ascending=False)


def rolling_groups(seat: pd.DataFrame, price: pd.DataFrame,
                   dates: pd.DatetimeIndex) -> tuple[pd.Series, list, list]:
    """逐日生效的席位组、**换人**历史、以及全部重选切点。

    第三个返回值是 2026-08-19 加的:`log` 只在**阵容变了**的时候写一条,于是玻璃
    自 2023-10 起三次重选都选中同一批人,界面上就只剩 2023 那条,运营者据此以为
    「席位三年没更新」。切点单独给出来,界面才说得清「重选跑过、只是没换人」。
    """
    start = dates.min() + pd.Timedelta(days=RULES["warmup_days"])
    cuts = pd.date_range(start, dates.max(), freq=f"{RULES['reselect_months']}MS")
    picks, log, cur = {}, [], None
    for cut in cuts:
        a = alpha_upto(seat, price, cut)
        if len(a) >= RULES["group_k"]:
            new = tuple(a.head(RULES["group_k"]).index)
            if new != cur:
                log.append({"date": cut.strftime("%Y-%m-%d"), "members": list(new),
                            "alpha": {m: round(float(a[m]) / 1e8, 2)
                                      for m in new}})
            cur = new
        picks[cut] = cur
    ser = pd.Series(index=dates, dtype=object)
    for d in dates:
        valid = [c for c in cuts if c <= d]
        ser[d] = picks[valid[-1]] if valid else None
    # 末尾补一个**未来**切点:界面要写「下次 X」。date_range 只走到数据末尾,
    # 不补的话 next 永远是空的。
    nxt = cuts[-1] + pd.DateOffset(months=RULES["reselect_months"]) if len(cuts) else None
    out = [c.strftime("%Y-%m-%d") for c in cuts]
    if nxt is not None:
        out.append(nxt.strftime("%Y-%m-%d"))
    return ser, log, out


def signal_series(seat: pd.DataFrame, groups: pd.Series) -> pd.DataFrame:
    """品种合计净持仓与它的变化、无量纲化 z。

    换组当天不能直接 diff:新旧两组的持仓水平不同,那会把"换了一批人"当成
    "机构大幅调仓"。所以每个组各自算一条,再按生效期取值。
    """
    net = pd.Series(index=groups.index, dtype=float)
    chg = pd.Series(index=groups.index, dtype=float)
    for grp in {g for g in groups.dropna().unique()}:
        days = groups.index[groups == grp]
        s = (seat[seat["member_key"].isin(list(grp))]
             .groupby("trade_date")["net"].sum().sort_index())
        net.loc[days] = s.reindex(days).values
        chg.loc[days] = s.diff(RULES["sig_win"]).reindex(days).values
    z = chg / chg.rolling(RULES["z_win"], min_periods=60).std()
    return pd.DataFrame({"net": net, "chg": chg, "z": z})


# ---------------------------------------------------------------- 回放

def entry_exit_signals(sig: pd.DataFrame, retail: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """按 RULES["signal_source"] 决定进场与出场各用哪一路信号。

    两路都以「正=看涨」为约定:聪明钱用净持仓变化本身(增加=减空/加多),
    散户那路在 retail_series 里已经取过负号。所以共振 = 两者同号。

    方案 C 的进场用共振后的散户信号、出场只用散户信号——出场不要求共振,
    否则聪明钱一转向就把仓位锁死在里面。
    """
    if RULES["signal_source"] != "resonance" or retail is None or retail.empty:
        return sig["z"], sig["z"]
    # 用**标准化后的 z** 判共振,不用原始 chg。
    # chg 不需要预热,拿它判等于「聪明钱信号还没预热完成就先拿来用」——
    # 2026-08-19 对拍时抓到:席位组 2024-05-01 首次生成,z 要 60 个交易日才有值,
    # 而 chg 当天就有,于是 2024-05-17 凭一个尚不可用的信号开了一仓。
    # np.sign(NaN) 是 NaN、NaN==NaN 为 False,所以改用 z 之后预热期自动不进场。
    resonate = np.sign(sig["z"]) == np.sign(retail["rz"])
    return retail["rz"].where(resonate), retail["rz"]


def replay(sig: pd.DataFrame, mkt: pd.DataFrame,
           retail: pd.DataFrame | None = None) -> tuple[list[dict], pd.Series]:
    """全量回放历史信号。

    **成交口径:信号日收盘出信号,次日开盘成交**(DEC-090,2026-08-19 运营者拍板)。

    为什么不能按信号日结算价成交:席位持仓排名是**收盘之后**才公布的——大商所约
    15:30-16:00、郑商所约 16:26,我们自己的采集就是 16:00 与 17:30 两轮。拿 16:26
    才拿到的数据去按 15:00 的结算价成交,是做不到的。生猪没有夜盘,次日开盘是它
    唯一能成交的时点;玻璃纯碱有夜盘,现实比这条口径还好一点,这里取保守的。

    这不是小修:实测按结算价成交玻璃 +5247%、纯碱 +1691%,换成次日开盘分别只剩
    +402% 和 +311%(毛)。**收益几乎全部集中在信号后的第一天**,而那一天恰恰是
    拿不到的。改这条之前页面上的数字是拿不到的数字。

    计价三条线,都逐合约、都不跨合约相除:
      ret_open  开→开,持仓期间的净值就走这条(成交价到成交价);
      o2c       同日开→结算,只用来算「持到今天收盘的浮盈」——止损要判的是
                **今天收盘时已知的**浮亏,不能用明天的开盘价去判今天该不该止损;
      ret       结→结,只留给对比栏和别的口径用。
    """
    idx = mkt.index
    z_in, z_out = entry_exit_signals(sig, retail)
    op = mkt["open"]
    ro = mkt["ret_open"]
    o2c = mkt["o2c"]
    trades, side, entry_i = [], 0, None
    v = 1.0            # 从进场成交价(open_{entry_i+1})到**今日开盘**的净值,已含方向
    cum = 0.0          # 到**今日收盘**的浮盈,止损/记账都看它
    pos = pd.Series(0.0, index=idx)
    # 逐日净值在回放结束后统一构造(见函数末尾):走**开→开**这条时钟,因为成交
    # 就在开盘,这样它连乘起来**恒等于**逐笔记账。以前是在 build_payload 里用
    # pos.shift(1)×结算价收益**另算**一条,两条对不上也没人会发现——夏普和回撤
    # 描述的是另一个策略。函数末尾有断言钉住这条恒等式。

    def _fill(k: int) -> float:
        """第 k 个信号日对应的成交价 = 次日开盘。越界或缺失给 nan。"""
        if k + 1 >= len(idx):
            return np.nan
        return float(op.iloc[k + 1]) if np.isfinite(op.iloc[k + 1]) else np.nan

    for i, d in enumerate(idx):
        z = z_out.get(d, np.nan)          # 出场判断用这一路
        if side != 0:
            if i >= entry_i + 2:          # 进场成交在 entry_i+1 的开盘,从 +2 起才有开→开
                r = ro.iloc[i]
                v *= 1 + side * (r if np.isfinite(r) else 0.0)
            c = o2c.iloc[i]
            cum = v * (1 + side * (c if np.isfinite(c) else 0.0)) - 1 if i > entry_i else 0.0
        reason = None
        near_delivery = mkt["dleft"].get(d, 99) <= RULES["exit_before_delivery"]
        if side != 0 and i > entry_i:
            # 交割纪律排在最前:它不是择时判断,是「不能再拿了」。
            if near_delivery:
                reason = "临近交割"
            elif cum <= -RULES["stop"]:
                reason = "止损"
            elif i - entry_i >= RULES["max_hold"]:
                reason = "持满"
            elif np.isfinite(z) and side * z <= -RULES["enter"]:
                reason = "反向"
            elif np.isfinite(z) and abs(z) <= RULES["exit_z"] and side * z <= 0:
                reason = "消退"
        # 平不掉就不算平:出场也要有次日开盘价才能成交,最后一天出的信号只能挂着。
        if reason and not np.isfinite(_fill(i)):
            reason = None
        if reason:
            e = idx[entry_i]
            rn = ro.iloc[i + 1]
            booked = v * (1 + side * (rn if np.isfinite(rn) else 0.0)) - 1
            trades.append({
                "side": "short" if side < 0 else "long",
                "entry_date": e.strftime("%Y-%m-%d"),
                "exit_date": d.strftime("%Y-%m-%d"),
                # 价格写的是**成交价**(次日开盘),不是信号日结算价——
                # 界面上「进场价」必须是能成交的那个数。
                "entry_px": _f(_fill(entry_i)),
                "exit_px": _f(_fill(i)),
                "contract": str(mkt["main"].get(e)),
                "ret_pct": round(booked * 100, 2),
                "hold_days": i - entry_i,
                "exit_reason": reason,
                "_i": entry_i, "_j": i,
            })
            side, v, cum = 0, 1.0, 0.0
        ze = z_in.get(d, np.nan)          # 进场判断用这一路(方案 C 下已含共振过滤)
        # 交割窗口内不进场:只挡不进,不改信号本身——换月之后主力是新合约,
        # 剩余天数一下子回到 90 多天,信号还在的话照常能进。
        if side == 0 and near_delivery:
            pos.iloc[i] = 0
            continue
        # 没有次日开盘价就进不了场(最后一天的信号、或缺开盘价的日子)。
        if side == 0 and np.isfinite(ze) and np.isfinite(_fill(i)):
            want = 0
            z = ze
            if z <= -RULES["enter"]:
                want = -1
            elif z >= RULES["enter"] and RULES["long_enabled"]:
                p = mkt["past"].get(d, np.nan)
                if (not RULES["long_needs_dip"]) or (np.isfinite(p) and p < 0):
                    want = 1
            if want != 0:
                side, entry_i, v, cum = want, i, 1.0, 0.0
        pos.iloc[i] = side
    # 尚未平仓的那笔单独带出来(界面要显示"持有中")。它按最新**结算价**估值,
    # 与已平仓那些按成交价记账不同——浮盈本来就是估值,不是成交结果。
    if side != 0:
        e = idx[entry_i]
        trades.append({
            "side": "short" if side < 0 else "long",
            "entry_date": e.strftime("%Y-%m-%d"), "exit_date": None,
            "entry_px": _f(_fill(entry_i)), "exit_px": None,
            "contract": str(mkt["main"].get(e)),
            "ret_pct": round(cum * 100, 2), "hold_days": len(idx) - 1 - entry_i,
            "exit_reason": None, "_i": entry_i, "_j": None,
        })

    # ---- 逐日净值 ----
    # 持仓区间是 [进场成交, 出场成交] = [open_{i+1}, open_{j+1}],所以吃到的开→开
    # 收益是 ret_open[i+2 .. j+1]。成本按单边各扣一次,记在两个成交日上。
    daily = pd.Series(0.0, index=idx)
    ro_f = ro.fillna(0.0).to_numpy()
    for t in trades:
        i0, j0 = t["_i"], t["_j"]
        sd = 1 if t["side"] == "long" else -1
        last = (j0 + 1) if j0 is not None else len(idx) - 1
        for k in range(i0 + 2, last + 1):
            daily.iloc[k] = sd * ro_f[k]
        if i0 + 1 < len(idx):
            daily.iloc[i0 + 1] -= COST
        if j0 is not None and j0 + 1 < len(idx):
            daily.iloc[j0 + 1] -= COST
    # 恒等式自检:**只拿已平仓那些**,逐日连乘必须等于逐笔连乘(都不含成本)。
    # 持有中那笔要排除:它的浮盈盯的是最新结算价,而逐日走到最新开盘,两边口径
    # 本来就差半天——把它算进来会造出一个假的不一致。
    # 这条错**不会报错**,只会让夏普和回撤描述另一个策略,所以必须断言。
    closed = [t for t in trades if t["_j"] is not None]
    if closed:
        by_trade = float(np.prod([1 + t["ret_pct"] / 100 for t in closed]))
        gross = pd.Series(0.0, index=idx)
        for t in closed:
            sd = 1 if t["side"] == "long" else -1
            for k in range(t["_i"] + 2, t["_j"] + 2):
                gross.iloc[k] = sd * ro_f[k]
        by_day = float((1 + gross).prod())
        if abs(by_trade - by_day) > max(0.01, 0.01 * abs(by_trade)):
            raise AssertionError(
                f"逐日净值与逐笔记账对不上:逐笔 {(by_trade-1)*100:+.1f}% / "
                f"逐日 {(by_day-1)*100:+.1f}%")
    for t in trades:
        t.pop("_i", None)
        t.pop("_j", None)
    return trades, pos, daily


def _f(v):
    return None if v is None or not np.isfinite(v) else round(float(v), 1)


def edge_split(sig: pd.DataFrame, mkt: pd.DataFrame,
               retail: pd.DataFrame | None) -> dict | None:
    """信号后第一天的超额,有多少落在**拿不到的隔夜跳空**里。

    2026-08-19 运营者问「散户反向明明是好的反向指标,为什么回撤这么大」,查出来的
    根因就是这个:席位持仓排名 16:26 才公布,所有人同一时刻看到,价格在夜盘/次日
    开盘一步跳过去。**指标是准的,准的那一段却结构性地拿不到。**

    实测(2026-08-19):生猪 71%、玻璃 86%、纯碱 83% 的第一天超额都在跳空里。
    剩下能吃到的日内部分只有 +0.07%~+0.14%,对玻纯 1.5% 量级的日波动就是噪音,
    净值曲线因此又毛又深。这也解释了为什么生猪受影响最小——它跳空占比最低,
    而且信号在 D+5~D+20 还有余温(+1.05%),不靠那一跳。

    **别拿它当可优化的参数**:延迟 1/2/3/5 天进场全部更差(实测),躲不开。
    """
    z_in, _ = entry_exit_signals(sig, retail)
    idx = mkt.index
    ret = mkt["ret"].fillna(0).to_numpy()
    o2c = mkt["o2c"].fillna(0).to_numpy()
    settle, openp = mkt["settle"].to_numpy(), mkt["open"].to_numpy()
    main_c = mkt["main"].to_numpy()
    d1, gp, it = [], [], []
    for i, d in enumerate(idx[:-1]):
        z = z_in.get(d, np.nan)
        if not np.isfinite(z) or abs(z) < RULES["enter"]:
            continue
        sd = float(np.sign(z))
        d1.append(sd * ret[i + 1] * 100)
        it.append(sd * o2c[i + 1] * 100)
        # 换月日 settle 与 open 不是同一个合约,跳空无意义,跳过
        if (main_c[i + 1] == main_c[i] and np.isfinite(openp[i + 1])
                and np.isfinite(settle[i]) and settle[i] > 0):
            gp.append(sd * (openp[i + 1] / settle[i] - 1) * 100)
    if len(d1) < 30 or not gp:
        return None
    m1, mg, mi = float(np.mean(d1)), float(np.mean(gp)), float(np.mean(it))
    return {"n": len(d1), "day1_pct": round(m1, 3), "gap_pct": round(mg, 3),
            "intraday_pct": round(mi, 3),
            "gap_share_pct": round(100 * mg / m1, 0) if m1 else None}


def risk_flags(strat: dict, closed: list, daily: pd.Series) -> list[dict]:
    """页面顶部那条醒目风险标识的素材(运营者 2026-08-19 要求)。

    **门槛写死、数字实算,不针对某个品种定制。**生猪现在一条都不触发,玻璃纯碱
    各触发几条——这是它们自己的数字说的,不是我挑出来贴上去的。哪天玻璃真的变好了,
    条目会自己消失;哪天生猪变差了,它自己会挂上来。

    五个门槛,每一个都有它自己的意思:
      夏普 < 1.0   —— 一年赚的抵不上一年的波动;
      回撤 ≥ 25%   —— 单次回撤超过四分之一,多数人拿不住;
      胜率 < 50%   —— 不到一半的交易赚钱,全靠少数几笔大赢撑着;
      t 值 < 2.0   —— 单笔均值在统计上分不出与 0 的差别,**等于还没验证**;
      亏损年 ≥ 1/4 —— 四年里有一年是亏的,不是偶发。
    """
    if not closed:
        return []
    r = np.array([t["ret_pct"] for t in closed])
    out = []
    sh = strat.get("sharpe")
    if sh is not None and sh < 1.0:
        out.append({"key": "sharpe",
                    "text": f"**夏普只有 {sh:.2f}** —— 一年赚到的抵不上一年的波动。"})
    dd = strat.get("max_dd_pct")
    if dd is not None and dd <= -25:
        out.append({"key": "drawdown",
                    "text": f"**最大回撤 {dd:.1f}%** —— 中途要扛得住净值腰斩级别的下跌。"})
    win = 100 * float((r > 0).mean())
    if win < 50:
        out.append({"key": "winrate",
                    "text": f"**胜率 {win:.1f}%,不到一半** —— 收益靠少数几笔大赢撑着,"
                            "连亏很多笔是常态。"})
    if len(r) >= 10 and r.std(ddof=1) > 0:
        t = float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r))))
        if t < 2.0:
            out.append({"key": "significance",
                        "text": f"**单笔均值的 t 值只有 {t:.2f}(<2)** —— 统计上分不出它"
                                f"与 0 的差别,{len(r)} 笔样本还**没有证明这条策略成立**。"})
    if len(daily):
        eq = (1 + daily).cumprod()
        yr = eq.resample("YE").last() / eq.resample("YE").last().shift(1) - 1
        yr = yr.dropna()
        neg = int((yr < 0).sum())
        if len(yr) >= 4 and neg * 4 >= len(yr):
            out.append({"key": "negative_years",
                        "text": f"**{len(yr)} 年里有 {neg} 年是亏的** —— 亏损年不是偶发。"})
    return out


def _caveats(strat: dict, bench: dict, closed: list) -> list[str]:
    """边界说明。**凡是数字都从实参算**,不许写死——参数一改文案就会对不上。"""
    shorts = [t for t in closed if t["side"] == "short"]
    out = ["样本只有三年(2023-08 起,大商所席位数据起点),且**只有一种市况**——全程熊市。"]
    if not RULES["long_enabled"]:
        out.append(
            "**做多支路已关闭**:回测里多头 15 笔逐笔累计 −1.5%、均值 −0.02%(抛硬币),"
            "关掉后夏普 1.96 → 2.39。机构减空时策略平仓观望,不是继续扛空单。"
            "(**这三个数是按结算价成交那一版算的**,DEC-090 改口径后没有重算;"
            "留作当时的决策依据,不要拿它和现在页面上的数字比。)")
    out.append(
        "**「机构真转多」也不是买入信号**:样本里它出现过 14 天(集中在 2025-07),"
        "之后 20 日主力仍平均跌 1.18%,最好一次只有 +0.61%。但那 14 天全挤在一个"
        "窗口里,实质是 1 个事件——只能说没有证据支持,不能说证明必亏。")
    if shorts:
        wins = sum(1 for t in shorts if t["ret_pct"] > 0)
        cum = (np.prod([1 + t["ret_pct"] / 100 for t in shorts]) - 1) * 100
        worst = min(t["ret_pct"] for t in shorts)
        out.append(f"空头信号有回测支撑:{len(shorts)} 笔 {cum:+.1f}%(毛),"
                   f"胜率 {100 * wins / len(shorts):.1f}%,最差 {worst:+.1f}%。")
    gap = "没跑赢" if strat["cum_pct"] < bench["cum_pct"] else "跑赢了"
    out.append(
        f"**绝对收益{gap}「躺着满仓做空」**({strat['cum_pct']:+.1f}% vs "
        f"{bench['cum_pct']:+.1f}%)。策略赢的是回撤({strat['max_dd_pct']:+.1f}% vs "
        f"{bench['max_dd_pct']:+.1f}%)与夏普({strat['sharpe']} vs {bench['sharpe']}),"
        "以及趋势反转时会跟着退出——后者样本内无法验证。")
    if SPLIT.get("v"):
        e = SPLIT["v"]
        out.append(
            f"**这个信号准的那一段,大部分拿不到。**{e['n']} 次触发里,信号后第一天的"
            f"平均超额是 {e['day1_pct']:+.2f}%,其中 **{e['gap_share_pct']:.0f}% 落在隔夜"
            f"跳空**(信号日结算 → 次日开盘,{e['gap_pct']:+.2f}%),真正能吃到的日内只有"
            f" {e['intraday_pct']:+.2f}%。席位排名 16:26 才公布,所有人同一时刻看到,"
            "价格在夜盘/次日开盘一步跳过去。**指标是准的,不等于这段钱赚得到。**"
            "延迟 1/2/3/5 天进场想躲开抢跑,实测全部更差。")
    out.append(
        "**成交口径:信号日收盘出信号,次日开盘成交**(DEC-090)。席位持仓排名是"
        "收盘后才公布的(大商所约 15:30-16:00、郑商所约 16:26),按信号日结算价"
        "成交做不到。这条口径下的数字比原来低很多——玻璃从 +5247% 降到 +436%(毛)"
        "——因为收益几乎全部集中在信号后第一天,而那一天恰恰是拿不到的。"
        "未模拟涨跌停与流动性冲击。")
    return out


def _perf(daily: pd.Series) -> dict:
    """一条日收益序列的累计/夏普/最大回撤。策略与基准共用,口径才对得上。"""
    dd = daily.fillna(0)
    eq = (1 + dd).cumprod()
    return {
        "cum_pct": round((float(eq.iloc[-1]) - 1) * 100, 1),
        "sharpe": round(float(dd.mean() / dd.std() * np.sqrt(242)), 2) if dd.std() > 0 else None,
        "max_dd_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
    }


# ---------------------------------------------------------------- 产物

def retail_series(seat: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """散户三家的合计净持仓、变化,以及**反向**信号的无量纲强度。

    反向:散户加多(净持仓上升)对应看跌,所以信号取负号。名单固定不重选。
    """
    have = [m for m in RULES["retail_seed"] if m in set(seat["member_key"])]
    if len(have) < 2:
        return pd.DataFrame(index=dates, columns=["net", "chg", "rz"], dtype=float), have
    s = (seat[seat["member_key"].isin(have)]
         .groupby("trade_date")["net"].sum().sort_index().reindex(dates))
    chg = s.diff(RULES["sig_win"])
    rz = -(chg - chg.rolling(RULES["z_win"], min_periods=60).mean()) /          chg.rolling(RULES["z_win"], min_periods=60).std()
    return pd.DataFrame({"net": s, "chg": chg, "rz": rz}), have


def build_payload(sig: pd.DataFrame, mkt: pd.DataFrame, seat: pd.DataFrame,
                  groups: pd.Series, log: list, cuts: list | None = None) -> dict:
    d = mkt.index[-1]
    z = sig["z"].get(d, np.nan)
    # 散户那路要先算出来:方案 C 的进出场都靠它(见 entry_exit_signals)
    rdf, rhave = retail_series(seat, mkt.index)
    trades, pos, daily = replay(sig, mkt, rdf)
    open_trade = trades[-1] if trades and trades[-1]["exit_date"] is None else None
    closed = [t for t in trades if t["exit_date"]]

    prev_d = mkt.index[-1 - RULES["sig_win"]] if len(mkt) > RULES["sig_win"] else None
    grp = list(groups.get(d) or ())
    today = seat[(seat["trade_date"] == d) & (seat["member_key"].isin(grp))]
    prev = seat[(seat["trade_date"] == prev_d) & (seat["member_key"].isin(grp))] if prev_d is not None else None
    members = []
    for m in grp:
        now = float(today[today["member_key"] == m]["net"].sum()) if len(today) else 0.0
        was = float(prev[prev["member_key"] == m]["net"].sum()) if prev is not None and len(prev) else np.nan
        members.append({
            "member": m,
            "net": round(now),
            "change": None if not np.isfinite(was) else round(now - was),
            "on_board": bool(len(today[today["member_key"] == m])),
        })

    # —— 散户反向维度 ——
    r_now = rdf.loc[d] if d in rdf.index else None
    rz_now = float(r_now["rz"]) if r_now is not None and np.isfinite(r_now.get("rz", np.nan)) else None
    # 共振 = 聪明钱流向与散户反向流向同号。两者都以「正=看涨」为约定:
    # 聪明钱用 chg 本身(净持仓增加=减空/加多),散户已在 retail_series 里取过负号。
    smart_now = sig["chg"].get(d, np.nan)
    resonate = bool(rz_now is not None and np.isfinite(smart_now)
                    and np.sign(smart_now) == np.sign(rz_now))
    rmembers = []
    for m in rhave:
        cur = seat[(seat["trade_date"] == d) & (seat["member_key"] == m)]["net"].sum()
        prev_row = seat[(seat["trade_date"] == prev_d) & (seat["member_key"] == m)]             if prev_d is not None else None
        was = prev_row["net"].sum() if prev_row is not None and len(prev_row) else np.nan
        rmembers.append({"member": m, "net": int(round(cur)),
                         "change": None if not np.isfinite(was) else int(round(cur - was)),
                         "on_board": bool(len(seat[(seat["trade_date"] == d)
                                                   & (seat["member_key"] == m)]))})
    retail_state = {
        "members": rmembers,
        "net": None if r_now is None or not np.isfinite(r_now.get("net", np.nan))
               else int(r_now["net"]),
        "change": None if r_now is None or not np.isfinite(r_now.get("chg", np.nan))
                  else int(r_now["chg"]),
        "z": None if rz_now is None else round(rz_now, 2),
        # z 为正 = 散户在减多/加空 → 反向看涨;为负 = 散户在加多 → 反向看跌
        "resonate": resonate,
        "trades": RULES["signal_source"] == "resonance",
        "note": "散户三家长期站多头、长期亏钱,故反向取用;名单跨品种固定、不逐品种重选。"
                "**现行策略(方案 C)就是用它进出场**:与聪明钱共振时按它的方向进场,"
                "它翻向时出场。选它是因为回撤最小(−4.1% vs 主信号 −9.4%)、夏普最高;"
                "但要如实知道——三个候选方案单笔均值差的 t 只有 0.22~0.49,"
                "**统计上分不出高下**,这是一个判断,不是数据证明的最优解。",
    }

    state = "观察中"
    if open_trade:
        state = "做空中" if open_trade["side"] == "short" else "做多中"

    # 机构方向本身要报出来,与「要不要进场」分开。运营者盯的就是这个拐点:
    # 做多支路虽然关着,但机构什么时候真的转成净多,他得第一时间看见。
    net_now = sig["net"].get(d, np.nan)
    net_ok = bool(np.isfinite(net_now))
    # 「刚转多」= 今天净多而 sig_win 天前还是净空,用来在界面上打一次提示
    prev_i = len(mkt) - 1 - RULES["sig_win"]
    prev_net = sig["net"].get(mkt.index[prev_i], np.nan) if prev_i >= 0 else np.nan
    institution = {
        "net": int(net_now) if net_ok else None,
        "side": ("net_long" if net_now > 0 else "net_short") if net_ok else None,
        "just_flipped_long": bool(net_ok and np.isfinite(prev_net)
                                  and net_now > 0 >= prev_net),
        "long_enabled": RULES["long_enabled"],
        # 关着做多时,z 上穿门槛只代表「机构在减空」,不产生进场——界面要说清,
        # 否则看的人会以为信号漏了。
        "long_signal_now": bool(np.isfinite(z) and z >= RULES["enter"]),
    }

    SPLIT["v"] = edge_split(sig, mkt, rdf)
    wins = [t for t in closed if t["ret_pct"] > 0]

    # 与「躺着满仓做空」的对比。**这一栏必须摆在界面上**:三年单边熊市里,
    # 什么都不做地持有空单本身就有 +99% 的复利收益,不给基准,看的人会把
    # 策略的累计收益当成本事。策略真正赢的是夏普与回撤,不是绝对收益。
    # 逐日净值直接用 replay 产出的那条(DEC-090):它按结算价盯市、成交那两天算半天,
    # 连乘起来与逐笔记账完全相等。以前是在这里用 pos.shift(1)×结算价收益**另算**
    # 一条,两条对不上也没人会发现——夏普和回撤描述的是另一个策略。
    strat_daily = daily
    bench_daily = -mkt["ret"].fillna(0)
    return {
        "instrument": CURRENT["code"],
        "name": CURRENT["name"],
        "unit": CURRENT["unit"],
        "multiplier": RULES["multiplier"],
        "data_date": d.strftime("%Y-%m-%d"),
        "computed_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "state": state,
        "contract": str(mkt["main"].get(d)),
        "price": _f(mkt["settle"].get(d)),
        # 散户交割纪律的当前读数(2026-08-19 运营者要求)。界面要能一眼看出
        # 「这个主力还能拿几天」——2026-08-14 玻璃主力还是 FG2609,只剩 11 个
        # 交易日,差一天就撞线,而页面当时对此只字不提。
        "delivery": {
            "window_end": window_end(mkt["main"].get(d)).strftime("%Y-%m-%d"),
            "days_left": int(mkt["dleft"].get(d, 0)),
            "limit": RULES["exit_before_delivery"],
            "must_exit": bool(mkt["dleft"].get(d, 99) <= RULES["exit_before_delivery"]),
        },
        "signal": {
            "z": None if not np.isfinite(z) else round(float(z), 2),
            "enter": RULES["enter"],
            "net": None if not np.isfinite(sig["net"].get(d, np.nan)) else int(sig["net"].get(d)),
            "change": None if not np.isfinite(sig["chg"].get(d, np.nan)) else int(sig["chg"].get(d)),
            "win": RULES["sig_win"],
            # 连续版的建议仓位强度:回测夏普比离散版更高(2.66 vs 2.26),
            # 但换手大、抗成本差,所以只作参考不作指令。
            "suggested_position": None if not np.isfinite(z) else round(float(np.clip(z, -2, 2)), 2),
        },
        "position": open_trade,
        "institution": institution,
        "retail": retail_state,
        "members": members,
        "group_log": log[-8:],
        # 重选切点(2026-08-19 加):`group_log` 只记**换人**,阵容没变就不写。
        # 界面要能说「最近一次重选是哪天、换没换人」,否则看上去像三年没重选过。
        "reselect": {
            "last": next((c for c in reversed(cuts or []) if c <= d.strftime("%Y-%m-%d")), None),
            "next": next((c for c in (cuts or []) if c > d.strftime("%Y-%m-%d")), None),
            "changed_at": log[-1]["date"] if log else None,
        },
        "history": closed,
        "stats": {
            "trades": len(closed),
            "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
            "avg_pct": round(float(np.mean([t["ret_pct"] for t in closed])), 2) if closed else None,
            "cum_pct": round((np.prod([1 + t["ret_pct"] / 100 for t in closed]) - 1) * 100, 1)
                       if closed else None,
            "short_trades": sum(1 for t in closed if t["side"] == "short"),
            "long_trades": sum(1 for t in closed if t["side"] == "long"),
            # 出场原因分布:策略方案页那句「实测 N 笔全部由 X 触发」由它生成,
            # 不写死——消退条件至今一次没触发过,但这是数出来的不是记住的。
            "exit_reasons": {r: sum(1 for t in closed if t["exit_reason"] == r)
                             for r in sorted({t["exit_reason"] for t in closed
                                              if t["exit_reason"]})},
        },
        "compare": {
            "strategy": _perf(strat_daily),
            "benchmark": _perf(bench_daily),
            "benchmark_name": "恒定满仓做空",
            "note": "同一段区间、同一口径(逐日复利,策略扣单边 0.05% 换手成本)。"
                    "做空的复利收益不是买入持有取反——价格跌 52.9% 对应做空 +99.2%。",
        },
        "rules": {k: v for k, v in RULES.items() if k not in ("alias",)},
        # 界面必须把这句话摆出来,不能让人以为多头信号和空头一样可信。
        # 数字一律**由实际回测结果生成**,不写死。上一版把 "+86.5% vs +99.2%"
        # 硬编码在这里,关掉做多支路后就成了错的——同一个事实两处维护,必栽。
        "caveats": _caveats(_perf(strat_daily), _perf(bench_daily), closed),
        "edge_split": SPLIT.get("v"),
        # 顶部醒目风险条(运营者 2026-08-19 要求)。门槛写死、数字实算,
        # 品种自己够不上门槛就没有条目——不是按品种硬编码的。
        "risk_flags": risk_flags(_perf(strat_daily), closed, strat_daily),
    }


# ---------------------------------------------------------------- FG-SA 配对

def pair_fgsa(cache: dict, out_dir: Path) -> dict | None:
    """玻璃与纯碱的**相对**资金流向,预测 FG−SA 价差的走向。

    为什么单独做这一条:研究阶段发现,两品种流向之**差**比它们各自的绝对流向
    更有信息量(全样本 t=+5.43,比 FG 单品种 +2.96、SA 单品种 +4.41 都高),
    而平台的套利监控本来就盯着 FG-SA 这个组合——它现在只看价差位置与历史分位,
    没有「资金在往哪边调」这一维。

    口径:两品种各自取 alpha 前 5 席位的合计净持仓 5 日变化,**各自**减均值除标准差
    之后相减。必须各自标准化再减:两个品种的持仓量级差一倍以上,直接相减等于让
    量级大的那个说了算。

    信号为正 = 玻璃这边资金相对更强 → 价差(FG−SA)倾向走扩。
    """
    need = ("FG", "SA")
    if any(c not in cache for c in need):
        print("[pair] FG/SA 未都跑成,跳过配对信号", file=sys.stderr)
        return None
    zs = {}
    for c in need:
        chg = cache[c]["chg"]
        zs[c] = (chg - chg.rolling(RULES["z_win"], min_periods=60).mean()) /                 chg.rolling(RULES["z_win"], min_periods=60).std()
    joined = pd.concat([zs["FG"].rename("fg"), zs["SA"].rename("sa")], axis=1).dropna()
    if joined.empty:
        return None
    d = joined.index[-1]
    z = float(joined["fg"].iloc[-1] - joined["sa"].iloc[-1])
    payload = {
        "pair": "FG-SA",
        "data_date": d.strftime("%Y-%m-%d"),
        "computed_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "z": round(z, 2),
        "fg_z": round(float(joined["fg"].iloc[-1]), 2),
        "sa_z": round(float(joined["sa"].iloc[-1]), 2),
        "direction": "widen" if z > 0 else ("narrow" if z < 0 else "flat"),
        "note": "玻璃与纯碱的**相对**资金流向。正=玻璃这边资金相对更强,价差(FG−SA)"
                "倾向走扩;负=倾向收窄。**这是背景不是交易信号**——它预测的是价差方向,"
                "不含进出场与仓位。",
        "evidence": "全样本偏相关 +0.140(t=+5.43,N=1480),比 FG 单品种 +2.96、"
                    "SA 单品种 +4.41 都高;逐年 6 正 1 负;滚动样本外四个截点 "
                    "+3.31/+0.76/+4.74/+1.77 全正无反向;五档两端差 114 元/吨。",
        "caveats": [
            "**2026 年是负的**(t=−1.16,不显著)——当下正处在这个信号的哑火年份。",
            "五档里中间档不单调,它能分辨两端、说不清中间。",
            "预测的是**价差方向**,不是某一条腿的方向,也没有出场规则。",
        ],
    }
    out = out_dir / "pair_fgsa.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)
    print(f"[pair] {payload['data_date']} 写出 {out}  z={payload['z']} "
          f"({'走扩' if z > 0 else '收窄'})")
    return payload


def run_one(code: str, src: str, out_dir: Path) -> dict | None:
    """跑一个品种。失败只告警不抛——一个品种挂了不该拖垮其余两个。"""
    global CURRENT
    v = use(code)
    CURRENT = {"code": code, **v}
    try:
        if src == "csv":
            price_raw, seat_raw = load_from_csv(
                code, Path(os.environ.get("CSV_DIR", "../research/data")))
        else:
            price_raw, seat_raw = load_from_pg(
                code,
                os.environ.get("PG_CONTAINER", "futures-analysis-platform-postgres-1"),
                os.environ.get("PG_USER", "futures_app"),
                os.environ.get("PG_DB", "futures_platform"))
        price = clean_price(price_raw)
        seat = clean_seat(seat_raw)
        mkt = main_series(price)
        mkt = mkt[mkt.index >= pd.Timestamp(RULES["replay_start"])]
        groups, log, cuts = rolling_groups(seat, price, mkt.index)
        sig = signal_series(seat, groups)
        payload = build_payload(sig, mkt, seat, groups, log, cuts)
    except Exception as e:                      # noqa: BLE001
        print(f"[{code}] 失败,保留上一版:{e}", file=sys.stderr)
        return None
    SIG_CACHE[code] = sig

    out_path = out_dir / v["out"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)   # 原子替换,避免前端读到半截文件
    st = payload["stats"]
    print(f"[{code}] {payload['data_date']} 写出 {out_path}")
    print(f"  状态 {payload['state']} | z={payload['signal']['z']} | "
          f"席位组 {'、'.join(m['member'] for m in payload['members'])}")
    print(f"  历史 {st['trades']} 笔(空 {st['short_trades']}/多 {st['long_trades']}),"
          f"累计 {st['cum_pct']}%,胜率 {st['win_rate']}%")
    return payload


def main():
    src = os.environ.get("ENGINE_SOURCE", "pg")
    # HOG_OUT 保留兼容:老调用方传的是「生猪那个文件」的完整路径,取它的目录。
    legacy = os.environ.get("HOG_OUT")
    out_dir = (Path(legacy).parent if legacy
               else Path(os.environ.get("FLOW_OUT_DIR", "/opt/futures-platform/signals")))
    codes = [c.strip().upper() for c in
             os.environ.get("FLOW_CODES", "LH,FG,SA").split(",") if c.strip()]
    ok = 0
    for code in codes:
        if code not in VARIETIES:
            print(f"[{code}] 未在 VARIETIES 里配置,跳过", file=sys.stderr)
            continue
        if run_one(code, src, out_dir) is not None:
            ok += 1
    # FG-SA 配对信号:两个品种都跑成了才算。失败只告警,不影响单品种产物。
    if {"FG", "SA"} <= set(SIG_CACHE):
        try:
            pair_fgsa(SIG_CACHE, out_dir)
        except Exception as e:                  # noqa: BLE001
            print(f"[pair] 配对信号失败,保留上一版:{e}", file=sys.stderr)
    print(f"[flow] 完成 {ok}/{len(codes)} 个品种")
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
