"""散户反向的超额收益,到底落在信号后的哪几天?

前一个猜想(「事件型信号被当连续持仓用」)已被数据否掉:平均只持 11~12 天,
把上限压到 5 天回撤反而更差。所以换个方向查:**edge 落在时间轴的哪里**。

如果超额几乎全在信号后**第一天**,而信号日 15:00 的结算价到次日 09:00 的开盘价
之间那段跳空是拿不到的,那么「指标很准」与「策略回撤大」就能同时成立 ——
指标测的是 D+1 的方向,而能下单的最早时点已经在 D+1 开盘。
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


def main():
    print("=" * 100)
    print("信号触发后,超额收益逐日落在哪里(方向已按信号符号调正,单位:%)")
    print("=" * 100)
    print("  口径:D = 信号日(收盘后才拿得到席位数据)。")
    print("  D→D+1结算 = 老回测吃到的那一天;其中「跳空」= D结算→D+1开盘,**拿不到**;")
    print("  「日内」= D+1开盘→D+1结算,是最早能吃到的一段。\n")
    print(f"  {'品种':6s}{'触发数':>7s}{'D→D+1结算':>11s}{'其中跳空':>10s}{'其中日内':>10s}"
          f"{'D+1→D+5':>10s}{'D+5→D+20':>11s}{'跳空占比':>9s}")
    for c in ("LH", "FG", "SA"):
        mkt, sig, rdf = prep(c)
        H.use(c)
        z_in, _ = H.entry_exit_signals(sig, rdf)
        idx = mkt.index
        ret = mkt["ret"].fillna(0).to_numpy()
        o2c = mkt["o2c"].fillna(0).to_numpy()
        settle = mkt["settle"].to_numpy()
        openp = mkt["open"].to_numpy()
        main_c = mkt["main"].to_numpy()
        rows = []
        for i, d in enumerate(idx):
            z = z_in.get(d, np.nan)
            if not np.isfinite(z) or abs(z) < H.RULES["enter"]:
                continue
            if i + 21 >= len(idx):
                continue
            sd = np.sign(z)
            # 跳空只在没换月时才有意义,换月了 settle 与 open 不是同一个合约
            gap = (openp[i + 1] / settle[i] - 1) if (main_c[i + 1] == main_c[i]
                                                    and np.isfinite(openp[i + 1])
                                                    and np.isfinite(settle[i])) else np.nan
            rows.append({
                "d1": sd * ret[i + 1] * 100,
                "gap": sd * gap * 100 if np.isfinite(gap) else np.nan,
                "intra": sd * o2c[i + 1] * 100,
                "d1_5": sd * (np.prod(1 + ret[i + 2:i + 6]) - 1) * 100,
                "d5_20": sd * (np.prod(1 + ret[i + 6:i + 21]) - 1) * 100,
            })
        df = pd.DataFrame(rows)
        share = df["gap"].mean() / df["d1"].mean() * 100 if df["d1"].mean() else np.nan
        print(f"  {NAMES[c]:6s}{len(df):>7d}{df['d1'].mean():>+11.3f}{df['gap'].mean():>+10.3f}"
              f"{df['intra'].mean():>+10.3f}{df['d1_5'].mean():>+10.3f}{df['d5_20'].mean():>+11.3f}"
              f"{share:>8.0f}%")
    print("\n  最后一列 = 跳空 ÷ D→D+1 全天。这一列越高,说明「指标准」与「策略赚不到」")
    print("  之间的落差越大 —— 准的那部分发生在你还没法下单的时候。")


if __name__ == "__main__":
    main()
