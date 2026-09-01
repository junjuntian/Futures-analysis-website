# -*- coding: utf-8 -*-
"""玻璃:永安减多 + 东证加多 同时发生时,之后怎么走(2026-09-01 运营者)。

运营者:「那这次永安减多、东证加多,到底说明了什么?」

**与其讲故事,不如去数。** 这两家在玻璃上都是十几年的大席位,「一个减一个加」
历史上出现过很多次 —— 那就把这些日子挑出来,看后面 5/10/20 日价格是涨是跌,
以及**谁那一边是对的**。

事件定义(**先写死,不跑完再调**):
  · A 组「永安减 + 东证加」:当日 Δ永安 < 0 且 Δ东证 > 0,且两者变动幅度都 ≥ 门槛;
  · B 组「永安加 + 东证减」:反过来;
  · 门槛取 5,000 与 10,000 两档并列报 —— **一个阈值出来的结论不算数**。
基准:全样本同期的 5/10/20 日收益(不能只看 A 组是正是负,要跟「随便哪天」比)。

口径:净持仓 = 该会员当日全部合约的 net_off 合计;收益按主力结算价、T+1 起算
(与全站回测同一条纪律,换月由 main_series 处理)。

**这不能回答的**:两家的动机、它们客户是谁。席位含代客,不是自营。

跑法:python research/run_fg_divergence.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

A_NAME, B_NAME = "永安期货", "东证期货"
GATES = (5000, 10000)
WINDOWS = (5, 10, 20)
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "fg_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "fg_seat.csv.gz"))
H.use("FG")
mkt = H.main_series(price)
idx = mkt.index[mkt.index >= seat["trade_date"].min()]
n = len(idx)
# 收益用逐日 ret 连乘(已处理换月),不拿 settle 直除 —— 换月那天会凭空跳几个点
r = mkt["ret"].reindex(idx).fillna(0.0).values


def fwd(k, w):
    """从 k+1 开盘起、往后 w 个交易日的累计收益(%)。"""
    a, b = k + 1, min(k + 1 + w, n)
    if a >= n:
        return np.nan
    return (float(np.prod(1 + r[a:b])) - 1) * 100


net = {}
for m in (A_NAME, B_NAME):
    s = seat[seat["member_key"] == m].groupby("trade_date")["net_off"].sum()
    net[m] = s.reindex(idx).ffill()
dA = net[A_NAME].diff()
dB = net[B_NAME].diff()

base = np.array([[fwd(k, w) for w in WINDOWS] for k in range(n)], dtype=float)
L = [f"玻璃:永安与东证反向操作之后怎么走(样本 {idx[0].date()} ~ {idx[-1].date()},{n} 个交易日)", ""]
L.append("**基准(随便哪天)**:" + "  ".join(
    f"{w}日 均值 {np.nanmean(base[:, i]):+.2f}% · 上涨 {np.nanmean(base[:, i] > 0)*100:.0f}%"
    for i, w in enumerate(WINDOWS)))
L.append("")

for gate in GATES:
    L.append(f"## 门槛 {gate:,} 手")
    L.append("")
    groups = {
        f"A 永安减 + 东证加(**本次就是这一型**)": (dA < -gate) & (dB > gate),
        f"B 永安加 + 东证减": (dA > gate) & (dB < -gate),
    }
    for label, mask in groups.items():
        ks = [k for k in range(n) if bool(mask.iloc[k])]
        if not ks:
            L.append(f"{label}:样本内 0 次")
            continue
        arr = np.array([[fwd(k, w) for w in WINDOWS] for k in ks], dtype=float)
        L.append(f"{label} —— {len(ks)} 次")
        for i, w in enumerate(WINDOWS):
            col = arr[:, i]
            up = np.nanmean(col > 0) * 100
            b_up = np.nanmean(base[:, i] > 0) * 100
            L.append(f"  {w:>2} 日:均值 {np.nanmean(col):+.2f}%(基准 {np.nanmean(base[:, i]):+.2f}%)"
                     f" · 上涨 {up:.0f}%(基准 {b_up:.0f}%)"
                     f" · 中位 {np.nanmedian(col):+.2f}%")
        L.append("  最近 6 次:" + "、".join(
            f"{idx[k].strftime('%y-%m-%d')}({fwd(k, 20):+.1f}%)" for k in ks[-6:]))
        L.append("")

# 本次事件本身
k = n - 1
L.append("## 本次(2026-09-01)")
L.append(f"  Δ永安 {int(dA.iloc[-1]):+,} 手 · Δ东证 {int(dB.iloc[-1]):+,} 手"
         f" · 永安净 {int(net[A_NAME].iloc[-1]):+,} · 东证净 {int(net[B_NAME].iloc[-1]):+,}")
L.append("")
L.append("**怎么读**:A 组的均值/上涨率要和基准比 —— 玻璃本身有漂移,只看 A 组是正是负")
L.append("会把品种的漂移当成信号。差得不明显,就是这个组合没有预测力。")
io.open(OUT / "fg_divergence.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
