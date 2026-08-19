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

回测证据(2023-08~2026-08,一年选人 + 只做空):
  恒定满仓做空基准 +99.2%/夏普 1.65/回撤 −14.8%
  本引擎            +79.7%/夏普 2.39/回撤  −9.4%,21 笔胜率 71.4%、最差 −3.8%

**必须正视的一件事:绝对收益没跑赢基准**(+79.7% vs +99.2%)。这三年是单边熊市,
躺着满仓做空本身就有 +99% 复利。策略赢的是夏普(2.39 vs 1.65)与回撤(−9.4% vs
−14.8%),以及**趋势反转时会退出而不是硬扛**——而后者在样本内无法验证(没有牛市)。
界面上必须摆出这个对比(payload 的 `compare`),不然看的人会把熊市 beta 当成本事。
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

CN_TZ = timezone(timedelta(hours=8))

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
    "long_enabled": False,
    # 做多真要开启时才用得上:Phase 0 双分档里「跌 × 机构减空」是全表唯一
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
    # 共振 = 聪明钱流向与散户反向流向同号。实测(样本外 2024-01 起,只做空):
    #   现有主信号 23 笔 +39.8%/胜率 52.2%/回撤 −10.9%/夏普 1.73
    #   共振      22 笔 +74.2%/胜率 68.2%/回撤  −5.7%/夏普 2.57
    # **但当前只做展示,不参与进出场**:22 笔、2.6 年撑不起推翻已上线的规则,
    # 且散户名单的最终三家是运营者看过全样本后从六家候选里挑的,有轻微选择偏差。
    # 等实盘观察一段再决定要不要让它主导(DEC-085 记了这个待办)。
    "resonance_trades": False,
    "multiplier": 16.0,      # LH 合约点值
    "replay_start": "2023-08-11",   # 大商所席位数据起点,再往前没有席位
}

SEAT_RANK = {"akshare_v1": 1, "eastmoney_seats_v1": 2, "sanhe": 3}
PRICE_RANK = {"akshare_v1": 1, "eastmoney_seats_v1": 2, "sina_v1": 3}


# ---------------------------------------------------------------- 数据

def _rank(src: pd.Series, table: dict) -> pd.Series:
    out = src.map(table)
    return out.where(~src.str.contains("_official", na=False), 0).fillna(4)


def load_from_pg(container: str, pg_user: str, pg_db: str) -> tuple[pd.DataFrame, pd.DataFrame]:
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
              "from price_history where instrument='LH'")
    seat = q("select instrument,contract,is_variety_total,trade_date,rank_type,member,"
             "quantity,change,source from seat_history where instrument='LH'")
    return price, seat


def load_from_csv(csv_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    def one(stem: str) -> pd.DataFrame:
        # 生产链路落的是 .csv(run-smart-money.sh 导出);研究目录存的是 .csv.gz。
        for name in (f"{stem}.csv", f"{stem}.csv.gz"):
            p = csv_dir / name
            if p.exists():
                return pd.read_csv(p)
        raise FileNotFoundError(f"{csv_dir}/{stem}.csv[.gz] 都不存在")
    return one("lh_price"), one("lh_seat")


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
    rows = []
    for d, c in zip(dates, main):
        s = px.get((c, d), np.nan)
        hist = px.loc[c] if c in px.index.get_level_values(0) else pd.Series(dtype=float)
        earlier = hist[hist.index < d]
        prev = earlier.iloc[-1] if len(earlier) else np.nan
        ret = s / prev - 1.0 if (np.isfinite(s) and np.isfinite(prev) and prev > 0) else np.nan
        rows.append((d, c, s, ret))
    out = pd.DataFrame(rows, columns=["trade_date", "main", "settle", "ret"]).set_index("trade_date")
    out["past"] = out["settle"].pct_change(RULES["dip_win"])
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
                   dates: pd.DatetimeIndex) -> tuple[pd.Series, list]:
    """逐日生效的席位组,外加一份重选历史(界面要展示"什么时候换了谁")。"""
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
    return ser, log


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

def replay(sig: pd.DataFrame, mkt: pd.DataFrame) -> tuple[list[dict], pd.Series]:
    """全量回放历史信号。与 research/run_lh_phase2.py 的 backtest_discrete 同口径。

    T+1:今日收盘算出的信号,吃的是明日收益——日内不可能按今日结算价成交。
    """
    idx = mkt.index
    trades, side, entry_i, cum = [], 0, None, 0.0
    pos = pd.Series(0.0, index=idx)
    for i, d in enumerate(idx):
        z = sig["z"].get(d, np.nan)
        r = mkt["ret"].get(d, np.nan)
        if side != 0:
            cum = (1 + cum) * (1 + side * (r if np.isfinite(r) else 0)) - 1
        reason = None
        if side != 0:
            if cum <= -RULES["stop"]:
                reason = "止损"
            elif i - entry_i >= RULES["max_hold"]:
                reason = "持满"
            elif np.isfinite(z) and side * z <= -RULES["enter"]:
                reason = "反向"
            elif np.isfinite(z) and abs(z) <= RULES["exit_z"] and side * z <= 0:
                reason = "消退"
        if reason:
            e = idx[entry_i]
            trades.append({
                "side": "short" if side < 0 else "long",
                "entry_date": e.strftime("%Y-%m-%d"),
                "exit_date": d.strftime("%Y-%m-%d"),
                "entry_px": _f(mkt["settle"].get(e)),
                "exit_px": _f(mkt["settle"].get(d)),
                "contract": str(mkt["main"].get(e)),
                "ret_pct": round(cum * 100, 2),
                "hold_days": i - entry_i,
                "exit_reason": reason,
            })
            side, cum = 0, 0.0
        if side == 0 and np.isfinite(z):
            want = 0
            if z <= -RULES["enter"]:
                want = -1
            elif z >= RULES["enter"] and RULES["long_enabled"]:
                p = mkt["past"].get(d, np.nan)
                if (not RULES["long_needs_dip"]) or (np.isfinite(p) and p < 0):
                    want = 1
            if want != 0:
                side, entry_i, cum = want, i, 0.0
        pos.iloc[i] = side
    # 尚未平仓的那笔单独带出来(界面要显示"持有中")
    if side != 0:
        e = idx[entry_i]
        trades.append({
            "side": "short" if side < 0 else "long",
            "entry_date": e.strftime("%Y-%m-%d"), "exit_date": None,
            "entry_px": _f(mkt["settle"].get(e)), "exit_px": None,
            "contract": str(mkt["main"].get(e)),
            "ret_pct": round(cum * 100, 2), "hold_days": len(idx) - 1 - entry_i,
            "exit_reason": None,
        })
    return trades, pos


def _f(v):
    return None if v is None or not np.isfinite(v) else round(float(v), 1)


def _caveats(strat: dict, bench: dict, closed: list) -> list[str]:
    """边界说明。**凡是数字都从实参算**,不许写死——参数一改文案就会对不上。"""
    shorts = [t for t in closed if t["side"] == "short"]
    out = ["样本只有三年(2023-08 起,大商所席位数据起点),且**只有一种市况**——全程熊市。"]
    if not RULES["long_enabled"]:
        out.append(
            "**做多支路已关闭**:回测里多头 15 笔逐笔累计 −1.5%、均值 −0.02%(抛硬币),"
            "关掉后夏普 1.96 → 2.39。机构减空时策略平仓观望,不是继续扛空单。"
            "(这三个数是关闭前那一版的回测结论,留作依据。)")
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
    out.append("回测按结算价成交、T+1 执行,未模拟涨跌停与流动性冲击。")
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
                  groups: pd.Series, log: list) -> dict:
    d = mkt.index[-1]
    z = sig["z"].get(d, np.nan)
    trades, pos = replay(sig, mkt)
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

    # —— 散户反向维度(展示用,不参与进出场,见 RULES["resonance_trades"])——
    rdf, rhave = retail_series(seat, mkt.index)
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
        "trades": RULES["resonance_trades"],
        "note": "散户三家长期站多头、长期亏钱,故反向取用。"
                "名单跨品种固定、不逐品种重选(实测加人反而变差)。"
                "**当前只作展示,不参与进出场**——22 笔、2.6 年样本还不足以推翻已上线的规则。",
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

    wins = [t for t in closed if t["ret_pct"] > 0]

    # 与「躺着满仓做空」的对比。**这一栏必须摆在界面上**:三年单边熊市里,
    # 什么都不做地持有空单本身就有 +99% 的复利收益,不给基准,看的人会把
    # 策略的累计收益当成本事。策略真正赢的是夏普与回撤,不是绝对收益。
    strat_daily = (pos.shift(1).fillna(0) * mkt["ret"]
                   - pos.shift(1).fillna(0).diff().abs().fillna(0) * 0.0005)
    bench_daily = -mkt["ret"]
    return {
        "instrument": "LH",
        "name": "生猪 LH",
        "unit": "元/吨",
        "multiplier": RULES["multiplier"],
        "data_date": d.strftime("%Y-%m-%d"),
        "computed_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "state": state,
        "contract": str(mkt["main"].get(d)),
        "price": _f(mkt["settle"].get(d)),
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
    }


def main():
    src = os.environ.get("ENGINE_SOURCE", "pg")
    out_path = Path(os.environ.get("HOG_OUT", "/opt/futures-platform/signals/hog_signals.json"))
    if src == "csv":
        price_raw, seat_raw = load_from_csv(Path(os.environ.get("CSV_DIR", "../research/data")))
    else:
        price_raw, seat_raw = load_from_pg(
            os.environ.get("PG_CONTAINER", "futures-analysis-platform-postgres-1"),
            os.environ.get("PG_USER", "futures_app"),
            os.environ.get("PG_DB", "futures_platform"))

    price = clean_price(price_raw)
    seat = clean_seat(seat_raw)
    mkt = main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(RULES["replay_start"])]
    groups, log = rolling_groups(seat, price, mkt.index)
    sig = signal_series(seat, groups)

    payload = build_payload(sig, mkt, seat, groups, log)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)   # 原子替换,避免前端读到半截文件
    s = payload["stats"]
    print(f"[hog] {payload['data_date']} 写出 {out_path}")
    print(f"  状态 {payload['state']} | z={payload['signal']['z']} | "
          f"席位组 {'、'.join(m['member'] for m in payload['members'])}")
    print(f"  历史 {s['trades']} 笔(空 {s['short_trades']}/多 {s['long_trades']}),"
          f"累计 {s['cum_pct']}%,胜率 {s['win_rate']}%")


if __name__ == "__main__":
    main()
