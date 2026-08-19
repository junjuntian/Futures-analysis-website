"""生猪席位能力验证 Phase 0：这些席位到底有没有择时能力,还是只是熊市 beta。

运营者 2026-08-19 立项。要回答的是"能不能做",不是"怎么做"——所以只出数,
不写策略。四问:

  Q1 累计盈亏排行里的钱,是方向暴露(beta)还是择时(alpha)?
  Q2 加减仓事件对未来收益有没有预测力?扣掉 beta 之后还剩多少?
  Q3 三年样本能不能撑住逐年检验?
  Q4 样本里机构到底有没有转过向?(运营者要跟的正是"空转多"那个拐点)

一律**逐合约**:LH 各合约相对主力偏离最大 49%,跨合约算收益就是在算价差。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import lhlib as L

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 50)

TOP_N = 12          # 只看在榜够久的前若干家
MIN_SEAT_DAYS = 200  # 席位入选门槛(席位×合约×日 计数)
HORIZONS = (5, 10, 20)


def section(title: str):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main():
    price = L.load_price()
    seat = L.load_seat()
    fwd = L.forward_returns(price, HORIZONS)
    mc = L.main_contract(price)

    print(f"行情 {len(price):,} 行 / {price['trade_date'].nunique()} 交易日 "
          f"/ {price['contract'].nunique()} 合约  "
          f"[{price['trade_date'].min():%Y-%m-%d} ~ {price['trade_date'].max():%Y-%m-%d}]")
    print(f"席位 {len(seat):,} 行 / {seat['member_key'].nunique()} 家")

    df = seat.merge(fwd[["contract", "trade_date", "settle", "px", "open_interest"]
                        + [f"fwd{h}" for h in HORIZONS]],
                    on=["contract", "trade_date"], how="inner")

    # ---------------------------------------------------------------- Q4 先答
    # 机构方向:每天把「在榜席位」的净持仓按合约加总,看多空阵营的构成随时间怎么变。
    section("Q4 三年里机构转过向吗?(要跟的正是这个拐点)")
    big = (df.groupby("member_key")["trade_date"].nunique()
             .pipe(lambda s: s[s >= MIN_SEAT_DAYS]).index)
    top_pnl = pnl_rank(df)
    smart = [m for m in top_pnl.head(8).index if m in big]
    print(f"用盈亏前 8 家做「机构」代表: {'、'.join(smart)}")

    inst = (df[df["member_key"].isin(smart)]
            .groupby("trade_date")["net"].sum().sort_index())
    q = inst.resample("QE").mean()
    print("\n季度平均合计净持仓(负=净空):")
    for d, v in q.items():
        bar = "#" * int(min(abs(v) / 400, 40))
        print(f"  {d.year}-Q{(d.month - 1) // 3 + 1}  {v:>10,.0f}  {'空' if v < 0 else '多'} {bar}")
    flips = ((inst > 0) != (inst.shift() > 0)).sum()
    print(f"\n合计净持仓穿越零轴次数: {flips}")
    print(f"净多天数 {int((inst > 0).sum())} / 净空天数 {int((inst < 0).sum())}")

    # ---------------------------------------------------------------- Q1
    section("Q1 盈亏排行是 alpha 还是 beta?")
    print("按持仓方向拆:如果一家的钱全来自「一直挂着空单 × 市场在跌」,")
    print("那它的净持仓与收益的相关性会很高,但增减仓与未来收益无关。\n")
    print(top_pnl.head(TOP_N).to_string())

    # ---------------------------------------------------------------- Q2
    section("Q2 加减仓事件的预测力(扣 beta 前后)")
    ev = events(df)
    print(f"事件定义: |dnet| 进入该席位自身历史 80 分位,且该席位在该合约在榜 ≥ 60 日")
    print(f"事件总数 {len(ev):,}\n")
    tbl = event_power(ev, df)
    print(tbl.head(TOP_N).to_string())

    # ---------------------------------------------------------------- Q3
    section("Q3 逐年是否稳定(三年样本能不能撑住)")
    yr = by_year(ev)
    print(yr.to_string())

    # ---------------------------------------------------------------- Q5
    section("Q5 「机构合计方向」的预测力(运营者要的趋势跟随,核心就是这个)")
    inst_signal(df, smart, fwd, mc)

    section("小结数据已打印,结论写进 REPORT_LH_SKILL_v1.md")


def inst_signal(df, smart, fwd, mc):
    """机构合计净持仓的**变化**能不能预测主力合约的未来收益。

    水平不能用:三年全程净空,水平永远是负的,当信号等于常数。运营者要跟的是
    「机构在往哪个方向调仓」,所以测的是变化——净空加深 = 看跌加码,净空收敛 =
    看跌减弱(离转多最近的形态)。

    收益一律取**当日主力合约自己**的前向收益,不跨合约(LH 合约间差最大 49%)。
    """
    inst = df[df["member_key"].isin(smart)].groupby("trade_date")["net"].sum().sort_index()

    # 主力合约的前向收益,按日对齐
    m = mc.set_index("trade_date")["main"]
    px = fwd.set_index(["contract", "trade_date"])
    rows = []
    for d, c in m.items():
        if (c, d) in px.index:
            r = px.loc[(c, d)]
            rows.append((d, r["fwd5"], r["fwd10"], r["fwd20"]))
    mret = pd.DataFrame(rows, columns=["trade_date", "fwd5", "fwd10", "fwd20"]).set_index("trade_date")

    for win in (5, 20):
        chg = inst.diff(win)
        j = pd.concat([chg.rename("chg"), mret], axis=1).dropna()
        if len(j) < 60:
            continue
        print(f"\n信号 = 机构合计净持仓 {win} 日变化 (N={len(j)})")
        print(f"  {'':10s}" + "".join(f"{f'fwd{h}':>12s}" for h in HORIZONS))
        # 相关系数:负相关才是对的——净空加深(chg<0)之后应当下跌(fwd<0)
        corr = [j["chg"].corr(j[f"fwd{h}"]) for h in HORIZONS]
        print(f"  {'相关系数':10s}" + "".join(f"{c:>12.3f}" for c in corr))
        # 五档:按信号分位分组看未来收益,单调才算有关系
        j["bucket"] = pd.qcut(j["chg"], 5, labels=["最空", "偏空", "中", "偏多", "最多"])
        g = j.groupby("bucket", observed=True)[[f"fwd{h}" for h in HORIZONS]].mean() * 100
        for b, r in g.iterrows():
            print(f"  {b:10s}" + "".join(f"{r[f'fwd{h}']:>11.2f}%" for h in HORIZONS))


def pnl_rank(df: pd.DataFrame) -> pd.DataFrame:
    """逐 (席位, 合约) 按结算价推算累计盈亏,并拆出 beta 归因。

    盈亏 = Σ (settle_t − settle_{t-1}) × net_{t-1} × 点值。掉榜断档的那些天不计
    (prev 与当日间隔超过 5 个自然日就断开),缺失是未知不是零。
    """
    d = df.sort_values(["member_key", "contract", "trade_date"]).copy()
    g = d.groupby(["member_key", "contract"])
    d["prev_net"] = g["net"].shift()
    d["prev_settle"] = g["settle"].shift()
    d["gap"] = (d["trade_date"] - g["trade_date"].shift()).dt.days
    ok = d["prev_net"].notna() & (d["gap"] <= 5)
    d.loc[ok, "pnl"] = (d["settle"] - d["prev_settle"]) * d["prev_net"] * L.MULTIPLIER

    out = d[ok].groupby("member_key").agg(
        pnl_亿=("pnl", lambda s: s.sum() / 1e8),
        在榜日=("trade_date", "nunique"),
        平均净=("prev_net", "mean"),
    )
    # beta 对照:把这家的净持仓换成「常数 = 它自己的平均净持仓」再算一遍。
    # 这就是"从头到尾挂着同样大小的仓不动"能赚到的钱——两者之差才是择时。
    beta = []
    for m, sub in d[ok].groupby("member_key"):
        const = sub["prev_net"].mean()
        beta.append(((sub["settle"] - sub["prev_settle"]) * const * L.MULTIPLIER).sum() / 1e8)
    out["beta_亿"] = beta
    out["alpha_亿"] = out["pnl_亿"] - out["beta_亿"]
    out = out[out["在榜日"] >= MIN_SEAT_DAYS].sort_values("pnl_亿", ascending=False)
    return out.round(2)


def events(df: pd.DataFrame) -> pd.DataFrame:
    """显著加减仓事件:|dnet| 超过该席位自身历史 80 分位。

    用席位自己的历史做门槛而不是全市场统一值——各家体量差一个量级,
    统一门槛会让大席位天天有事件、小席位永远没有。
    """
    d = df[df["dnet"].notna() & (df["dnet"] != 0)].copy()
    d = d.sort_values(["member_key", "trade_date"])
    absd = d.groupby("member_key")["dnet"].transform(lambda s: s.abs())
    thr = d.groupby("member_key")["dnet"].transform(
        lambda s: s.abs().expanding(min_periods=60).quantile(0.80))
    return d[(absd >= thr) & thr.notna()].copy()


def event_power(ev: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """事件后的前向收益:按事件方向取号。

    加多(dnet>0)看正向收益,加空(dnet<0)看反向收益——把两者统一成
    「顺着这家的动作做,赚不赚」。同时给出扣掉同期市场平均涨跌的超额。
    """
    mkt = {h: df.groupby("trade_date")[f"fwd{h}"].mean() for h in HORIZONS}
    rows = []
    for m, sub in ev.groupby("member_key"):
        if len(sub) < 30:
            continue
        sign = np.sign(sub["dnet"])
        row = {"事件数": len(sub)}
        for h in HORIZONS:
            r = sign * sub[f"fwd{h}"]
            base = sign * sub["trade_date"].map(mkt[h])
            excess = (r - base).dropna()
            row[f"{h}日%"] = r.mean() * 100
            row[f"{h}日超额%"] = excess.mean() * 100
            if len(excess) > 5 and excess.std() > 0:
                row[f"{h}日t"] = excess.mean() / (excess.std() / np.sqrt(len(excess)))
        rows.append(pd.Series(row, name=m))
    out = pd.DataFrame(rows)
    return out.sort_values("20日超额%", ascending=False).round(2)


def by_year(ev: pd.DataFrame) -> pd.DataFrame:
    """逐年看事件数与方向命中率——三年样本最怕的就是全靠某一年撑着。"""
    d = ev.copy()
    d["year"] = d["trade_date"].dt.year
    sign = np.sign(d["dnet"])
    d["r20"] = sign * d["fwd20"]
    g = d.groupby("year")["r20"]
    return pd.DataFrame({
        "事件数": g.size(),
        "均值%": (g.mean() * 100).round(2),
        "胜率%": (d.groupby("year")["r20"].apply(lambda s: (s > 0).mean()) * 100).round(1),
    })


if __name__ == "__main__":
    main()
