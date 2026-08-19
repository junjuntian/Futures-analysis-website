"""生猪:现有引擎信号 vs 共振信号,是互补还是重复。

在同一时间轴(2024-01 起)上比,并测三种组合方式:
  ① 各自单独   ② 二选一先到先得   ③ 两者都触发才进(与运算)
决定共振该以什么形态加进生产引擎。
"""
from __future__ import annotations
import numpy as np, pandas as pd
import lhlib as L
from run_flow_skill import seat_alpha
from run_lh_phase2 import main_returns
import run_resonance_backtest as R

CODE, CUT = "LH", pd.Timestamp("2024-01-01")
price = L.load_price(CODE); seat = L.load_seat(CODE)
df = seat.merge(price[["contract", "trade_date", "settle"]], on=["contract", "trade_date"], how="inner")
mr = main_returns(price); mr = mr[mr.index >= CUT]

# —— A. 现有生产引擎的信号:滚动重选前 5、只做空、z<=-1 进场 ——
# 与 engine/hog_money.py 同口径(每年重选、5 日变化、120 日标准化)
def prod_signal():
    cuts = pd.date_range(price["trade_date"].min() + pd.Timedelta(days=250),
                         mr.index.max(), freq="12MS")
    picks, cur = {}, None
    for c in cuts:
        a = seat_alpha(df[df["trade_date"] < c], CODE, min_days=120)
        if not a.empty and len(a) >= 5:
            cur = tuple(a.sort_values("alpha", ascending=False).head(5).index)
        picks[c] = cur
    out = pd.Series(index=mr.index, dtype=float)
    for d in mr.index:
        v = [c for c in cuts if c <= d]
        g = picks[v[-1]] if v else None
        if not g: continue
        s = df[df["member_key"].isin(list(g))].groupby("trade_date")["net"].sum().sort_index()
        out[d] = s.diff(5).get(d, np.nan)
    z = out / out.rolling(120, min_periods=60).std()
    return z

pz = prod_signal()
sig, mr2, smart5, have = R.prepare(CODE)
rz = sig["rz"]; res = sig["resonate"]

print(f"现有引擎信号(滚动重选、只做空)  vs  共振信号(散户反向×聪明钱,双向)")
print(f"两者相关系数: {pz.corr(rz):+.3f}")
both_fire = ((pz <= -1) & (rz.abs() >= 1) & res)
print(f"同日都触发的天数: {int(both_fire.sum())} / {int(((pz<=-1) | ((rz.abs()>=1)&res)).sum())} "
      f"(任一触发的天数)")

def bt(entry_mask, side_series, hold=20, stop=0.06, cost=0.0005):
    """entry_mask=可进场日;side_series=方向。持仓期间不重叠。"""
    idx = mr.index; trades=[]; side=0; ei=None; cum=0.0
    for i, d in enumerate(idx):
        r = mr["ret"].get(d, np.nan)
        if side != 0: cum = (1+cum)*(1+side*(r if np.isfinite(r) else 0))-1
        reason=None
        if side != 0:
            zz = rz.get(d, np.nan)
            if cum <= -stop: reason="止损"
            elif i-ei >= hold: reason="持满"
            elif np.isfinite(zz) and side*zz <= -1: reason="反向"
        if reason:
            trades.append({"进场":idx[ei],"出场":d,"方向":"多" if side>0 else "空",
                           "收益%":(cum-2*cost)*100,"持有":i-ei})
            side, cum = 0, 0.0
        if side == 0 and bool(entry_mask.get(d, False)):
            s = side_series.get(d, np.nan)
            if np.isfinite(s) and s != 0: side, ei, cum = int(np.sign(s)), i, 0.0
    return pd.DataFrame(trades)

def show(tr, label):
    if tr.empty: print(f"  {label:24s} 无交易"); return
    o = R.stats(tr, mr, "", quiet=True)
    n_long = (tr["方向"]=="多").sum()
    print(f"  {label:24s} {o['笔数']:3d}笔(多{n_long}/空{o['笔数']-n_long}) "
          f"累计{o['累计%']:+7.1f}% 胜率{o['胜率%']:5.1f}% 回撤{o['回撤%']:6.1f}% "
          f"夏普{o['夏普']:5.2f} 在场{o['在场%']:4.0f}%")

print("\n三种组合:")
show(bt(pz <= -1, pd.Series(-1.0, index=mr.index)), "① 只用现有引擎(只做空)")
show(bt((rz.abs() >= 1) & res, np.sign(rz)), "① 只用共振(双向)")
show(bt((pz <= -1) | ((rz.abs() >= 1) & res),
        pd.Series(np.where(pz <= -1, -1.0, np.sign(rz)), index=mr.index)), "② 二选一(先到先得)")
show(bt(both_fire, pd.Series(-1.0, index=mr.index)), "③ 都触发才进(与)")

print("\n共振信号的多空拆分(它是双向的,现有引擎只做空):")
tr = bt((rz.abs() >= 1) & res, np.sign(rz))
for s in ("空", "多"):
    sub = tr[tr["方向"] == s]
    if len(sub): print(f"  {s}: {len(sub):2d}笔 累计{((1+sub['收益%']/100).prod()-1)*100:+7.1f}% "
                       f"均值{sub['收益%'].mean():+5.2f}% 胜率{(sub['收益%']>0).mean()*100:5.1f}%")

# ---------------------------------------------------------------- ④
# 多头那 20 笔是净拖累(-3.5%),与 DEC-084 关掉生猪做多支路同一个结论。
# 所以候选形态都限定只做空,重新比一遍。
print("\n④ 只做空的几种形态(生猪引擎已按 DEC-084 只做空,这里对齐口径)")
short_only = lambda mask: bt(mask, pd.Series(-1.0, index=mr.index))
cands = {
    "现有引擎(生产在跑)": pz <= -1,
    "共振(只做空)": (rz <= -1) & res,
    "散户反向单独(只做空)": rz <= -1,
    "两者都触发(与)": (pz <= -1) & (rz <= -1) & res,
    "任一触发(或)": (pz <= -1) | ((rz <= -1) & res),
}
for label, mask in cands.items():
    show(short_only(mask), label)

print("\n⑤ 逐年(只做空口径)")
for label, mask in cands.items():
    tr = short_only(mask)
    if tr.empty: continue
    tr["年"] = tr["出场"].dt.year
    parts = []
    for y, g in tr.groupby("年"):
        parts.append(f"{y} {((1+g['收益%']/100).prod()-1)*100:+6.1f}%")
    print(f"  {label:22s} " + "  ".join(parts))

print("\n⑥ 稳健性:换进场门槛(只做空,看相邻档同不同向)")
print(f"  {'形态':22s}" + "".join(f"{f'z<=-{e}':>12s}" for e in (0.8, 1.0, 1.2, 1.5)))
for label, base in [("现有引擎", "prod"), ("共振", "res"), ("与", "and")]:
    row = f"  {label:22s}"
    for e in (0.8, 1.0, 1.2, 1.5):
        if base == "prod": m = pz <= -e
        elif base == "res": m = (rz <= -e) & res
        else: m = (pz <= -e) & (rz <= -e) & res
        o = R.stats(short_only(m), mr, "", quiet=True)
        row += f"{o['累计%']:>+11.1f}%" if o else f"{'—':>12s}"
    print(row)
