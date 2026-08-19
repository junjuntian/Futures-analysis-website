"""生猪 Phase 2：出场规则 + 完整回测 + 交易成本。

Phase 1 证明了信号有预测力(样本外 t=4~6),但"信号有预测力"不等于"策略能赚钱"。
本轮补上缺的三块:怎么出场、扣掉成本还剩多少、逐年是不是一致。

三条纪律,都是 PITFALLS 六的教训:
  - 参数尽量少,门槛不做旋钮;
  - 结论要求「相邻档同向」,不挑峰值;
  - 席位组**滚动重选**(运营者 2026-08-19 拍板),训练段不含未来数据。

计价口径:
  - 主力合约,**逐合约算收益,换月日用新合约自己的前一日结算价**——不跨合约相除
    (LH 各合约相对主力偏离最大 49%,跨合约得到的是价差不是收益)。
  - 信号用**品种合计**(Phase 1 P4/P5:逐合约会被移仓撕成相反的两半,84.6% 的
    交易日同日方向相反)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import lhlib as L

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 60)

SIG_WIN = 5        # 信号窗口。Phase 0 已定,20 日窗混入动量
GROUP_K = 5        # 组内席位数。Phase 1 三个截点上都是 5 最好
# 2026-08-19 运营者拍板 3→12(「3 个月太短,会有很多噪音」)。必须与
# engine/hog_money.py 的 RULES["reselect_months"] 保持一致,否则对拍会假失败。
RESELECT_M = 12
WARMUP_DAYS = 250  # 首次选组前至少要有这么多天历史
COST_ONEWAY = 0.0005   # 单边成本(手续费+滑点)。LH 一跳 5 元/吨 × 16 吨 ≈ 万 3.8


def section(t: str):
    print("\n" + "=" * 82)
    print(t)
    print("=" * 82)


# ---------------------------------------------------------------- 计价

def main_returns(price: pd.DataFrame) -> pd.DataFrame:
    """主力合约的逐日收益。**换月日用新主力自己的前一日结算价。**

    这是整个回测的地基:一旦这里跨合约相除,换月那天就会凭空多出一笔几个百分点
    的假收益,而且不报错。
    """
    mc = L.main_contract(price)
    px = price.set_index(["contract", "trade_date"])["settle"].sort_index()
    rows = []
    prev_by_c = {}
    for d, c in zip(mc["trade_date"], mc["main"]):
        s = px.get((c, d), np.nan)
        # 该合约自己的上一交易日结算价
        hist = px.loc[c] if c in px.index.get_level_values(0) else pd.Series(dtype=float)
        earlier = hist[hist.index < d]
        prev = earlier.iloc[-1] if len(earlier) else np.nan
        ret = s / prev - 1.0 if (np.isfinite(s) and np.isfinite(prev) and prev > 0) else np.nan
        rows.append((d, c, s, ret))
        prev_by_c[c] = s
    out = pd.DataFrame(rows, columns=["trade_date", "main", "settle", "ret"]).set_index("trade_date")
    return out


# ---------------------------------------------------------------- 席位组与信号

def alpha_upto(df: pd.DataFrame, hi: pd.Timestamp) -> pd.Series:
    """截至 hi(不含)的每家 alpha。滚动重选就靠它,绝不许看 hi 之后的数据。"""
    d = df[df["trade_date"] < hi]
    d = d.sort_values(["member_key", "contract", "trade_date"]).copy()
    g = d.groupby(["member_key", "contract"])
    d["prev_net"] = g["net"].shift()
    d["prev_settle"] = g["settle"].shift()
    gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
    d = d[d["prev_net"].notna() & (gap <= 5)]
    if d.empty:
        return pd.Series(dtype=float)
    dpx = (d["settle"] - d["prev_settle"]) * L.MULTIPLIER
    pnl = (dpx * d["prev_net"]).groupby(d["member_key"]).sum()
    beta = dpx.groupby(d["member_key"]).apply(lambda s: np.nan)  # 占位,下面重算
    grp = d.groupby("member_key")
    beta = grp.apply(lambda s: ((s["settle"] - s["prev_settle"]) * L.MULTIPLIER
                                * s["prev_net"].mean()).sum(), include_groups=False)
    days = grp["trade_date"].nunique()
    alpha = (pnl - beta)[days >= 120]
    return alpha.sort_values(ascending=False)


def rolling_groups(df: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.Series:
    """每 RESELECT_M 个月重选一次前 GROUP_K 家,返回逐日生效的组。

    生效日一律在重选日**之后**:当天算出的名单当天就用,等于用了当天的数据选人。
    """
    start = dates.min() + pd.Timedelta(days=WARMUP_DAYS)
    cuts = pd.date_range(start, dates.max(), freq=f"{RESELECT_M}MS")
    picks, cur = {}, None
    for cut in cuts:
        a = alpha_upto(df, cut)
        if len(a) >= GROUP_K:
            cur = tuple(a.head(GROUP_K).index)
        picks[cut] = cur
    ser = pd.Series(index=dates, dtype=object)
    for d in dates:
        valid = [c for c in cuts if c <= d]
        ser[d] = picks[valid[-1]] if valid else None
    return ser


def signal_series(df: pd.DataFrame, groups: pd.Series) -> pd.Series:
    """品种合计净持仓的 SIG_WIN 日变化,按当日生效的席位组算。

    组会换人,所以不能一次算完:换组当天,新旧两组的持仓水平不同,直接 diff 会
    把「换了一批人」当成「机构大幅调仓」。这里对每个组各自算一条,再按生效期取值。
    """
    out = pd.Series(index=groups.index, dtype=float)
    for grp in set(g for g in groups.dropna().unique()):
        mask = groups == grp
        days = groups.index[mask]
        s = (df[df["member_key"].isin(list(grp))]
             .groupby("trade_date")["net"].sum().sort_index())
        chg = s.diff(SIG_WIN)
        out.loc[days] = chg.reindex(days).values
    return out


def zscore(sig: pd.Series, win: int = 120) -> pd.Series:
    """用滚动标准差把信号无量纲化。

    绝对手数不能直接当阈值:2026 年机构合计净空是 2024 年的四倍,同样 2000 手的
    变化,在两个时期的含义完全不同。无量纲化是通用化的前提(PITFALLS 六)。
    """
    return sig / sig.rolling(win, min_periods=60).std()


# ---------------------------------------------------------------- 回测

def backtest_continuous(z: pd.Series, ret: pd.Series, cap: float = 2.0,
                        cost: float = COST_ONEWAY) -> pd.DataFrame:
    """信号即仓位:position = clip(z, ±cap)。参数最少的诊断版。

    T+1 执行:今天收盘算出的信号,吃的是**明天**的收益。日内不可能按今日结算价成交。
    """
    pos = z.clip(-cap, cap).shift(1)
    turn = pos.diff().abs().fillna(0)
    gross = pos * ret
    net = gross - turn * cost
    return pd.DataFrame({"pos": pos, "ret": ret, "gross": gross, "net": net}).dropna()


def backtest_discrete(z: pd.Series, ret: pd.Series, past: pd.Series,
                      enter: float = 1.0, exit_z: float = 0.0,
                      stop: float = 0.06, max_hold: int = 40,
                      long_needs_dip: bool = True, long_enabled: bool = True,
                      cost: float = COST_ONEWAY) -> tuple[pd.DataFrame, list]:
    """离散进出场,更接近实盘。

    进场:|z| ≥ enter,方向跟随机构(z<0 机构加空 → 做空;z>0 机构减空/加多 → 做多)。
      做多**额外要求过去 20 日是跌的**——Phase 0 双分档里,「跌 × 机构减空」是全表
      唯一的正收益格子(+0.30%),而「涨 × 减空」是 -1.97%。这不是调参,是那张表的
      直接读数。
    出场:信号回到 exit_z 以内、反向信号、硬止损、或持满 max_hold。
    """
    idx = ret.index
    pos = pd.Series(0.0, index=idx)
    trades = []
    side, entry_i, entry_px, cum = 0, None, None, 0.0
    equity = (1 + ret.fillna(0)).cumprod()

    for i, d in enumerate(idx):
        zz, r = z.get(d, np.nan), ret.get(d, np.nan)
        if side != 0:
            cum = (1 + cum) * (1 + side * (r if np.isfinite(r) else 0)) - 1
        reason = None
        if side != 0:
            if cum <= -stop:
                reason = "止损"
            elif i - entry_i >= max_hold:
                reason = "持满"
            elif np.isfinite(zz) and side * zz <= -enter:
                reason = "反向"
            elif np.isfinite(zz) and abs(zz) <= exit_z and side * zz <= 0:
                reason = "消退"
        if reason:
            trades.append({"进场": idx[entry_i], "出场": d, "方向": "多" if side > 0 else "空",
                           "收益%": (cum - 2 * cost) * 100, "持有日": i - entry_i, "出场原因": reason})
            side, cum = 0, 0.0
        if side == 0 and np.isfinite(zz):
            want = 0
            if zz <= -enter:
                want = -1
            elif zz >= enter and long_enabled:
                p = past.get(d, np.nan)
                if (not long_needs_dip) or (np.isfinite(p) and p < 0):
                    want = 1
            if want != 0:
                side, entry_i, cum = want, i, 0.0
        pos[d] = side
    tr = pd.DataFrame(trades)
    return tr, pos.tolist()


def summarize(tr: pd.DataFrame, label: str):
    if tr.empty:
        print(f"  {label:22s} 无交易")
        return
    r = tr["收益%"]
    tot = (1 + r / 100).prod() - 1
    print(f"  {label:22s} 笔数{len(tr):4d}  累计{tot * 100:+8.1f}%  "
          f"均值{r.mean():+6.2f}%  胜率{(r > 0).mean() * 100:5.1f}%  "
          f"平均持有{tr['持有日'].mean():4.1f}日  最差{r.min():+6.1f}%")


def main():
    price = L.load_price()
    seat = L.load_seat()
    df = seat.merge(price[["contract", "trade_date", "settle"]],
                    on=["contract", "trade_date"], how="inner")
    mr = main_returns(price)
    mr = mr[mr.index >= df["trade_date"].min()]
    ret = mr["ret"]
    past = mr["settle"].pct_change(20)  # 同一主力序列内的过去收益,仅作条件用

    print(f"回测区间 {ret.index.min():%Y-%m-%d} ~ {ret.index.max():%Y-%m-%d},"
          f" {len(ret)} 个交易日")
    bh = (1 + ret.fillna(0)).prod() - 1
    short_bh = (1 - ret.fillna(0)).prod() - 1
    # 做空收益不是买入持有取反:逐日复利不对称。价格跌 52.9%,做空复利赚 99.2%。
    print(f"基准:主力买入持有 {bh * 100:+.1f}%;恒定满仓做空 {short_bh * 100:+.1f}%"
          f"(不是前者取反——逐日复利不对称)")

    groups = rolling_groups(df, ret.index)
    used = [g for g in groups.dropna().unique()]
    print(f"\n滚动重选:每 {RESELECT_M} 个月一次,共出现 {len(used)} 种组合")
    seen = {}
    for d, g in groups.dropna().items():
        seen.setdefault(g, d)
    for g, d0 in sorted(seen.items(), key=lambda kv: kv[1]):
        print(f"  自 {d0:%Y-%m-%d}  {'、'.join(g)}")

    sig = signal_series(df, groups)
    z = zscore(sig)

    # ------------------------------------------------------------ 连续版
    section("A 连续仓位(信号即仓位,参数最少的诊断版)")
    for cap in (1.0, 2.0, 3.0):
        b = backtest_continuous(z, ret, cap=cap)
        g_tot = (1 + b["gross"]).prod() - 1
        n_tot = (1 + b["net"]).prod() - 1
        sharpe = b["net"].mean() / b["net"].std() * np.sqrt(242) if b["net"].std() > 0 else np.nan
        print(f"  cap={cap:.1f}  毛{g_tot * 100:+7.1f}%  净{n_tot * 100:+7.1f}%  "
              f"夏普{sharpe:5.2f}  日均换手{b['pos'].diff().abs().mean():.3f}")

    b = backtest_continuous(z, ret, cap=2.0)
    b["year"] = b.index.year
    print("\n  逐年(cap=2.0,净):")
    for y, s in b.groupby("year")["net"]:
        print(f"    {y}  {((1 + s).prod() - 1) * 100:+7.1f}%   交易日 {len(s)}")

    # ------------------------------------------------------------ 离散版
    section("B 离散进出场(接近实盘)")
    print("  进场 |z|≥enter;出场=反向/消退/止损/持满。做多额外要求过去 20 日是跌的。\n")
    for enter in (0.8, 1.0, 1.2, 1.5):
        tr, _ = backtest_discrete(z, ret, past, enter=enter)
        summarize(tr, f"enter={enter}")

    section("C 参数敏感性(要的是相邻档同向,不是挑峰值)")
    print("  止损档:")
    for stop in (0.04, 0.06, 0.08, 0.10):
        tr, _ = backtest_discrete(z, ret, past, stop=stop)
        summarize(tr, f"stop={stop:.0%}")
    print("  最长持有:")
    for mh in (20, 30, 40, 60):
        tr, _ = backtest_discrete(z, ret, past, max_hold=mh)
        summarize(tr, f"max_hold={mh}")

    section("D 拆解:多空各自表现、出场原因分布、逐年")
    tr, _ = backtest_discrete(z, ret, past)
    for side in ("空", "多"):
        summarize(tr[tr["方向"] == side], f"仅{side}头")
    print("\n  出场原因:")
    for why, sub in tr.groupby("出场原因"):
        print(f"    {why:6s} {len(sub):3d} 笔  均值{sub['收益%'].mean():+6.2f}%  "
              f"胜率{(sub['收益%'] > 0).mean() * 100:5.1f}%")
    print("\n  逐年:")
    tr["年"] = tr["出场"].dt.year
    for y, sub in tr.groupby("年"):
        summarize(sub, f"{y}")

    print("\n  做多那条支路单独看(样本极少,不足以下结论):")
    print(tr[tr["方向"] == "多"][["进场", "出场", "收益%", "持有日", "出场原因"]]
          .to_string(index=False) if (tr["方向"] == "多").any() else "    无做多交易")

    section("E 与「恒定满仓做空」比:超额到底有多少")
    print("  三年单边熊市里,躺着做空就有 +52.9%。策略必须明显赢过它才值得做。\n")

    def stats(daily: pd.Series, label: str):
        eq = (1 + daily.fillna(0)).cumprod()
        dd = (eq / eq.cummax() - 1).min()
        sh = daily.mean() / daily.std() * np.sqrt(242) if daily.std() > 0 else np.nan
        print(f"  {label:26s} 累计{(eq.iloc[-1] - 1) * 100:+8.1f}%  夏普{sh:5.2f}  "
              f"最大回撤{dd * 100:7.1f}%")

    short_only = -ret
    stats(short_only, "恒定满仓做空(基准)")
    for cap in (1.0, 2.0):
        bb = backtest_continuous(z, ret, cap=cap)
        stats(bb["net"], f"连续仓位 cap={cap:.1f}(净)")
    # 离散版的日收益:按持仓状态还原
    tr_d, pos_list = backtest_discrete(z, ret, past)
    pos_s = pd.Series(pos_list, index=ret.index).shift(1).fillna(0)
    turn_d = pos_s.diff().abs().fillna(0)
    stats(pos_s * ret - turn_d * COST_ONEWAY, "离散进出场(净)")
    # 只做空版本:把多头支路关掉,看它是不是拖后腿
    tr_s, pos_s2 = backtest_discrete(z, ret, past, enter=1.0, long_needs_dip=True)
    only_short = pd.Series(pos_s2, index=ret.index).clip(upper=0).shift(1).fillna(0)
    stats(only_short * ret - only_short.diff().abs().fillna(0) * COST_ONEWAY,
          "离散·只做空(净)")

    section("F 成本敏感性(这套策略换手不低,成本假设要经得起怀疑)")
    print(f"  基准假设单边 {COST_ONEWAY:.2%}(LH 一跳 5 元/吨 × 16 吨 ≈ 万 3.8,加手续费)\n")
    for c in (0.0002, 0.0005, 0.0010, 0.0020):
        bb = backtest_continuous(z, ret, cap=2.0, cost=c)
        trc, _ = backtest_discrete(z, ret, past, cost=c)
        tot = (1 + trc["收益%"] / 100).prod() - 1 if not trc.empty else np.nan
        print(f"  单边{c:.2%}  连续净{((1 + bb['net']).prod() - 1) * 100:+7.1f}%   "
              f"离散净{tot * 100:+7.1f}%")

    section("数据已打印,结论写进 REPORT_LH_PHASE2_v1.md")


if __name__ == "__main__":
    main()
