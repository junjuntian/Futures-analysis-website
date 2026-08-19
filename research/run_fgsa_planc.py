"""玻璃/纯碱按生猪同一套(方案 C:共振进场/散户出场)测,决定引擎参数。

生猪是**只做空**(DEC-084 关掉做多支路)。玻璃纯碱要不要一样?Phase 2 那轮是双向的,
没单独验过只做空。上线前必须测——不能假设换个品种规则照抄。
"""
from __future__ import annotations
import numpy as np, pandas as pd
import lhlib as L
from run_flow_skill import seat_alpha
from run_lh_phase2 import main_returns

RETAIL = ["东方财富", "平安期货", "徽商期货"]
STARTS = {"FG": pd.Timestamp("2013-01-01"), "SA": pd.Timestamp("2020-06-01")}


def prep(code):
    price = L.load_price(code); seat = L.load_seat(code)
    df = seat.merge(price[["contract", "trade_date", "settle"]],
                    on=["contract", "trade_date"], how="inner")
    mr = main_returns(price); mr = mr[mr.index >= STARTS[code]]
    cuts = pd.date_range(mr.index.min() + pd.Timedelta(days=250), mr.index.max(), freq="12MS")
    picks, cur = {}, None
    for c in cuts:
        a = seat_alpha(df[df["trade_date"] < c], code, min_days=120)
        if not a.empty and len(a) >= 5:
            cur = tuple(a.sort_values("alpha", ascending=False).head(5).index)
        picks[c] = cur
    net = pd.Series(index=mr.index, dtype=float)
    for grp in {picks[c] for c in cuts if picks[c]}:
        days = [d for d in mr.index
                if (lambda v: picks[v[-1]] if v else None)([c for c in cuts if c <= d]) == grp]
        if not days: continue
        s = seat[seat["member_key"].isin(list(grp))].groupby("trade_date")["net"].sum().sort_index()
        net.loc[days] = s.diff(5).reindex(days).values
    pz = net / net.rolling(120, min_periods=60).std()
    have = [m for m in RETAIL if m in set(seat["member_key"])]
    rs = seat[seat["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().reindex(mr.index)
    rchg = rs.diff(5)
    rz = -(rchg - rchg.rolling(120, min_periods=60).mean()) / rchg.rolling(120, min_periods=60).std()
    return mr, pz, rz, have


def bt(z_in, z_out, mr, enter=1.0, hold=40, stop=0.06, cost=0.0005, short_only=True,
       long_needs_dip=False):
    # long_needs_dip 是生猪时代的遗留(Phase 0:跌×减空是唯一正格子),
    # 对玻璃纯碱从没验过。引擎默认带着它,所以这里要能开关才对得上。
    # 成交口径:信号日收盘出信号,**次日开盘成交**(DEC-090)。席位数据收盘后
    # 才公布,按信号日结算价成交做不到。净值走开→开,止损判的是今天收盘已知的
    # 浮亏(开→开累计 × 当日开→结算)。与 engine/hog_money.py 的 replay 同口径。
    past = mr["settle"].pct_change(20)
    idx = mr.index; op = mr["open"]; ro = mr["ret_open"]; o2c = mr["o2c"]
    trades=[]; side=0; ei=None; v=1.0; cum=0.0

    def fill(k):
        if k+1 >= len(idx): return np.nan
        return float(op.iloc[k+1]) if np.isfinite(op.iloc[k+1]) else np.nan

    for i, d in enumerate(idx):
        ze, zx = z_in.get(d, np.nan), z_out.get(d, np.nan)
        if side != 0:
            if i >= ei+2:
                rr = ro.iloc[i]; v *= 1 + side*(rr if np.isfinite(rr) else 0.0)
            cc = o2c.iloc[i]
            cum = v*(1 + side*(cc if np.isfinite(cc) else 0.0)) - 1 if i > ei else 0.0
        reason=None
        if side != 0 and i > ei:
            if cum <= -stop: reason="止损"
            elif i-ei >= hold: reason="持满"
            elif np.isfinite(zx) and side*zx <= -enter: reason="反向"
        if reason and not np.isfinite(fill(i)): reason=None
        if reason:
            rn = ro.iloc[i+1]
            booked = v*(1 + side*(rn if np.isfinite(rn) else 0.0)) - 1
            trades.append({"进场":idx[ei],"出场":d,"方向":"多" if side>0 else "空",
                           "收益%":(booked-2*cost)*100,"持有":i-ei})
            side, v, cum = 0, 1.0, 0.0
        if side == 0 and np.isfinite(ze) and np.isfinite(fill(i)):
            want = -1 if ze <= -enter else 0
            if ze >= enter and not short_only:
                pv = past.get(d, float("nan"))
                if (not long_needs_dip) or (np.isfinite(pv) and pv < 0):
                    want = 1
            if want: side, ei, v, cum = want, i, 1.0, 0.0
    return pd.DataFrame(trades)


def rep(tr, mr, label, cost=0.0005):
    if tr.empty: print(f"  {label:22s} 无交易"); return
    # 与引擎同构的逐日净值(开→开、成本记在成交日),连乘恒等于逐笔。
    idx = mr.index; ro = mr["ret_open"].fillna(0.0).to_numpy()
    loc = {d: i for i, d in enumerate(idx)}
    daily = pd.Series(0.0, index=idx)
    for _, t in tr.iterrows():
        i0, j0 = loc[t["进场"]], loc[t["出场"]]
        sd = 1.0 if t["方向"] == "多" else -1.0
        for k in range(i0+2, j0+2):
            if k < len(idx): daily.iloc[k] = sd*ro[k]
        if i0+1 < len(idx): daily.iloc[i0+1] -= cost
        if j0+1 < len(idx): daily.iloc[j0+1] -= cost
    eq=(1+daily).cumprod(); dd=(eq/eq.cummax()-1).min()
    sh=daily.mean()/daily.std()*np.sqrt(242) if daily.std()>0 else np.nan
    n_long=(tr["方向"]=="多").sum()
    print(f"  {label:22s} {len(tr):3d}笔(多{n_long}/空{len(tr)-n_long}) 净{(eq.iloc[-1]-1)*100:+8.1f}% "
          f"胜率{(tr['收益%']>0).mean()*100:5.1f}% 回撤{dd*100:6.1f}% 夏普{sh:5.2f} "
          f"在场{(daily!=0).mean()*100:3.0f}%")

for code in ("FG", "SA"):
    mr, pz, rz, have = prep(code)
    res = np.sign(pz) == np.sign(rz)
    rz_res = rz.where(res)
    bh=(1+mr["ret"].fillna(0)).prod()-1; sh_=(1-mr["ret"].fillna(0)).prod()-1
    print(f"\n【{code}】{mr.index.min():%Y-%m}~{mr.index.max():%Y-%m}  散户={'、'.join(have)}")
    print(f"  对照:买入持有 {bh*100:+.1f}%  恒定满仓做空 {sh_*100:+.1f}%")
    rep(bt(rz_res, rz, mr), mr, "方案C 只做空")
    rep(bt(rz_res, rz, mr, short_only=False), mr, "方案C 双向(不要 dip)")
    rep(bt(rz_res, rz, mr, short_only=False, long_needs_dip=True), mr, "方案C 双向(要 dip)")
    rep(bt(rz, rz, mr), mr, "散户反向 只做空")
    rep(bt(pz, pz, mr), mr, "聪明钱 只做空")
    tr = bt(rz_res, rz, mr)
    if not tr.empty:
        tr["年"]=tr["出场"].dt.year
        print("  方案C只做空 逐年:" + "  ".join(
            f"{y}{((1+g['收益%']/100).prod()-1)*100:+.0f}%" for y,g in tr.groupby("年")))
