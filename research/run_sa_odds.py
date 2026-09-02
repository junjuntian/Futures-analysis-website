# -*- coding: utf-8 -*-
"""纯碱 SA2701:09-02 冲高回落 + 夜盘收复,之后偏涨还是偏跌(2026-09-03 运营者直问)。

运营者:「sa2701 后续涨还是跌,2 号收盘是 1056,2 号夜盘又拉上去了,这是什么情况。」

**不猜,做条件基准率。** 把当下的状态拆成**可量化、可回溯**的条件,每个条件
单独统计「之后 5/10/20 日涨的比例」,并与**全样本基准**对照。

条件**先写死再跑**(PITFALLS 第 5 条):

  A **冲高回落 + 减仓**:(最高−收盘)/收盘 ≥ 1.5%,且 收盘 < 开盘,且持仓量下降。
    —— 09-02 正是:开 1073 / 高 1083 / 低 1053 / 收 1056,持仓 −7,367。
  B **机构净空但已大幅回补**:五家合计净空,且较近 30 日峰值已回补 ≥ 50%。
    —— 09-02 正是:净空 −165,391,峰值 −356,252,已卸掉 54%。
  C = A 且 B(今天同时满足)。
  D **A 之后次日高开或收复**:A 成立日的次日结算 ≥ 当日结算。
    —— 夜盘把 1056 拉回 1068(vs 09-02 结算 1067),属于这一型。

另报 A/B 的宽松档与严格档 —— **一个门槛出来的数不算数**。

**这回答的是基准率,不是预测。** 条件相同 ≠ 结局相同。

**基差有意不入判据**:我们库里的现货(生意社 1087)与运营者行情软件的现货(998)
差 89 元,**连升贴水的方向都相反**。口径没对齐之前,拿它做判据是在拿一个自己
都不确定的数当证据。这件事单独报,不混进统计。

跑法:python research/run_sa_odds.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

WINDOWS = (5, 10, 20)
INST5 = ["国泰君安", "海通期货", "东证期货", "华泰期货", "永安期货"]
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "sa_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "sa_seat.csv.gz"))
H.use("SA")
mkt = H.main_series(price)
idx = mkt.index
n = len(idx)
# 收益用逐日 ret 连乘(main_series 已处理换月),不拿 settle 直除
r = mkt["ret"].fillna(0.0).values

# 主力合约的当日 OHLC 与持仓量:逐日取主力那一行
main = mkt["main"]
by = price.set_index(["contract", "trade_date"])


def pick(col):
    out = []
    for d, c in zip(idx, main):
        try:
            out.append(float(by.loc[(c, d), col]))
        except (KeyError, TypeError, ValueError):
            out.append(np.nan)
    return pd.Series(out, index=idx)


op_, hi_, cl_, oi_ = pick("open_price"), pick("high_price"), pick("px"), pick("open_interest")
settle = mkt["settle"]


def fwd(k, w):
    a, e = k + 1, min(k + 1 + w, n)
    return (float(np.prod(1 + r[a:e])) - 1) * 100 if a < n else np.nan


F = np.array([[fwd(k, w) for w in WINDOWS] for k in range(n)], dtype=float)

# ---- 五家机构合计净持仓,及「较近 30 日峰值回补了多少」
inst = (seat[seat["member_key"].isin(INST5)]
        .groupby("trade_date")["net"].sum().reindex(idx).ffill())
peak = inst.rolling(30, min_periods=10).min()          # 最负 = 净空峰值
unload = (inst - peak) / peak.abs()                     # 回补比例(峰值为负时)

L = [f"纯碱 SA:条件基准率(样本 {idx[0].date()} ~ {idx[-1].date()},{n} 个交易日)", ""]
L.append("**这是基准率,不是预测。** 条件相同 ≠ 结局相同;它只说明「历史上处在类似")
L.append("状态时偏哪边」。基差有意不入判据 —— 两个现货口径连方向都相反,见文末。")
L.append("")
base_up = [np.nanmean(F[:, i] > 0) * 100 for i in range(3)]
base_mean = [np.nanmean(F[:, i]) for i in range(3)]
L.append("**全样本基准**:" + "  ".join(
    f"{w}日 涨 {base_up[i]:.0f}% · 均值 {base_mean[i]:+.2f}%" for i, w in enumerate(WINDOWS)))
L.append("")


def report(label, mask, note=""):
    ks = np.where(np.asarray(mask, dtype=bool))[0]
    ks = ks[ks < n - 1]
    if len(ks) < 5:
        L.append(f"{label}:样本仅 {len(ks)} 次,太少不报")
        L.append("")
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
        f"{idx[k].strftime('%y-%m-%d')}({F[k, 2]:+.1f}%)" for k in ks[-5:]))
    L.append("")


# A 冲高回落 + 减仓
spike = (hi_ - cl_) / cl_
for gate in (0.015, 0.010, 0.020):
    m = (spike >= gate) & (cl_ < op_) & (oi_.diff() < 0)
    tag = "(**09-02 这一档**)" if gate == 0.015 else ""
    report(f"A 冲高回落≥{gate*100:.1f}% + 收<开 + 减仓{tag}", m.fillna(False).values)

# B 机构净空但已回补
for gate in (0.50, 0.40, 0.60):
    m = (inst < 0) & (unload >= gate)
    tag = "(**09-02 这一档:已卸 54%**)" if gate == 0.50 else ""
    report(f"B 机构净空 且 较 30 日峰值已回补≥{gate*100:.0f}%{tag}", m.fillna(False).values)

# C 两者同时
mA = ((spike >= 0.015) & (cl_ < op_) & (oi_.diff() < 0)).fillna(False)
mB = ((inst < 0) & (unload >= 0.50)).fillna(False)
report("C = A 且 B(**09-02 同时满足**)", (mA & mB).values)

# D A 之后次日收复
d_settle = settle.diff().shift(-1)      # 次日结算 − 当日结算
mD = mA & (d_settle >= 0)
report("D A 型日 且 **次日结算收复**(夜盘拉回属这一型)", mD.fillna(False).values)
mD2 = mA & (d_settle < 0)
report("D' 对照组:A 型日但次日继续跌", mD2.fillna(False).values)

L.append("## 今天的读数(2026-09-02 收盘)")
L.append("")
k = n - 1
L.append(f"  主力 {main.iloc[-1]} · 开 {op_.iloc[-1]:.0f} 高 {hi_.iloc[-1]:.0f} "
         f"收 {cl_.iloc[-1]:.0f} 结算 {settle.iloc[-1]:.0f}")
L.append(f"  冲高回落幅度 {spike.iloc[-1]*100:.2f}% · 持仓量变化 {oi_.diff().iloc[-1]:+,.0f}")
L.append(f"  五家机构净持仓 {inst.iloc[-1]:+,.0f} 手 · 30 日峰值 {peak.iloc[-1]:+,.0f} "
         f"· 已回补 {unload.iloc[-1]*100:.0f}%")
L.append("")
L.append("## 怎么用")
L.append("")
L.append("看「涨的比例」与基准差多少。差 3~5 个百分点是噪音;差 15 个百分点以上、")
L.append("且换门槛还在同一边,才值得当回事。样本少于 15 次的一律别当结论。")
io.open(OUT / "sa_odds.txt", "w", encoding="utf-8").write("\n".join(L))
print("done ->", OUT / "sa_odds.txt")
