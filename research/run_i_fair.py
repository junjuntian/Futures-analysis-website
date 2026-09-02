# -*- coding: utf-8 -*-
"""铁矿石:用**和焦煤当年完全相同的尺子**重测(2026-09-02 运营者质疑)。

运营者:「按你的说法铁矿石没有赚钱的席位吗?即使焦煤玻璃这种改来改去的品种
都能做出策略,为什么铁矿石做不出来?」

## 我先承认一件事:尺子不一致

| | 候选池 | 第七关 p | 判定 |
|---|---|---|---|
| 玻璃跟永安 | 五选一 | 安慰剂 p=0.000(14 年样本) | 过 |
| **焦煤跟华泰** | **席位组五家 → 五选一** | **Bonferroni p≈0.17,未过** | **靠 IC 旁证上线** |
| 铁矿石跟永安 | **全市场 47 家** | 最大统计量 p=0.17,未过 | **我判否** |

**同一个 p 值(0.17),焦煤上线了,铁矿石被我否了。** 差别有两处:

1. **候选池**:焦煤只在席位组那五家里选,我给铁矿石扫了全市场 47 家 ——
   同样的规则,分母差近十倍,惩罚就差十倍;
2. **旁证**:焦煤有「华泰是五家里唯一 5 日流向 IC 显著者(t=2.32)」这条
   **另一条通路**的独立指认。**而我当时说「铁矿石没找到等价旁证」—— 我没真去测。**

本文把这两处都补上,**用焦煤那套口径**,不是重新发明一把尺子。

## 两件事

- **A. 五选一**:把候选池限定为席位组那五家(永安/海通/东证/中信/华泰),
  跑最大统计量置换 + Bonferroni,与焦煤逐条对应;
- **B. IC 旁证**:算五家各自的 5 日流向与未来 5 日收益的相关(IC)与 t 值,
  看永安是不是像华泰那样**唯一显著**。

**判定沿用焦煤的口径**(不是我另定的):五选一校正后不过,但若有**另一条通路
独立指认同一家**,则按「打折看」上线,丑话照挂。

跑法:python research/run_i_fair.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

COST = 0.0005
SIMS = 500
IC_WIN = 5           # 5 日流向,与焦煤 REPORT_JM_SEAT_PICK_v1 同
FWD = 5              # 未来 5 日收益
rng = np.random.default_rng(20260902)

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "i_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "i_seat.csv.gz"))
H.use("I")
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
idx = mkt.index
n = len(idx)
groups, _l, _c = H.rolling_groups(seat, price, idx)
# 席位组 = 引擎当日实际在用的那五家。**这就是焦煤当年的候选池**。
POOL = list(groups.get(idx[-1]) or ())


def position(member: str) -> pd.Series:
    sub = seat[seat["member_key"] == member]
    sig = pd.Series(np.nan, index=idx)
    for c in dict.fromkeys(mkt["main"]):
        if not isinstance(c, str):
            continue
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = idx[mkt["main"] == c]
        if not len(days):
            continue
        sig.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
    pos = np.sign(sig)
    pos[pos == 0] = np.nan
    return pos.ffill()


def sharpe_of(pos: pd.Series, lag: int = 2, cost: float = COST) -> float:
    p = pos.shift(lag)
    turn = (pos.shift(lag) != pos.shift(lag + 1)).astype(float)
    daily = (p * mkt["ret_open"] - turn * cost * 2).dropna()
    if len(daily) < 20 or daily.std() == 0:
        return np.nan
    return float(daily.mean() / daily.std() * np.sqrt(242))


def cum_of(pos: pd.Series, lag: int = 2, cost: float = COST) -> float:
    p = pos.shift(lag)
    turn = (pos.shift(lag) != pos.shift(lag + 1)).astype(float)
    daily = (p * mkt["ret_open"] - turn * cost * 2).dropna()
    if not len(daily):
        return np.nan
    return (float(np.prod(1 + daily)) - 1) * 100


L = [f"铁矿石:用焦煤那把尺子重测(样本 {idx[0].date()} ~ {idx[-1].date()},{n} 日)", ""]
L.append(f"席位组(引擎当日在用,**= 焦煤当年的候选池口径**):{'、'.join(POOL)}")
L.append("")

# ---------------------------------------------------------------- A 五选一
L.append("## A、五选一(与焦煤逐条对应)")
L.append("")
POS = {m: position(m) for m in POOL}
rows = [(m, sharpe_of(POS[m]), cum_of(POS[m])) for m in POOL]
rows.sort(key=lambda r: (-r[1]) if np.isfinite(r[1]) else 9)
L.append(f"{'席位':<12}{'夏普':>8}{'累计%':>10}")
L.append("-" * 32)
for m, sh, cu in rows:
    L.append(f"{m:<12}{sh:>8.2f}{cu:>+10.1f}")
L.append("")
best_m, best_sh, _ = rows[0]
L.append(f"**五家里第一是 {best_m}**(夏普 {best_sh:.2f})。")
L.append("")

shifts = rng.integers(1, n, size=SIMS)
sims_single = np.empty(SIMS)
sims_max = np.full(SIMS, -np.inf)
for m in POOL:
    v = POS[m].values
    arr = np.array([sharpe_of(pd.Series(np.roll(v, sh), index=idx)) for sh in shifts])
    if m == best_m:
        sims_single = arr
    ok = np.isfinite(arr)
    sims_max[ok] = np.maximum(sims_max[ok], arr[ok])
sims_max = sims_max[np.isfinite(sims_max)]
p_single = float((np.sum(sims_single >= best_sh) + 1) / (SIMS + 1))
p_bonf = min(p_single * len(POOL), 1.0)
p_max5 = float((np.sum(sims_max >= best_sh) + 1) / (len(sims_max) + 1))
L.append(f"  单席位安慰剂 p = {p_single:.4f}(按 (r+1)/(m+1))")
L.append(f"  **五选一 Bonferroni p = {p_bonf:.4f}**  "
         f"{'过' if p_bonf < 0.05 else '不过'}   ← 焦煤当年这一格是 ≈0.17,未过")
L.append(f"  **五选一最大统计量 p = {p_max5:.4f}**  "
         f"{'过' if p_max5 < 0.05 else '不过'}   ← 比 Bonferroni 准,因为五家高度相关")
L.append("")
L.append("对照:全市场 47 家扫描时最大统计量 p = 0.17(REPORT_I_FOLLOW_v1)。")
L.append("**同一条规则、同一份数据,只因候选池从 47 缩到 5,结论就不同了** ——")
L.append("这不是数据变了,是「我做了多少次尝试」变了。哪个池子才算数,是个方法学问题:")
L.append("焦煤当年用的是席位组五家,本文照它。")
L.append("")

# ---------------------------------------------------------------- B IC 旁证
L.append("## B、IC 旁证:永安是不是像华泰那样「唯一显著」")
L.append("")
L.append(f"口径同焦煤 REPORT_JM_SEAT_PICK_v1:{IC_WIN} 日净持仓变化 vs 未来 {FWD} 日收益,")
L.append("按主力合约、Spearman 秩相关,t = IC均值 / (IC标准差/√N)。")
L.append("")
ret_f = pd.Series(
    [(float(np.prod(1 + mkt["ret_open"].iloc[i + 1:i + 1 + FWD])) - 1) if i + 1 + FWD <= n else np.nan
     for i in range(n)], index=idx)
L.append(f"{'席位':<12}{'IC':>9}{'t 值':>9}{'样本':>7}  显著")
L.append("-" * 44)
ic_rows = []
for m in POOL:
    sub = seat[seat["member_key"] == m]
    net = sub.groupby("trade_date")["net_off"].sum().reindex(idx).ffill()
    flow = net.diff(IC_WIN)
    df = pd.concat([flow.rename("f"), ret_f.rename("r")], axis=1).dropna()
    if len(df) < 100:
        L.append(f"{m:<12}{'—':>9}{'—':>9}{len(df):>7}  样本不足")
        continue
    # 滚动 60 日窗内的秩相关,再对这些 IC 求 t —— 与焦煤同,避免整段单值无从检验
    ics = []
    for s in range(0, len(df) - 60, 20):
        w = df.iloc[s:s + 60]
        if w["f"].std() > 0 and w["r"].std() > 0:
            # 秩相关自己算:环境里没有 scipy,而 pandas 的 spearman 要靠它。
            # 「先转秩再求 Pearson」与 Spearman 定义等价,不是近似。
            ics.append(float(w["f"].rank().corr(w["r"].rank())))
    if len(ics) < 5:
        L.append(f"{m:<12}{'—':>9}{'—':>9}{len(ics):>7}  窗口不足")
        continue
    ics = np.array(ics)
    t = float(ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics)))) if ics.std(ddof=1) > 0 else np.nan
    ic_rows.append((m, ics.mean(), t))
    L.append(f"{m:<12}{ics.mean():>+9.3f}{t:>+9.2f}{len(ics):>7}  "
             f"{'**显著**' if abs(t) >= 2 else '不显著'}")
L.append("")
sig_seats = [m for m, _ic, t in ic_rows if abs(t) >= 2]
L.append(f"  显著的席位:{'、'.join(sig_seats) if sig_seats else '一家都没有'}")
if sig_seats == [best_m]:
    L.append(f"  → **{best_m} 是五家里唯一显著者**,与华泰在焦煤上的地位相同:")
    L.append("     另一条通路(流向 IC)独立指认了同一家,不是回测挑出来的。")
elif best_m in sig_seats:
    L.append(f"  → {best_m} 显著,但**不唯一** —— 旁证比焦煤那次弱。")
else:
    L.append(f"  → **{best_m} 不在显著名单里** —— 没有等价旁证,这一条撑不起来。")
L.append("")

# ---------------------------------------------------------------- 五家池的走前
L.append("## B2、走前检验:候选池同样限定为这五家")
L.append("")
L.append("`REPORT_I_FOLLOW_v1` 里那个 −13.2% 是**在 47 家里**按季度重挑的结果。")
L.append("池子换成五家之后要重算 —— 不重算就是拿一个口径的检验去否另一个口径的结论。")
L.append("")
DAILY = {}
for m in POOL:
    pos = POS[m]
    pp = pos.shift(2)
    turn = (pos.shift(2) != pos.shift(3)).astype(float)
    DAILY[m] = (pp * mkt["ret_open"] - turn * COST * 2).dropna()
picks, wf_daily = [], []
for q in pd.date_range(idx[0], idx[-1], freq="QE"):
    test = idx[(idx > q) & (idx <= q + pd.offsets.QuarterEnd(1))]
    if len(test) < 10:
        continue
    pick, pick_sh = None, -np.inf
    for m, d_all in DAILY.items():
        d_ = d_all[d_all.index <= q]
        if len(d_) < 60 or d_.std() == 0:
            continue
        sh = float(d_.mean() / d_.std() * np.sqrt(242))
        if sh > pick_sh:
            pick, pick_sh = m, sh
    if pick is None:
        continue
    d_test = DAILY[pick].reindex(test).dropna()
    if not len(d_test):
        continue
    picks.append((str(q.date()), pick, pick_sh, (float(np.prod(1 + d_test)) - 1) * 100))
    wf_daily.append(d_test)
if wf_daily:
    wf = pd.concat(wf_daily).sort_index()
    wf_cum = (float(np.prod(1 + wf)) - 1) * 100
    wf_sh = float(wf.mean() / wf.std() * np.sqrt(242)) if wf.std() > 0 else np.nan
    L.append(f"{'重挑日':<12}{'挑中':<12}{'训练夏普':>9}{'下季实得%':>11}")
    L.append("-" * 46)
    for d_, m_, sh_, r_ in picks:
        L.append(f"{d_:<12}{m_:<12}{sh_:>9.2f}{r_:>+11.1f}")
    L.append("-" * 46)
    hit = sum(1 for _, m_, _, _ in picks if m_ == best_m)
    L.append(f"  **五家池走前:合计 {wf_cum:+.1f}%、夏普 {wf_sh:.2f}**;"
             f"{len(picks)} 次里 {hit} 次挑中{best_m}")
    L.append(f"  对照:47 家池走前 −13.2%(REPORT_I_FOLLOW_v1)")
L.append("")

# ---------------------------------------------------------------- 判定
L.append("## C、按焦煤那套口径判定")
L.append("")
L.append("焦煤当年:五选一 Bonferroni p≈0.17 **未过**,但 IC 独立指认同一家 → 打折上线。")
L.append("")
if p_bonf < 0.05 or p_max5 < 0.05:
    L.append(f"铁矿石:五选一校正后 **p = {min(p_bonf, p_max5):.4f},比焦煤当年还干净** ——")
    L.append("焦煤是靠旁证撑先验才上的,这里是校正本身就过。")
elif best_m in sig_seats:
    L.append("铁矿石:五选一校正未过,但 IC 指认同一家 —— **与焦煤当年同款**。")
else:
    L.append("铁矿石:五选一校正未过,且 IC 没指认同一家 —— **比焦煤当年弱**,不该上。")
L.append("")
L.append("**必须同时记住的一条**:席位组那五家本身是按历史择时收益滚动选出来的,")
L.append("把候选池限定成它,等于**把一部分挑人动作藏进了池子的定义里**。焦煤当年")
L.append("也有这个问题 —— 本文照搬它的口径是为了可比,不是说这个口径没有毛病。")
io.open(OUT / "i_fair.txt", "w", encoding="utf-8").write("\n".join(L))
print("done ->", OUT / "i_fair.txt")
