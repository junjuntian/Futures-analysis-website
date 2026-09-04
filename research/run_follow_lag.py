# -*- coding: utf-8 -*-
"""订正 T+1 的真实代价。

时序(郑商所):
  · 交易日 d 的龙虎榜约 16:26 公布(日盘 15:00 收盘之后);
  · 下一个可交易时刻 = 当晚 21:00 夜盘 = **交易日 d+1 的开盘**(PITFALLS #8);
  · 所以看到 d 日持仓的人,在 d+1 开盘就能进,只差
    「d 日结算价 → d+1 开盘价」这一个跳空,不是整整一天。

昨天的拆解用了 shift(2)(晚两天),高估了 T+1 的代价。这里三条并排:
  甲 席位自己 : net_d × (settle_{d+1} − settle_d)
  乙 正确跟随 : net_d × (open_{d+2} − open_{d+1})     ← 回测用的就是这条
  丙 错误口径 : net_{d-1} × (settle_{d+1} − settle_d) ← 昨天算的
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
MEM = ["永安期货", "东证期货"]
MULT = 20.0

PX, ST, MK, OP = {}, {}, {}, {}
for c, stem in (("FG", "fg"), ("SA", "sa")):
    H.use(c)
    PX[c] = H.clean_price(pd.read_csv(D / f"{stem}_price.csv.gz"))
    ST[c] = H.clean_seat(pd.read_csv(D / f"{stem}_seat.csv.gz"))
    MK[c] = H.main_series(PX[c])
    OP[c], _ = H.contract_prices(PX[c])
IDX = MK["FG"].index.intersection(MK["SA"].index)
IDX = IDX[IDX >= pd.Timestamp("2020-06-01")]


def series(member):
    """返回三条 P&L(亿元)与对冲态掩码。全部按品种净持仓 × 主力合约。"""
    a = pd.Series(0.0, index=IDX)   # 甲 席位自己
    b = pd.Series(0.0, index=IDX)   # 乙 正确跟随
    c = pd.Series(0.0, index=IDX)   # 丙 错误口径(多滞后一天)
    nets = {}
    for k in ("FG", "SA"):
        s = ST[k][ST[k].member_key == member]
        vnet = s.groupby("trade_date")["net_off"].sum().reindex(IDX)
        nets[k] = vnet
        main = MK[k]["main"].reindex(IDX)
        st = PX[k].pivot_table(index="trade_date", columns="contract",
                               values="settle", aggfunc="first").reindex(IDX)
        op = OP[k].reindex(IDX)

        def pick(tab, shift_days=0):
            out = []
            for i, d in enumerate(IDX):
                j = i + shift_days
                mc = main.iloc[i] if i < len(main) else None
                v = np.nan
                if isinstance(mc, str) and mc in tab.columns and 0 <= j < len(IDX):
                    v = tab[mc].iloc[j]
                out.append(float(v) if np.isfinite(v) else np.nan)
            return pd.Series(out, index=IDX)

        d_settle = pick(st, 0) - pick(st, -1)         # settle_d − settle_{d-1}
        # 乙:从 d 日主力的 open_{d+1} 持到 open_{d+2}
        d_open = pick(op, 2) - pick(op, 1)
        a = a.add(vnet.shift() * d_settle, fill_value=0.0)
        b = b.add(vnet * d_open, fill_value=0.0)
        c = c.add(vnet.shift(2) * d_settle, fill_value=0.0)
    hedge = (nets["FG"] * nets["SA"] < 0).reindex(IDX).fillna(False)
    same = (nets["FG"] * nets["SA"] > 0).reindex(IDX).fillna(False)
    return a * MULT, b * MULT, c * MULT, hedge, same


print(f"样本 {IDX[0].date()} ~ {IDX[-1].date()}  单位:亿元\n")
print(f"{'':<10}{'甲 席位自己':>14}{'乙 正确跟随':>14}{'丙 昨天算错的':>16}"
      f"{'乙/甲':>9}{'丙/甲':>9}")
print("-" * 74)
for m in MEM:
    a, b, c, hedge, same = series(m)
    for tag, msk in (("全部日", pd.Series(True, index=IDX)),
                     ("对冲态", hedge), ("同向日", same)):
        A, B, C = a[msk].sum() / 1e8, b[msk].sum() / 1e8, c[msk].sum() / 1e8
        r1 = f"{B/A*100:.0f}%" if A else "—"
        r2 = f"{C/A*100:.0f}%" if A else "—"
        print(f"{m[:2]+' '+tag:<10}{A:>13.2f}{B:>13.2f}{C:>15.2f}{r1:>9}{r2:>9}")
    print()
