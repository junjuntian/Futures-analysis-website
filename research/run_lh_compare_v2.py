"""生猪:现有引擎 vs 共振,**修正版**。

上一版(run_lh_combine.py)有两个错,运营者从界面数字对不上发现的:
  ① 时间轴用 2024-01,而生产引擎从 2023-08 起——两者不可比;
  ② **出场判断一律用散户信号 rz**,连「只用现有引擎」那一行也是,
     所以测出来的根本不是现有引擎,是个混合体。

这一版:每路信号**各用各的信号进场、各用各的信号出场**,并先与生产引擎的
真实输出对拍(21 笔 / 累计 +83.5% 毛 / 胜率 71.4%),对上了才往下比。
"""
from __future__ import annotations
import numpy as np, pandas as pd
import lhlib as L
from run_flow_skill import seat_alpha
from run_lh_phase2 import main_returns

CODE = "LH"
PROD_START = pd.Timestamp("2023-08-11")     # engine/hog_money.py 的 replay_start
RETAIL = ["东方财富", "平安期货", "徽商期货"]

price = L.load_price(CODE); seat = L.load_seat(CODE)
df = seat.merge(price[["contract", "trade_date", "settle"]],
                on=["contract", "trade_date"], how="inner")
mr_all = main_returns(price)


def rolling_group_signal(dates, reselect_months=12, k=5, warmup=250):
    """与 engine/hog_money.py 完全同口径:滚动重选 + 5 日变化 + 120 日标准差。"""
    cuts = pd.date_range(dates.min() + pd.Timedelta(days=warmup), dates.max(),
                         freq=f"{reselect_months}MS")
    picks, cur = {}, None
    for c in cuts:
        a = seat_alpha(df[df["trade_date"] < c], CODE, min_days=120)
        if not a.empty and len(a) >= k:
            cur = tuple(a.sort_values("alpha", ascending=False).head(k).index)
        picks[c] = cur
    net = pd.Series(index=dates, dtype=float)
    for grp in {picks[c] for c in cuts if picks[c]}:
        days = [d for d in dates if (lambda v: picks[v[-1]] if v else None)(
            [c for c in cuts if c <= d]) == grp]
        if not days: continue
        # **用全量 seat 而不是与行情内连接后的 df**——席位持仓不依赖于当天有没有
        # 对应合约的价格。2026-08-19 对拍时正是这里差出一笔(2024-05-17):
        # 引擎用 seat,研究这边用了 df,少掉几行席位就让信号在边界上跨过了门槛。
        s = seat[seat["member_key"].isin(list(grp))].groupby("trade_date")["net"].sum().sort_index()
        net.loc[days] = s.diff(5).reindex(days).values
    return net / net.rolling(120, min_periods=60).std()


def retail_signal(dates):
    have = [m for m in RETAIL if m in set(seat["member_key"])]
    s = seat[seat["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().reindex(dates)
    chg = s.diff(5)
    return -(chg - chg.rolling(120, min_periods=60).mean()) / chg.rolling(120, min_periods=60).std()


def backtest(z_entry, z_exit, mr, enter=1.0, hold=40, stop=0.06, cost=0.0005,
             short_only=True):
    """**进场与出场用同一路信号**(z_entry/z_exit 传同一个就是纯单信号)。"""
    # 成交口径:信号日收盘出信号,**次日开盘成交**(DEC-090),与引擎同口径。
    idx = mr.index; op = mr["open"]; ro = mr["ret_open"]; o2c = mr["o2c"]
    trades=[]; side=0; ei=None; v=1.0; cum=0.0

    def fill(k):
        if k+1 >= len(idx): return np.nan
        return float(op.iloc[k+1]) if np.isfinite(op.iloc[k+1]) else np.nan

    for i, d in enumerate(idx):
        ze, zx = z_entry.get(d, np.nan), z_exit.get(d, np.nan)
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
                           "收益%":(booked-2*cost)*100,"持有":i-ei,"原因":reason})
            side, v, cum = 0, 1.0, 0.0
        if side == 0 and np.isfinite(ze) and np.isfinite(fill(i)):
            want = -1 if ze <= -enter else (0 if short_only else (1 if ze >= enter else 0))
            if want: side, ei, v, cum = want, i, 1.0, 0.0
    return pd.DataFrame(trades)


def daily_series(tr, mr, cost=0.0005):
    """逐日净值,与 engine/hog_money.py 的 replay 同构:持仓区间 [open_{i+1},
    open_{j+1}] 吃 ret_open[i+2..j+1],成本记在两个成交日上。这样它连乘起来
    **恒等于**逐笔记账 —— 以前这里用 `pos×结算价收益` 另算一条,和逐笔对不上。"""
    idx = mr.index; ro = mr["ret_open"].fillna(0.0).to_numpy()
    loc = {d: i for i, d in enumerate(idx)}
    d = pd.Series(0.0, index=idx)
    for _, t in tr.iterrows():
        i0, j0 = loc[t["进场"]], loc[t["出场"]]
        sd = 1.0 if t["方向"] == "多" else -1.0
        for k in range(i0+2, j0+2):
            if k < len(idx): d.iloc[k] = sd*ro[k]
        if i0+1 < len(idx): d.iloc[i0+1] -= cost
        if j0+1 < len(idx): d.iloc[j0+1] -= cost
    return d


def perf(tr, mr, label, cost=0.0005):
    if tr.empty: print(f"  {label:24s} 无交易"); return
    daily = daily_series(tr, mr, cost)
    eq=(1+daily).cumprod(); dd=(eq/eq.cummax()-1).min()
    sh=daily.mean()/daily.std()*np.sqrt(242) if daily.std()>0 else np.nan
    gross=(np.prod(1+(tr["收益%"]+2*cost*100)/100)-1)*100
    print(f"  {label:24s} {len(tr):2d}笔 毛{gross:+7.1f}% 净{(eq.iloc[-1]-1)*100:+7.1f}% "
          f"胜率{(tr['收益%']>0).mean()*100:5.1f}% 均值{tr['收益%'].mean():+5.2f}% "
          f"回撤{dd*100:6.1f}% 夏普{sh:5.2f}")

print("=== 0. 先与生产引擎对拍(它跑的是 2023-08 起、只做空、持满 40) ===")
mr = mr_all[mr_all.index >= PROD_START]
pz = rolling_group_signal(mr.index)
tr_prod = backtest(pz, pz, mr)
perf(tr_prod, mr, "复刻生产引擎")
print("  (口径已于 DEC-090 改为次日开盘成交,这一行是**旧口径**的历史记录,别拿来对账)")
print("  ↑ 对得上才说明复刻没问题,下面的比较才算数")

print("\n=== 1. 同一时间轴(2023-08 起)对比,各用各的信号进出场 ===")
rz = retail_signal(mr.index)
perf(backtest(pz, pz, mr), mr, "A 现有引擎")
perf(backtest(rz, rz, mr), mr, "B 散户反向(只做空)")
res = np.sign(pz) == np.sign(rz)
rz_res = rz.where(res)
perf(backtest(rz_res, rz, mr), mr, "C 共振进场/散户出场")

print("\n=== 2. 逐年(看改进是不是稳定,还是靠某一年) ===")
cands = {"A 现有引擎": (pz, pz), "B 散户反向": (rz, rz), "C 共振进场": (rz_res, rz)}
for label, (ze, zx) in cands.items():
    tr = backtest(ze, zx, mr)
    if tr.empty: continue
    tr["年"] = tr["出场"].dt.year
    parts = [f"{y} {((1+g['收益%']/100).prod()-1)*100:+6.1f}%({len(g)}笔)"
             for y, g in tr.groupby("年")]
    print(f"  {label:12s} " + "  ".join(parts))

print("\n=== 3. 差异有没有统计意义(21 笔样本上,10 个点的差可能是噪音) ===")
import itertools
base = backtest(pz, pz, mr)["收益%"]
for label, (ze, zx) in [("B 散户反向", (rz, rz)), ("C 共振进场", (rz_res, rz))]:
    other = backtest(ze, zx, mr)["收益%"]
    # 两组独立样本的均值差 t 检验(笔与笔之间近似独立,持仓不重叠)
    n1, n2 = len(base), len(other)
    s = np.sqrt(base.var(ddof=1)/n1 + other.var(ddof=1)/n2)
    t = (other.mean() - base.mean()) / s if s > 0 else np.nan
    print(f"  {label} 单笔均值 {other.mean():+.2f}% vs 现有 {base.mean():+.2f}%  "
          f"差 {other.mean()-base.mean():+.2f}%  t={t:+.2f}  "
          f"{'显著' if abs(t)>2 else '**不显著,这点差距可能是噪音**'}")

print("\n=== 4. 门槛稳健性(各自用自己的信号) ===")
print(f"  {'方案':12s}" + "".join(f"{f'|z|>={e}':>12s}" for e in (0.8, 1.0, 1.2, 1.5)))
for label, (ze, zx) in cands.items():
    row = f"  {label:12s}"
    for e in (0.8, 1.0, 1.2, 1.5):
        tr = backtest(ze, zx, mr, enter=e)
        row += f"{(np.prod(1+tr['收益%']/100)-1)*100:>+11.1f}%" if not tr.empty else f"{'—':>12s}"
    print(row)
