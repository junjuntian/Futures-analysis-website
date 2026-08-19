"""鸡蛋 JD / 焦煤 JM 能不能照生猪那套做「跟随聪明钱」?

**先验再接**。PITFALLS:同一套规则换个品种必须重新验一遍,不许照抄——
玻璃当初就是带着生猪的 long_needs_dip 跑,少了几十笔。

这两个品种的席位数据与生猪同起点(2023-08-11,大商所),样本同样只有三年。
逐项看:①席位组能不能选出来;②方案 C 在两个开关的四种组合下各是什么样;
③首日超额有多少落在拿不到的跳空里(DEC-091 那一课,决定「能不能做」)。
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
CODES = ("LH", "JD", "JM")


def prep(code):
    H.use(code)
    price, seat = H.load_from_csv(code, DATA)
    price, seat = H.clean_price(price), H.clean_seat(seat)
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    g, log, _ = H.rolling_groups(seat, price, mkt.index)
    sig = H.signal_series(seat, g)
    rdf, have = H.retail_series(seat, mkt.index)
    return mkt, sig, rdf, log, have


def stat(code, cache, **over):
    H.use(code)
    for k, v in over.items():
        H.RULES[k] = v
    mkt, sig, rdf, _, _ = cache[code]
    tr, _, daily = H.replay(sig, mkt, rdf)
    closed = [t for t in tr if t.get("exit_date")]
    if not closed:
        return None
    r = np.array([t["ret_pct"] for t in closed])
    eq = (1 + daily).cumprod()
    t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 2 and r.std(ddof=1) > 0 else np.nan
    return {"n": len(r), "cum": (float(eq.iloc[-1]) - 1) * 100,
            "dd": float((eq / eq.cummax() - 1).min()) * 100,
            "sh": float(daily.mean() / daily.std() * np.sqrt(242)) if daily.std() > 0 else np.nan,
            "win": (r > 0).mean() * 100, "avg": r.mean(), "t": t,
            "long": sum(1 for x in closed if x["side"] == "long")}


def main():
    cache = {c: prep(c) for c in CODES}

    print("=" * 100)
    print("一、数据与席位:能不能选出组来")
    print("=" * 100)
    for c in CODES:
        mkt, sig, rdf, log, have = cache[c]
        H.use(c)
        dev = None
        print(f"  {H.VARIETIES[c]['name']}:回放 {mkt.index.min():%Y-%m-%d}~{mkt.index.max():%Y-%m-%d}"
              f"({len(mkt)} 个交易日);换人 {len(log)} 次;"
              f"当前组 {'、'.join(log[-1]['members'][:5]) if log else '选不出'}")
        print(f"      散户三家在榜的:{'、'.join(have) if have else '一家都没有'};"
              f"信号可用天数 {int(sig['z'].notna().sum())};散户信号可用 {int(rdf['rz'].notna().sum())}")

    print("\n" + "=" * 100)
    print("二、方案 C 在两个开关的四种组合下(次日开盘成交,与线上同口径)")
    print("=" * 100)
    print(f"  {'品种':10s}{'做多':>5s}{'要dip':>6s}{'笔数':>6s}{'其中多':>7s}{'累计%':>9s}"
          f"{'回撤%':>8s}{'夏普':>7s}{'胜率%':>7s}{'单笔%':>7s}{'t值':>7s}")
    for c in CODES:
        for le in (False, True):
            for dip in (False, True):
                if not le and dip:
                    continue          # 做多关着时 dip 无意义
                o = stat(c, cache, long_enabled=le, long_needs_dip=dip)
                if not o:
                    continue
                head = f"  {H.VARIETIES[c]['name']:10s}" if (le, dip) == (False, False) else " " * 12
                print(f"{head}{'开' if le else '关':>5s}{('是' if dip else '否') if le else '—':>6s}"
                      f"{o['n']:>6d}{o['long']:>7d}{o['cum']:>+9.1f}{o['dd']:>+8.1f}"
                      f"{o['sh']:>7.2f}{o['win']:>7.1f}{o['avg']:>+7.2f}{o['t']:>+7.2f}")
        print()

    print("=" * 100)
    print("三、首日超额有多少落在拿不到的跳空里(DEC-091:这一列决定能不能做)")
    print("=" * 100)
    print(f"  {'品种':10s}{'触发数':>7s}{'首日超额%':>10s}{'跳空%':>8s}{'日内%':>8s}{'跳空占比':>9s}")
    for c in CODES:
        H.use(c)
        mkt, sig, rdf, _, _ = cache[c]
        e = H.edge_split(sig, mkt, rdf)
        if not e:
            print(f"  {H.VARIETIES[c]['name']:10s}   样本不足")
            continue
        print(f"  {H.VARIETIES[c]['name']:10s}{e['n']:>7d}{e['day1_pct']:>+10.3f}"
              f"{e['gap_pct']:>+8.3f}{e['intraday_pct']:>+8.3f}{e['gap_share_pct']:>8.0f}%")


if __name__ == "__main__":
    main()
