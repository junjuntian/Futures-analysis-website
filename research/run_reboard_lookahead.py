"""生产引擎的回测里,有多少是靠「回榜反推」这些未来数据撑起来的?

**起因**:做出场对照实验时,我的基准跑出玻璃 −12.4%/235 笔,而文档记的是
+442%/214 笔 —— 差得离谱。查下来我漏了两件生产会做的事,其中第二件很严重:

  1. 生产在算 groups/signals **之前**先把 `mkt` 裁到 `RULES["replay_start"]`;
  2. **生产用的是全部席位数据,包含 `source='reboard_inferred'`。**

第 2 条不只是让基准跑偏。那些行是**回榜日之后才写进库的**:
`fix-sanhe-fabricated-changes.sql` 里的生产实例 ——

    04-16  822 (+194)   在榜,交易所原始
    04-17  215 (−822)   掉榜。215 由 **04-18** 的增减倒推
    04-18  824 (+609)   回榜;824 − 609 = 215 印证

04-17 那天实盘看不到 215,它是 04-18 才算得出来的。**回测能看到,实盘看不到**
—— 这是教科书式的前视。`research/PITFALLS.md` 第 4 条早就写着「做严格点时回放
要用 `reboard_visibility.csv.gz` 的 `reboard_date` 当可见日门」,但生产引擎
(`clean_seat`)只按来源优先级去重,**没有排除也没有按可见日门控**。

这个脚本只做一件事:**同一套代码、同一套参数,只把 `reboard_inferred` 拿掉,
看回测差多少。** 差得少 = 这条前视无关紧要;差得多 = 现有那些回测数字要重估。

**先复现生产**:不复现就没有基准可比,这是仓库的既有纪律
(`docs/PITFALLS.md`「先复现生产,断言,否则中止」)。
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"
CODES = ["LH", "FG", "SA", "JD", "JM"]


def backtest(code: str, drop_reboard: bool) -> dict:
    """**逐字照抄 `run_one` 的序列**,只在 clean_seat 之前多一道过滤。

    照抄而不是自己拼:口径一分叉,这个对比就没有意义了。
    """
    H.use(code)
    low = code.lower()
    price_raw = pd.read_csv(DATA / f"{low}_price.csv.gz")
    seat_raw = pd.read_csv(DATA / f"{low}_seat.csv.gz")
    dropped = 0
    if drop_reboard:
        dropped = int((seat_raw["source"] == "reboard_inferred").sum())
        seat_raw = seat_raw[seat_raw["source"] != "reboard_inferred"].copy()

    price = H.clean_price(price_raw)
    seat = H.clean_seat(seat_raw)
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, _, _ = H.rolling_groups(seat, price, mkt.index)
    sig = H.signal_series(seat, groups)
    rdf, _ = H.retail_series(seat, mkt.index)
    trades, _, daily = H.replay(sig, mkt, rdf, op, st)

    closed = [t for t in trades if t["exit_date"]]
    cum = (float(np.prod([1 + t["ret_pct"] / 100 for t in closed])) - 1) * 100 if closed else 0.0
    wins = sum(1 for t in closed if t["ret_pct"] > 0)
    eq = (1 + daily).cumprod()
    dd = float((eq / eq.cummax() - 1).min() * 100)
    return {"trades": len(closed), "cum": cum,
            "win": wins / len(closed) * 100 if closed else 0.0,
            "dd": dd, "dropped": dropped}


def main() -> None:
    print(__doc__.split("\n")[0])
    print(f"\n{'品种':10s}{'口径':14s}{'笔数':>6s}{'累计':>10s}{'胜率':>8s}{'回撤':>9s}"
          f"   文档记录(VARIETIES.backtest)")
    print("=" * 118)
    for code in CODES:
        keep = backtest(code, drop_reboard=False)
        drop = backtest(code, drop_reboard=True)
        doc = H.VARIETIES[code]["backtest"]
        print(f"{H.VARIETIES[code]['name']:10s}{'含回榜反推(生产)':14s}"
              f"{keep['trades']:>6d}{keep['cum']:>+9.1f}%{keep['win']:>7.1f}%"
              f"{keep['dd']:>8.1f}%   {doc}")
        print(f"{'':10s}{'**排除反推**':14s}"
              f"{drop['trades']:>6d}{drop['cum']:>+9.1f}%{drop['win']:>7.1f}%"
              f"{drop['dd']:>8.1f}%   (排掉 {drop['dropped']:,} 行)")
        delta = drop["cum"] - keep["cum"]
        share = delta / keep["cum"] * 100 if keep["cum"] else float("nan")
        print(f"{'':10s}{'差':14s}{drop['trades'] - keep['trades']:>+6d}"
              f"{delta:>+9.1f}%{'':>7s}{'':>8s}   → 累计变化 {share:+.0f}%")
        print("-" * 118)
    print("\n怎么读:「含回榜反推」那一行应当与文档记录对得上 —— 对不上说明我没复现生产,")
    print("后面的对比就不作数。对得上之后,两行的差就是**这条前视贡献了多少**。")


if __name__ == "__main__":
    main()
