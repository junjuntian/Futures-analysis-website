"""挑轮次:哪些机构建仓值得跟?—— 判据必须在决定的那一刻就算得出来。

`REPORT_FOLLOW_BUILD_v1` 的结论:机构确实赚钱(每轮 +0.48%~+2.21%、胜率 62%~78%),
但「晚一天 + 手续费」的摩擦是 0.46~1.21pp,**与机构利润同一个量级**,
所以只有生猪玻璃跟得下来。问题因此从「怎么拿到成本优势」(结构上无解)变成
**「怎么事先分辨出利润够大的轮次」**。

设计上的硬约束:**判据只能用观察点之前的数据**。所以先定一个固定的观察点 ——
一轮新方向开始后看 K 天,第 K+1 天开盘才跟。这样每个判据都是当场可算的。

顺带回答一个独立的问题:**等几天再跟,是更好还是更差?**
等待换来信息,代价是更差的价格 —— 这两股力量谁大,数据说了算。

候选判据(全部在第 K 天可算):
  · 建仓速度  第 K 天的持仓 ÷ K,再除以该品种的典型规模(可跨品种比)
  · 建仓规模  第 K 天的持仓 ÷ 这组人过去若干轮峰值的中位(他们这次下手重不重)
  · 历史胜率  这组人**之前**几轮赚钱的比例(严格只用过去的轮次)
  · 在榜家数  第 K 天站在这个方向上的席位数(几家一致)
  · 当时波动  近 20 日日波动 ÷ 价格(摩擦大致随波动放大)

纪律:跨品种合并看形状,再逐品种看符号;最后按时间切样本外。
轮次数本来就少(生猪 18、焦煤 24),分档不能太细,只分三档。
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
from run_follow_build import regimes  # noqa: E402
from run_inst_cost import group_book  # noqa: E402

CODES = ["LH", "FG", "SA", "JD", "JM"]
FEE = 2 * H.COST * 100          # 往返手续费,百分点
WAITS = [0, 3, 5, 10]           # 观察几天再跟


def follow_from(book: pd.DataFrame, a: int, b: int, k: int) -> float | None:
    """观察 k 天后跟入,随他补仓,轮次结束时次日开盘平掉。返回净收益(百分点)。

    进出场都罚一天(DEC-090)。k=0 就是上一轮那个「一开始就跟」。
    """
    net = book["net"].to_numpy()
    op = book["open"].to_numpy()
    if a + k > b:
        return None
    sign = 1.0 if net[a] > 0 else -1.0
    lots = cost = 0.0
    prev = abs(net[a + k]) if np.isfinite(net[a + k]) else 0.0
    # 第 K 天当时的持仓当作我们的**首笔**,按第 K+1 天开盘成交
    if prev > 0 and a + k + 1 <= b and np.isfinite(op[a + k + 1]):
        lots, cost = prev, op[a + k + 1]
    for i in range(a + k + 1, b + 1):
        n = net[i]
        if not np.isfinite(n):
            continue
        add = abs(n) - prev
        prev = abs(n)
        if add <= 0 or i + 1 > b or not np.isfinite(op[i + 1]):
            continue
        cost = (cost * lots + op[i + 1] * add) / (lots + add)
        lots += add
    if lots == 0:
        return None
    ex = op[b + 1] if b + 1 < len(op) and np.isfinite(op[b + 1]) else np.nan
    if not np.isfinite(ex):
        return None
    return sign * (ex - cost) / cost * 100 - FEE


def rounds_of(code: str, k: int) -> pd.DataFrame:
    """一个品种的全部轮次,带上第 k 天可算的那几个判据。"""
    book = group_book(code)
    net = book["net"].to_numpy()
    px = book["settle"]
    sigma = (px.diff().rolling(20, min_periods=10).std() / px).to_numpy()
    legs = book["n"].to_numpy()
    segs = regimes(book["net"])
    typical = np.nanmedian([np.nanmax(np.abs(net[a:b + 1])) for a, b in segs]) or 1.0

    rows = []
    prior: list[float] = []                 # 之前几轮的净收益,只用过去
    for a, b in segs:
        r = follow_from(book, a, b, k)
        pos_k = abs(net[a + k]) if a + k <= b and np.isfinite(net[a + k]) else np.nan
        rows.append({
            "code": code,
            "date": book.index[a],
            "ret": r,
            "speed": pos_k / max(k, 1) / typical,
            "size": pos_k / typical,
            # **严格只用过去**:这一轮的结果不能进它自己的判据
            "prior_win": (np.mean([x > 0 for x in prior[-10:]])
                          if len(prior) >= 5 else np.nan),
            "legs": legs[a + k] if a + k <= b else np.nan,
            "vol": sigma[a + k] if a + k <= b else np.nan,
        })
        if r is not None and np.isfinite(r):
            prior.append(r)
    return pd.DataFrame(rows)


def terciles(df: pd.DataFrame, col: str) -> pd.Series | None:
    d = df[[col, "ret"]].dropna()
    if len(d) < 60:
        return None
    try:
        q = pd.qcut(d[col], 3, labels=False, duplicates="drop")
    except ValueError:
        return None
    if pd.Series(q).nunique() < 3:
        return None
    return d.groupby(q, observed=True)["ret"].agg(["mean", "count"])


def main() -> None:
    print(__doc__.split("\n")[0])

    # ---- 等几天再跟? ----
    print(f"\n{'=' * 92}")
    print("① 观察几天再跟?(等待换信息,代价是更差的价格)")
    print("=" * 92)
    print(f"    {'等待':>6s}" + "".join(f"{c:>10s}" for c in CODES) + f"{'合并':>10s}{'轮次':>8s}")
    best = {}
    for k in WAITS:
        cells, pool = "", []
        for c in CODES:
            df = rounds_of(c, k)
            v = df["ret"].dropna()
            cells += f"{v.mean():>+9.2f}%" if len(v) else "        ——"
            pool.extend(v.tolist())
            best[(c, k)] = df
        cells += f"{np.mean(pool):>+9.2f}%{len(pool):>8d}" if pool else ""
        print(f"    {k:>4} 天{cells}")

    # ---- 判据 ----
    for k in (0, 3):
        allrounds = pd.concat([best[(c, k)] for c in CODES], ignore_index=True)
        allrounds = allrounds.dropna(subset=["ret"])
        print(f"\n{'=' * 92}")
        print(f"② 判据(等 {k} 天入场,跨品种合并 {len(allrounds)} 轮):"
              f"低/中/高三档的净收益")
        print("=" * 92)
        base = allrounds["ret"].mean()
        print(f"    基准(全跟) {base:+.2f}%   胜率 {(allrounds['ret'] > 0).mean():.0%}")
        for col, name in (("speed", "建仓速度"), ("size", "建仓规模"),
                          ("prior_win", "历史胜率"), ("legs", "在榜家数"),
                          ("vol", "当时波动")):
            g = terciles(allrounds, col)
            if g is None:
                print(f"    {name}: 样本不足或分不出档")
                continue
            cells = "  ".join(f"{v['mean']:+6.2f}%" for _, v in g.iterrows())
            hi_lo = g["mean"].iloc[-1] - g["mean"].iloc[0]
            print(f"    {name}: {cells}   高−低 {hi_lo:+.2f}%   每档~{int(g['count'].mean())}")

    # ---- 样本外 ----
    k = 3
    allrounds = pd.concat([best[(c, k)] for c in CODES], ignore_index=True)
    allrounds = allrounds.dropna(subset=["ret"]).sort_values("date")
    cut = allrounds["date"].quantile(0.5)
    print(f"\n{'=' * 92}")
    print(f"③ 样本外(等 {k} 天,按时间中位切 {cut.date()})")
    print("=" * 92)
    for col, name in (("speed", "建仓速度"), ("size", "建仓规模"),
                      ("prior_win", "历史胜率"), ("legs", "在榜家数"),
                      ("vol", "当时波动")):
        parts = []
        for label, sub in (("样本内", allrounds[allrounds["date"] <= cut]),
                           ("样本外", allrounds[allrounds["date"] > cut])):
            g = terciles(sub, col)
            parts.append(f"{label} {g['mean'].iloc[-1] - g['mean'].iloc[0]:+.2f}%"
                         if g is not None else f"{label} 不足")
        print(f"    {name}: " + "   |   ".join(parts))

    print(f"\n{'=' * 92}")
    print("判定:高−低要有量级、跨品种符号一致、样本外不翻号。")
    print("轮次数少(合并四百出头),三档每档一百出头 —— 这一步只筛方向,不下定论。")


if __name__ == "__main__":
    main()
