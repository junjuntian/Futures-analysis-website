"""跳空「看着不大」,为什么把玻璃纯碱的收益砍掉九成?

运营者 2026-08-19 的追问。两个放大器:
  ① **两头各丢一次**。进场时错过顺向的跳空,出场时又吃下反向的跳空——
     出场信号是「反向极值」,那个反向本身也有一部分发生在跳空里。
  ② **连乘**。玻璃 13 年 207 笔,单笔差 1 个百分点,连乘完就是几十倍的差距。

口径:gap_t = open_{t+1}/settle_t − 1(换月日跳过,那不是跳空是换合约)。
     旧口径收益 = settle_i→settle_j;新口径 = open_{i+1}→open_{j+1}。
     两者之差 ≈ (出场跳空 − 进场跳空),方向都按持仓方向调正。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "engine"))
os.environ.setdefault("ENGINE_SOURCE", "csv")
import hog_money as H  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"
SC = pathlib.Path(os.environ["SCRATCH"])
NAMES = {"LH": "生猪", "FG": "玻璃", "SA": "纯碱"}
FILES = {"LH": "hog_signals.json", "FG": "fg_signals.json", "SA": "sa_signals.json"}


def mkt_of(code):
    H.use(code)
    price, seat = H.load_from_csv(code, DATA)
    price, seat = H.clean_price(price), H.clean_seat(seat)
    m = H.main_series(price)
    return m[m.index >= pd.Timestamp(H.RULES["replay_start"])]


def main():
    print("=" * 98)
    print("一、跳空本身有多大(全样本,不分方向)")
    print("=" * 98)
    print(f"  {'品种':6s}{'跳空绝对值中位':>15s}{'跳空标准差':>12s}{'日波动(结→结)':>15s}{'跳空/日波动':>12s}")
    for c in ("LH", "FG", "SA"):
        m = mkt_of(c)
        gap = (m["open"] / m["settle"].shift(1) - 1).where(m["main"] == m["main"].shift(1))
        gap = gap.dropna()
        dv = m["ret"].std()
        print(f"  {NAMES[c]:6s}{gap.abs().median()*100:>14.2f}%{gap.std()*100:>11.2f}%"
              f"{dv*100:>14.2f}%{gap.std()/dv:>12.2f}")
    print("\n  跳空绝对值中位数确实只有零点几个百分点 —— 运营者说的「不大」是对的。")
    print("  问题在于它要跟**策略的单笔利润**比,而不是跟价格比。\n")

    print("=" * 98)
    print("二、两头各丢一次:拿旧口径的每一笔,把差额拆开")
    print("=" * 98)
    print(f"  {'品种':6s}{'笔数':>5s}{'旧单笔均值':>11s}{'进场跳空(丢)':>13s}{'出场跳空(吃)':>13s}"
          f"{'合计影响':>10s}{'新单笔均值':>11s}")
    for c in ("LH", "FG", "SA"):
        m = mkt_of(c)
        idx = m.index
        loc = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(idx)}
        settle, openp, main_c = m["settle"].to_numpy(), m["open"].to_numpy(), m["main"].to_numpy()

        def gap(i):
            if i + 1 >= len(idx) or main_c[i + 1] != main_c[i]:
                return np.nan
            if not (np.isfinite(openp[i + 1]) and np.isfinite(settle[i]) and settle[i] > 0):
                return np.nan
            return openp[i + 1] / settle[i] - 1

        old = json.loads((SC / "oldout" / FILES[c]).read_text(encoding="utf-8"))
        new = json.loads((SC / "flowout" / FILES[c]).read_text(encoding="utf-8"))
        ent, ext, olds = [], [], []
        for t in old["history"]:
            if not t.get("exit_date"):
                continue
            i, j = loc[t["entry_date"]], loc[t["exit_date"]]
            sd = 1.0 if t["side"] == "long" else -1.0
            ge, gx = gap(i), gap(j)
            if not (np.isfinite(ge) and np.isfinite(gx)):
                continue
            ent.append(sd * ge * 100)
            ext.append(sd * gx * 100)
            olds.append(t["ret_pct"])
        ne = np.mean([t["ret_pct"] for t in new["history"] if t.get("exit_date")])
        me, mx = np.mean(ent), np.mean(ext)
        print(f"  {NAMES[c]:6s}{len(ent):>5d}{np.mean(olds):>+10.2f}%{-me:>+12.2f}%{mx:>+12.2f}%"
              f"{(mx - me):>+9.2f}%{ne:>+10.2f}%")
    print("\n  「进场跳空(丢)」= 进场那一跳本来顺着你,现在吃不到,记负;")
    print("  「出场跳空(吃)」= 出场信号是反向极值,那个反向也有一部分在跳空里,现在要吃下来。")
    print("  **两头同向扣,合计约等于单笔均值的下降幅度。**\n")

    print("=" * 98)
    print("三、连乘:单笔差一点点,乘上百次就是天壤之别")
    print("=" * 98)
    print(f"  {'品种':6s}{'笔数':>5s}{'旧单笔':>9s}{'新单笔':>9s}{'旧连乘':>12s}{'新连乘':>12s}{'倍数差':>9s}")
    for c in ("LH", "FG", "SA"):
        old = json.loads((SC / "oldout" / FILES[c]).read_text(encoding="utf-8"))
        new = json.loads((SC / "flowout" / FILES[c]).read_text(encoding="utf-8"))
        ro = [t["ret_pct"] for t in old["history"] if t.get("exit_date")]
        rn = [t["ret_pct"] for t in new["history"] if t.get("exit_date")]
        co = np.prod([1 + r / 100 for r in ro])
        cn = np.prod([1 + r / 100 for r in rn])
        print(f"  {NAMES[c]:6s}{len(ro):>5d}{np.mean(ro):>+8.2f}%{np.mean(rn):>+8.2f}%"
              f"{(co-1)*100:>+11.0f}%{(cn-1)*100:>+11.0f}%{co/cn:>8.1f}x")
    print("\n  生猪只有 18 笔,单笔差 0.24 个点 → 总差不到 9 个点。")
    print("  玻璃 207 笔,单笔差 1.1 个点 → 连乘之后差 11 倍。**笔数才是放大器。**")


if __name__ == "__main__":
    main()
