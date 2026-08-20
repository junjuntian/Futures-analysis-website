"""盈亏比能不能当筛选器?——DEC-067 否决过一次,这次换了构造,重测。

DEC-067 否决的是 `历年最有利中位 ÷ 历年最大MAE中位`,**分子分母都是跨起点搬不动
的历年中位数**(DEC-097 已判定不可比),失败时还留了一句「疑与临近交割混杂」。
现在这个是 `到第一目标 ÷ 到止损`,两个距离都量在**今天这张图的今天这个价位**上,
是另一个东西。但换了构造不等于就能用,得测。

**要测对东西**:盈亏比越大 → 目标越远、止损越近 → 先摸到目标的概率必然越低。
这是算术不是信息。无漂移随机游走下,任何盈亏比的**期望**恰好是 0
(P·gain − (1−P)·risk = 0)。所以检验的是:**它偏不偏离这个零**。

赛跑口径:从当日起向前走,先碰到目标档还是先碰到止损档(用收盘价判),
两个都没碰到就走到窗口止点作平。期望以 σ 为单位,跨品种才可加。
"""
from __future__ import annotations

import pathlib

import numpy as np
import pandas as pd

DATA = pathlib.Path(__file__).resolve().parent / "data"
NAMES = {"LH": "生猪", "JM": "焦煤", "JD": "鸡蛋", "FG": "玻璃", "SA": "纯碱", "AP": "AP"}


def window_end(contract: str) -> pd.Timestamp:
    raw = "".join(c for c in str(contract) if c.isdigit())
    yy, mm = 2000 + int(raw[:2]), int(raw[2:])
    d = pd.Timestamp(year=yy, month=mm, day=1) - pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d


def shelves_at(pivots: list[float], band: float) -> list[float]:
    """把已确认的转折位按 0.5σ 并档,返回各档中点(从高到低)。与生产同口径。"""
    if not pivots or not np.isfinite(band) or band <= 0:
        return []
    out, cur = [], [pivots[0]]
    for p in pivots[1:]:
        if cur[-1] - p <= band:
            cur.append(p)
        else:
            out.append(float(np.mean(cur)))
            cur = [p]
    out.append(float(np.mean(cur)))
    return out


def rows_for(code: str, df: pd.DataFrame, step: int = 3) -> list[dict]:
    """逐 (组合, 日) 算出两个方向的 gain/risk/比值,再向前跑赛跑。

    step=3 是抽样:相邻交易日的档位与结果高度重叠,全取只是把同一件事数三遍,
    会把样本量吹大而不增加信息。**这一条要写出来,不能默默抽。**
    """
    out = []
    for (c1, c2), g in df.groupby(["contract_1", "contract_2"], sort=False):
        g = g.sort_values("trade_date")
        end = min(window_end(c1), window_end(c2))
        g = g[g["trade_date"] <= end]
        v = g["spread"].astype(float).to_numpy()
        n = len(v)
        if n < 80:
            continue
        sig = pd.Series(v).diff().rolling(20, min_periods=15).std().to_numpy()
        hi = pd.Series(v).rolling(7, center=True).max().to_numpy()
        lo = pd.Series(v).rolling(7, center=True).min().to_numpy()
        is_piv = np.zeros(n, dtype=bool)
        is_piv[3:n - 3] = (v[3:n - 3] == hi[3:n - 3]) | (v[3:n - 3] == lo[3:n - 3])
        for i in range(40, n - 10, step):
            band = sig[i]
            if not np.isfinite(band) or band <= 0:
                continue
            seen = sorted((v[k] for k in range(i - 2) if is_piv[k]), reverse=True)
            lv = shelves_at(seen, 0.5 * band)
            now = v[i]
            above = [x for x in lv if x > now]
            below = [x for x in lv if x < now]
            if not above or not below:
                continue
            up, dn = min(above), max(below)          # 各自最近的一档
            fwd = v[i + 1:]
            for down in (True, False):
                tgt, stp = (dn, up) if down else (up, dn)
                gain, risk = abs(now - tgt), abs(now - stp)
                if risk <= 0 or gain <= 0:
                    continue
                hit_t = np.argmax(fwd <= tgt) if down else np.argmax(fwd >= tgt)
                hit_s = np.argmax(fwd >= stp) if down else np.argmax(fwd <= stp)
                ok_t = (fwd <= tgt).any() if down else (fwd >= tgt).any()
                ok_s = (fwd >= stp).any() if down else (fwd <= stp).any()
                if ok_t and (not ok_s or hit_t < hit_s):
                    pnl = gain
                elif ok_s:
                    pnl = -risk
                else:
                    pnl = (now - fwd[-1]) if down else (fwd[-1] - now)
                out.append({"inst": code, "ratio": gain / risk,
                            "pnl_sig": pnl / band, "won": pnl > 0,
                            "days_left": n - 1 - i})
    return out


def main() -> None:
    df = pd.read_csv(DATA / "allspreads.csv.gz", parse_dates=["trade_date"])
    cross = df["is_cross_variety"].isin(["t", "true", "True", True])
    df = df[~cross]
    rows = []
    for code, g in df.groupby("instrument_1"):
        rows += rows_for(code, g)
    a = pd.DataFrame(rows)
    print(f"样本 {len(a):,} 个(组合×日×方向,每 3 个交易日抽一次)\n")

    print("=" * 88)
    print("一、先摸到目标的比例 —— 这一列必然随盈亏比下降,是算术不是信息")
    print("=" * 88)
    a["bin"] = pd.cut(a["ratio"], [0, 0.5, 0.8, 1.2, 2, 3, 5, 1e9],
                      labels=["<0.5", "0.5~0.8", "0.8~1.2", "1.2~2", "2~3", "3~5", ">5"])
    t = a.groupby("bin", observed=True).agg(
        n=("won", "size"), 先到目标=("won", "mean"),
        期望σ=("pnl_sig", "mean"))
    t["先到目标"] = (100 * t["先到目标"]).round(1)
    t["期望σ"] = t["期望σ"].round(3)
    print(t.to_string())

    print("\n" + "=" * 88)
    print("二、真正的检验:期望值偏不偏离 0(无漂移随机游走下应当恰好为 0)")
    print("=" * 88)
    print(f"  {'盈亏比':>9s}{'样本':>8s}{'期望(σ)':>10s}{'标准误':>9s}{'t值':>8s}")
    for b, g in a.groupby("bin", observed=True):
        m, se = g["pnl_sig"].mean(), g["pnl_sig"].std(ddof=1) / np.sqrt(len(g))
        print(f"  {str(b):>9s}{len(g):>8,d}{m:>+10.3f}{se:>9.3f}{m/se:>+8.2f}")
    m, se = a["pnl_sig"].mean(), a["pnl_sig"].std(ddof=1) / np.sqrt(len(a))
    print(f"  {'全样本':>9s}{len(a):>8,d}{m:>+10.3f}{se:>9.3f}{m/se:>+8.2f}")

    print("\n" + "=" * 88)
    print("三、DEC-067 那个隐患:比值会不会被剩余期污染")
    print("=" * 88)
    a["T"] = pd.cut(a["days_left"], [0, 20, 40, 80, 1e9],
                    labels=["≤20日", "21~40日", "41~80日", ">80日"])
    print(a.groupby("T", observed=True).agg(
        n=("ratio", "size"), 平均盈亏比=("ratio", "mean"),
        期望σ=("pnl_sig", "mean")).round(3).to_string())
    print("\n  若平均盈亏比随剩余期系统性变化,那 DEC-067 那次「与临近交割混杂」")
    print("  的毛病在这里也会重演 —— 那就必须先把剩余期分开再谈。")


if __name__ == "__main__":
    main()
