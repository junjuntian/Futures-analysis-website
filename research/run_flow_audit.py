"""合计流向三品种(LH/FG/SA)的过拟合与未来函数审查。

**直接 import 生产引擎**,不另写一份实现 —— 审查一份重写的代码毫无意义
(PITFALLS:对比脚本必须先复刻线上那条,再谈别的)。

三件事:
  一、执行时点:席位数据是**收盘后**才公布的,而回放按**当日结算价**成交。
      把进出场都推迟一天重新计价,看差多少。
  二、成本:界面头条的累计收益是**毛的**(逐笔 ret_pct 不含手续费滑点)。
  三、参数邻域与逐年:每个品种**各自**验一遍,不许拿生猪的结论套玻璃。
"""
from __future__ import annotations

import os
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "engine"))
os.environ.setdefault("ENGINE_SOURCE", "csv")
import hog_money as H  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"
COST = 0.0005          # 单边,与引擎 compare 块同值


def section(t: str) -> None:
    print("\n" + "=" * 88)
    print(t)
    print("=" * 88)


def prep(code: str):
    H.use(code)
    price, seat = H.load_from_csv(code, DATA)
    price, seat = H.clean_price(price), H.clean_seat(seat)
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, log = H.rolling_groups(seat, price, mkt.index)
    sig = H.signal_series(seat, groups)
    rdf, _ = H.retail_series(seat, mkt.index)
    price = price.assign(px_open=price["open_price"].replace(0, np.nan))
    mko = open_series(price, mkt["main"]).reindex(mkt.index)
    return mkt, sig, rdf, log, mko


def open_series(price: pd.DataFrame, main: pd.Series) -> pd.DataFrame:
    """主力合约的**开盘价**与开盘到开盘的收益。

    换月日照样用新合约自己的前一日开盘价——跟 main_series 对结算价的处理同一条
    纪律,跨合约相除得到的是价差不是收益。
    """
    op = price.set_index(["contract", "trade_date"])["px_open"].sort_index()
    rows = []
    for d, c in main.items():
        o = op.get((c, d), np.nan)
        hist = op.loc[c] if c in op.index.get_level_values(0) else pd.Series(dtype=float)
        earlier = hist[hist.index < d]
        prev = earlier.iloc[-1] if len(earlier) else np.nan
        r = o / prev - 1.0 if (np.isfinite(o) and np.isfinite(prev) and prev > 0) else np.nan
        rows.append((d, c, o, r))
    return pd.DataFrame(rows, columns=["trade_date", "main", "settle", "ret"]).set_index("trade_date")


def replay_of(code: str, cache: dict):
    """复刻线上那一条。

    **每次都要先 use(code)**:RULES 是全局的,prep 循环跑完之后它停在最后一个
    品种上。2026-08-19 我第一版审查脚本漏了这一句,拿纯碱的规则(做多开着)跑
    生猪,18 笔跑成 36 笔,差点据此得出错误结论。
    """
    H.use(code)
    mkt, sig, rdf, _, mko = cache[code]
    tr, _ = H.replay(sig, mkt, rdf)
    return mkt, mko, [t for t in tr if t.get("exit_date")]


def stats(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    r = np.array(rets)
    return {"n": len(r), "cum": (np.prod(1 + r / 100) - 1) * 100,
            "avg": r.mean(), "win": (r > 0).mean() * 100, "worst": r.min()}


def line(tag: str, s: dict) -> str:
    if not s.get("n"):
        return f"  {tag:24s} 无交易"
    return (f"  {tag:24s} {s['n']:3d}笔 累计{s['cum']:+9.1f}% 均值{s['avg']:+6.2f}% "
            f"胜率{s['win']:5.1f}% 最差{s['worst']:+6.1f}%")


def reprice(trades: list[dict], mkt: pd.DataFrame, lag: int, cost: float) -> list[float]:
    """把进出场同时推迟 lag 天重新计价。

    收益 = 持仓期间逐日收益连乘。mkt["ret"] 已经按「换月用新合约自己的前一日
    结算价」算好,所以直接连乘不会跨合约相除。
    """
    idx = mkt.index
    pos = {d: i for i, d in enumerate(idx)}
    r = mkt["ret"].fillna(0).to_numpy()
    out = []
    for t in trades:
        i = pos[pd.Timestamp(t["entry_date"])] + lag
        j = pos[pd.Timestamp(t["exit_date"])] + lag
        if j >= len(idx):
            continue                      # 推迟后越界的最后一笔丢掉,不猜
        side = 1 if t["side"] == "long" else -1
        seg = r[i + 1:j + 1]
        cum = np.prod(1 + side * seg) - 1
        out.append((cum - 2 * cost if cost else cum) * 100)
    return out


def main() -> None:
    codes = ("LH", "FG", "SA")
    cache = {c: prep(c) for c in codes}

    section("〇、先复刻线上那一条(对不上,下面全部作废)")
    EXPECT = {"LH": 18, "FG": 207, "SA": 101}   # DEC-090 次日开盘成交之后的笔数
    for c in codes:
        _, _, closed = replay_of(c, cache)
        st = stats([t["ret_pct"] for t in closed])
        ok = "对上了" if st["n"] == EXPECT[c] else f"**对不上,引擎是 {EXPECT[c]} 笔**"
        print(f"  {c}: {st['n']} 笔 累计 {st['cum']:+.1f}% 胜率 {st['win']:.1f}%   {ok}")
        assert st["n"] == EXPECT[c], f"{c} 复刻失败"

    section("一、执行时点:席位数据收盘后才公布,回放却按当日结算价成交")
    print("  席位持仓排名不是盘中能看到的:大商所约 15:30-16:00、郑商所约 16:26 才出,")
    print("  我们自己的采集就是 16:00 与 17:30 两轮。所以「按信号日结算价成交」做不到。")
    print("  下面把进出场同时推迟一天(次日结算价成交),看结论会不会翻。\n")
    for c in codes:
        mkt, mko, closed = replay_of(c, cache)
        print(f"  【{c}】{H.VARIETIES[c]['name']}")
        print(line("线上口径:信号日结算价", stats([t["ret_pct"] for t in closed])))
        print(line("次日开盘成交(毛)", stats(reprice(closed, mko, 1, 0))))
        print(line("次日开盘+双边成本", stats(reprice(closed, mko, 1, COST))))
        print(line("次日结算价(毛,最保守)", stats(reprice(closed, mkt, 1, 0))))

    section("二、成本:界面头条的累计收益是毛的")
    print(f"  单边 {COST:.2%}(手续费+滑点),一来一回 {2*COST:.2%}。换手越多啃得越狠。\n")
    for c in codes:
        mkt, _, closed = replay_of(c, cache)
        g = stats([t["ret_pct"] for t in closed])
        n = stats(reprice(closed, mkt, 0, COST))
        print(f"  {c}: 毛 {g['cum']:+9.1f}%  →  净 {n['cum']:+9.1f}%   "
              f"({g['n']} 笔,单笔均值 {g['avg']:+.2f}% → {n['avg']:+.2f}%)")

    section("三、逐年(看是不是靠某一两年撑起来的)")
    for c in codes:
        mkt, _, closed = replay_of(c, cache)
        df = pd.DataFrame(closed)
        if df.empty:
            continue
        df["年"] = pd.to_datetime(df["exit_date"]).dt.year
        parts = []
        for y, sub in df.groupby("年"):
            s = stats(sub["ret_pct"].tolist())
            parts.append(f"{y} {s['cum']:+7.1f}%({s['n']}笔)")
        print(f"  【{c}】" + "  ".join(parts))
        neg = sum(1 for y, sub in df.groupby("年")
                  if stats(sub["ret_pct"].tolist())["cum"] < 0)
        print(f"        为负的年份 {neg}/{df['年'].nunique()}")

    section("四、参数邻域:每个品种各自验,要的是相邻档同向而不是峰值")
    for key, values in (("enter", (0.8, 1.0, 1.2, 1.5)),
                        ("stop", (0.04, 0.06, 0.08, 0.10)),
                        ("max_hold", (20, 30, 40, 60)),
                        ("sig_win", (3, 5, 10)),
                        ("z_win", (60, 120, 250)),
                        ("group_k", (3, 5, 8)),
                        ("reselect_months", (6, 12, 24))):
        print(f"\n  {key}:")
        for v in values:
            row = f"    {str(v):<6}"
            for c in codes:
                H.use(c)
                mkt, sig0, rdf, _, _ = cache[c]
                old = H.RULES[key]
                H.RULES[key] = v
                # 这几个参数会改变席位组或信号本身,必须重算
                if key in ("group_k", "reselect_months", "sig_win", "z_win"):
                    price, seat = H.load_from_csv(c, DATA)
                    price, seat = H.clean_price(price), H.clean_seat(seat)
                    m2 = H.main_series(price)
                    m2 = m2[m2.index >= pd.Timestamp(H.RULES["replay_start"])]
                    g2, _ = H.rolling_groups(seat, price, m2.index)
                    s2 = H.signal_series(seat, g2)
                    r2, _ = H.retail_series(seat, m2.index)
                else:
                    m2, s2, r2 = mkt, sig0, rdf
                tr, _ = H.replay(s2, m2, r2)
                cl = [t["ret_pct"] for t in tr if t.get("exit_date")]
                st = stats(cl)
                H.RULES[key] = old
                row += f"  {c} {st.get('cum', 0):+8.1f}%/{st.get('n', 0):3d}笔"
            print(row)


if __name__ == "__main__":
    main()
