# -*- coding: utf-8 -*-
"""玻璃:处在今天这种状态时,历史上涨的多还是跌的多(2026-09-01 运营者直问)。

运营者:「那到底是继续涨的概率大,还是继续跌的概率大?」

**不猜,做条件基准率。** 把今天的状态拆成几个**可量化、可回溯**的条件,
每个条件单独统计「之后 5/10/20 日涨的比例」,并与**全样本基准**对照。

条件**先写死再跑**(不跑完再挑,PITFALLS 第 5 条):
  A 基差由贴水转升水(dominant_basis 由负转正)—— 今天正好发生;
  B 轧空特征:5 日涨幅 ≥ 5% 且 5 日持仓量降幅 ≥ 10%;
  C = A 且 B(今天同时满足)。
另报 A/B 的宽松版(不同门槛)看结论稳不稳 —— **一个门槛出来的数不算数**。

**这回答的是「基准率」,不是预测。** 样本里每一次的宏观背景都不同,
条件相同不等于结局相同;基准率只说明「在类似状态下历史上偏哪一边、偏多少」。

跑法:python research/run_fg_odds.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

WINDOWS = (5, 10, 20)
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "fg_price.csv.gz"))
H.use("FG")
mkt = H.main_series(price)
basis = pd.read_csv(D / "fg_basis.csv", parse_dates=["trade_date"]).set_index("trade_date")
idx = mkt.index.intersection(basis.index)
mkt = mkt.reindex(idx)
basis = basis.reindex(idx)
n = len(idx)
# 收益用逐日 ret 连乘(已处理换月),不拿 settle 直除
r = mkt["ret"].fillna(0.0).values
# 主力持仓量:逐日取主力合约那一行
oi = pd.Series(
    [price[(price["contract"] == c) & (price["trade_date"] == d)]["open_interest"].max()
     if isinstance(c, str) else np.nan
     for d, c in zip(idx, mkt["main"])], index=idx, dtype=float)
b = basis["dominant_basis"].astype(float)


def fwd(k, w):
    a, e = k + 1, min(k + 1 + w, n)
    return (float(np.prod(1 + r[a:e])) - 1) * 100 if a < n else np.nan


F = np.array([[fwd(k, w) for w in WINDOWS] for k in range(n)], dtype=float)
ret5 = pd.Series(r, index=idx).rolling(5).apply(lambda x: np.prod(1 + x) - 1) * 100
oi5 = oi / oi.shift(5) - 1

L = [f"玻璃:条件基准率(样本 {idx[0].date()} ~ {idx[-1].date()},{n} 个交易日)", ""]
L.append("**这是基准率,不是预测。** 条件相同 ≠ 结局相同;它只说明「历史上处在类似状态时偏哪边」。")
L.append("")
base_up = [np.nanmean(F[:, i] > 0) * 100 for i in range(3)]
base_mean = [np.nanmean(F[:, i]) for i in range(3)]
L.append("**全样本基准**:" + "  ".join(
    f"{w}日 涨 {base_up[i]:.0f}% · 均值 {base_mean[i]:+.2f}%" for i, w in enumerate(WINDOWS)))
L.append("")


def report(label, mask, note=""):
    ks = np.where(mask)[0]
    ks = ks[ks < n]
    if len(ks) < 5:
        L.append(f"{label}:样本仅 {len(ks)} 次,太少不报")
        return
    a = F[ks]
    L.append(f"### {label} —— {len(ks)} 次{note}")
    for i, w in enumerate(WINDOWS):
        col = a[:, i]
        up = np.nanmean(col > 0) * 100
        L.append(f"  {w:>2} 日:**涨 {up:.0f}%**(基准 {base_up[i]:.0f}%)"
                 f" · 均值 {np.nanmean(col):+.2f}%(基准 {base_mean[i]:+.2f}%)"
                 f" · 中位 {np.nanmedian(col):+.2f}%")
    L.append("  最近 5 次:" + "、".join(
        f"{idx[k].strftime('%y-%m-%d')}({F[k,2]:+.1f}%)" for k in ks[-5:]))
    L.append("")


# A 基差由贴水转升水
flip_up = (b > 0) & (b.shift(1) <= 0)
report("A 基差由贴水转升水(**今天发生**)", flip_up.values)
report("A' 基差处在升水(不限当天转)", (b > 0).values)

# B 轧空特征
for up_gate, oi_gate in ((5, -10), (4, -8), (6, -12)):
    m = (ret5 >= up_gate) & (oi5 <= oi_gate / 100)
    tag = "(**今天这一档**)" if (up_gate, oi_gate) == (5, -10) else ""
    report(f"B 五日涨≥{up_gate}% 且 持仓量降≥{abs(oi_gate)}%{tag}", m.fillna(False).values)

# C 两者同时
m = flip_up & (ret5 >= 5) & (oi5 <= -0.10)
report("C = A 且 B(**今天同时满足**)", m.fillna(False).values)

L.append("## 今天的读数")
k = n - 1
L.append(f"  基差 {b.iloc[-1]:+.0f}(昨日 {b.iloc[-2]:+.0f}) · 5 日涨幅 {ret5.iloc[-1]:+.1f}%"
         f" · 5 日持仓量变化 {oi5.iloc[-1]*100:+.1f}%")
L.append("")
L.append("**怎么用**:看「涨的比例」与基准差多少。差 3~5 个百分点是噪音;")
L.append("差 15 个百分点以上、且换门槛还在同一边,才值得当回事。样本少于 15 次的一律别当结论。")
io.open(OUT / "fg_odds.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
