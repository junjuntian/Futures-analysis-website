"""生猪「卸仓反弹做多」信号对**跨月套利**有没有用?(运营者 2026-08-23)

向上套利 = 牛市价差:买近卖远,赌近月相对远月走强。
检验:信号触发后,同一对合约(信号日主力 vs 月份更远的次活跃合约)的价差
(近 − 远,元/吨)在 5/10/20/40 日里怎么变;对照无条件、以及「机构净空但卸仓<50%」
(还在持空)的日子。三组事件:
  E1 卸仓首次越过 50%(与 REPORT_LH_LONG_v1 同一口径);
  E2 做多腿实际进场的那些信号日(引擎路径,DEC-118 配置);
  E3 bounce_long 为真的全部日子(状态而非事件)。
判据写死:要说「有用」,20 日价差变化均值 > 0 且 t > 2,并且胜率 > 无条件 10 个点以上。
三年样本,事件个位数到十几个 —— 先看方向,别把 t 当真理。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402

HS = (5, 10, 20, 40)


def tstat(a):
    a = np.array(a, dtype=float)
    a = a[np.isfinite(a)]
    return a.mean() / a.std(ddof=1) * np.sqrt(len(a)) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan


sig, mkt, rdf, op, st, groups, unload = R.load("LH")
v = H.use("LH")
H.CURRENT = {"code": "LH", **v}
# DEC-127 复验:席位组与门槛都按引擎现行配置(固定名单 + long_unload_min),不再写死 0.5 / 滚动组。
import sys as _sys
if len(_sys.argv) > 1:
    H.RULES["long_unload_min"] = float(_sys.argv[1])
THR = H.RULES["long_unload_min"]
if H.RULES.get("fixed_members"):
    price0 = H.clean_price(pd.read_csv(R.DATA / "lh_price.csv.gz")); seat0 = H.clean_seat(pd.read_csv(R.DATA / "lh_seat.csv.gz"))
    groups, _, _ = H.fixed_groups(H.RULES["fixed_members"], seat0, price0, mkt.index, "2026-08-23")
    sig = H.signal_series(seat0, groups)
print(f"[配置] 席位组 {'固定 '+'、'.join(groups.iloc[-1]) if H.RULES.get('fixed_members') else '滚动'} | 卸仓门槛 {THR:.0%}")
price = H.clean_price(pd.read_csv(R.DATA / "lh_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(R.DATA / "lh_seat.csv.gz"))
idx = mkt.index
stx = st.reindex(idx)                        # 各合约逐日结算价
oi = price.pivot_table(index="trade_date", columns="contract", values="open_interest", aggfunc="first").reindex(idx)


def ym(c):
    return int(str(c)[2:])


def far_of(d, near):
    """月份晚于 near、当日持仓量最大的合约。"""
    row = oi.loc[d].dropna()
    cands = [c for c in row.index if ym(c) > ym(near) and np.isfinite(stx.at[d, c] if c in stx.columns else np.nan)]
    if not cands:
        return None
    return max(cands, key=lambda c: row[c])


def spread_fwd(i, h):
    """第 i 日定下的(近,远)对,h 日后价差变化(元/吨);同对合约,不换腿。"""
    if i + h >= len(idx):
        return np.nan
    d0, d1 = idx[i], idx[i + h]
    near = mkt["main"].iloc[i]
    far = far_of(d0, near)
    if far is None or near not in stx.columns:
        return np.nan
    s0 = stx.at[d0, near] - stx.at[d0, far]
    s1 = stx.at[d1, near] - stx.at[d1, far]
    return float(s1 - s0) if np.isfinite(s0) and np.isfinite(s1) else np.nan


def outright_fwd(i, h):
    r = mkt["ret"].fillna(0)
    if i + h >= len(idx):
        return np.nan
    return float((1 + r.iloc[i + 1:i + 1 + h]).prod() - 1) * 100


# —— 事件 ——
sig_b = H.attach_bounce_long(sig, seat, mkt, groups)
side = sig_b["bounce_side"].reindex(idx)
unl = sig_b["bounce_unload"].reindex(idx)
flag = sig_b["bounce_long"].reindex(idx).fillna(False).astype(bool)
E1, armed = [], True
for i in range(len(idx)):
    s_, u_ = side.iloc[i], unl.iloc[i]
    if not (np.isfinite(s_) and s_ < 0):
        armed = True
        continue
    if np.isfinite(u_) and u_ >= THR and armed:
        E1.append(i)
        armed = False
tr, _, _ = H.replay(sig_b, mkt, rdf, op, st)
pos = {d: i for i, d in enumerate(idx)}
E2 = [pos[pd.Timestamp(t["entry_date"])] for t in tr if t["side"] == "long" and pd.Timestamp(t["entry_date"]) in pos]
E3 = [i for i in range(len(idx)) if flag.iloc[i]]
HOLD = [i for i in range(len(idx)) if np.isfinite(side.iloc[i]) and side.iloc[i] < 0
        and np.isfinite(unl.iloc[i]) and unl.iloc[i] < THR]
ALL = list(range(len(idx)))

print("=" * 100)
print("生猪 跨月价差(近−远,元/吨)在信号之后的变化;括号里是 t,[ ] 里是价差上涨的比例")
print("=" * 100)


def line(tag, ev):
    parts = []
    for h in HS:
        x = [spread_fwd(i, h) for i in ev]
        x = [a for a in x if np.isfinite(a)]
        hit = np.mean([a > 0 for a in x]) * 100 if x else np.nan
        parts.append(f"{h}日 {np.mean(x) if x else float('nan'):>+6.1f}(t{tstat(x):+.1f})[{hit:.0f}%]")
    print(f"  {tag:<26}{len(ev):>5} | " + "  ".join(parts))


line("无条件(全部交易日)", ALL)
line("机构净空且卸仓<50%(持空中)", HOLD)
line("E1 卸仓首越 50%", E1)
line("E2 做多腿实际进场日", E2)
line("E3 bounce_long 为真的天", E3)
print()
print("参考:同一事件之后**主力单边**的涨跌(%):")


def line2(tag, ev):
    parts = []
    for h in HS:
        x = [outright_fwd(i, h) for i in ev]
        x = [a for a in x if np.isfinite(a)]
        parts.append(f"{h}日 {np.mean(x) if x else float('nan'):>+5.2f}(t{tstat(x):+.1f})")
    print(f"  {tag:<26}{len(ev):>5} | " + "  ".join(parts))


line2("无条件", ALL)
line2("E1 卸仓首越 50%", E1)
line2("E2 做多腿实际进场日", E2)
print()
# 价差与单边的「安全性」对比:20 日变化的波动
sp20 = np.array([spread_fwd(i, 20) for i in ALL], dtype=float)
sp20 = sp20[np.isfinite(sp20)]
px = mkt["settle"].reindex(idx).mean()
print(f"20 日价差变化 标准差 {np.std(sp20):.0f} 元/吨(≈ 主力均价的 {np.std(sp20)/px*100:.1f}%);"
      f"20 日单边 标准差 {np.std([a for a in (outright_fwd(i,20) for i in ALL) if np.isfinite(a)]):.1f}%")
print()
print("E2 做多腿逐笔:进场日 → 20 日价差变化 / 主力涨跌")
for i in E2:
    print(f"  {idx[i].date()}  近 {mkt['main'].iloc[i]} 远 {far_of(idx[i], mkt['main'].iloc[i])}  "
          f"价差 20 日 {spread_fwd(i, 20):+.0f} 元/吨   单边 20 日 {outright_fwd(i, 20):+.2f}%")
