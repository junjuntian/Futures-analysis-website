"""散户反向明明是好的反向指标,为什么回撤这么大?

运营者 2026-08-19 的问题。DD 拆解已经确定:回撤变大 100% 来自成交口径
(旧的日净值 bug 不影响回撤)。剩下的问题是**为什么这个信号扛不住少掉第一天**。

猜想:**这个信号是「事件型」的,却被当成「连续持仓型」在用。**
早先验证散户反向用的是事件研究——「共振 + 极端」触发后 **20 日**的超额收益,
而且明说过「两端对、中间乱,五档里中间三档基本没信息」。
而引擎的出场是「等反向极值出现」(FG 207 笔里 181 笔是反向出场),
于是仓位从一个极值一直扛到相反极值,**中间那段没信息的路全都持着**。

三个检验:
  一、在场时间与出场原因 —— 是不是真的几乎一直在场;
  二、把 max_hold 压短 —— 若回撤随之显著变小,猜想成立;
  三、回撤发生在什么时候 —— 是一次性事故还是常态。
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
NAMES = {"LH": "生猪", "FG": "玻璃", "SA": "纯碱"}


def prep(code):
    H.use(code)
    price, seat = H.load_from_csv(code, DATA)
    price, seat = H.clean_price(price), H.clean_seat(seat)
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    g, _, _ = H.rolling_groups(seat, price, mkt.index)
    sig = H.signal_series(seat, g)
    rdf, _ = H.retail_series(seat, mkt.index)
    return mkt, sig, rdf


def run(code, cache, **over):
    H.use(code)
    for k, v in over.items():
        H.RULES[k] = v
    mkt, sig, rdf = cache[code]
    tr, _, daily = H.replay(sig, mkt, rdf)
    eq = (1 + daily).cumprod()
    closed = [t for t in tr if t.get("exit_date")]
    r = np.array([t["ret_pct"] for t in closed]) if closed else np.array([0.0])
    return {
        "n": len(closed), "cum": (float(eq.iloc[-1]) - 1) * 100,
        "dd": float((eq / eq.cummax() - 1).min()) * 100,
        "sharpe": float(daily.mean() / daily.std() * np.sqrt(242)) if daily.std() > 0 else np.nan,
        "expo": float((daily != 0).mean()) * 100,
        "hold": float(np.mean([t["hold_days"] for t in closed])) if closed else 0.0,
        "avg": r.mean(), "win": (r > 0).mean() * 100,
        "reasons": pd.Series([t["exit_reason"] for t in closed]).value_counts().to_dict(),
        "eq": eq, "trades": closed,
    }


def main():
    cache = {c: prep(c) for c in ("LH", "FG", "SA")}

    print("=" * 96)
    print("一、现状:在场时间与出场原因")
    print("=" * 96)
    print("  这个信号当初是按**事件**验证的(触发后 20 日的超额收益),")
    print("  而引擎是**扛到反向极值才走**。先看看这意味着什么。\n")
    for c in ("LH", "FG", "SA"):
        o = run(c, cache)
        rs = "、".join(f"{k} {v}笔" for k, v in o["reasons"].items())
        print(f"  {NAMES[c]}:在场 {o['expo']:.0f}% 的交易日,平均持有 {o['hold']:.0f} 天;出场原因 {rs}")

    print("\n" + "=" * 96)
    print("二、把持有上限压短(其余不动),看回撤跟不跟着降")
    print("=" * 96)
    print(f"  {'品种':6s}{'持有上限':>9s}{'笔数':>6s}{'累计%':>10s}{'回撤%':>9s}{'夏普':>7s}{'在场%':>7s}{'单笔均值%':>10s}")
    for c in ("LH", "FG", "SA"):
        base = H.VARIETIES[c]
        for h in (5, 10, 15, 20, 40):
            o = run(c, cache, max_hold=h)
            head = f"  {NAMES[c]:6s}" if h == 5 else " " * 8
            star = " ←现行" if h == 40 else ""
            print(f"{head}{h:>9d}{o['n']:>6d}{o['cum']:>+10.1f}{o['dd']:>+9.1f}"
                  f"{o['sharpe']:>7.2f}{o['expo']:>7.0f}{o['avg']:>+10.2f}{star}")
        H.use(c); H.RULES["max_hold"] = base.get("max_hold", 40)
        print()

    print("=" * 96)
    print("三、回撤发生在什么时候(现行参数)")
    print("=" * 96)
    for c in ("LH", "FG", "SA"):
        o = run(c, cache)
        eq = o["eq"]
        dd = eq / eq.cummax() - 1
        low = dd.idxmin()
        peak = eq.loc[:low].idxmax()
        rec = dd.loc[low:]
        back = rec[rec >= -0.01]
        healed = back.index[0].strftime("%Y-%m-%d") if len(back) else "至今未回本"
        yr = dd.groupby(dd.index.year).min() * 100
        worst = "、".join(f"{y} {v:.0f}%" for y, v in yr.sort_values().head(4).items())
        print(f"  {NAMES[c]}:最大回撤 {dd.min()*100:.1f}%,从 {peak:%Y-%m-%d} 跌到 {low:%Y-%m-%d},"
              f"回到高点 {healed}")
        print(f"          逐年最深回撤(最差四年):{worst}")


if __name__ == "__main__":
    main()
