# -*- coding: utf-8 -*-
"""焦煤第二引擎换人(预注册 PLAN_JM_FOLLOW2_v1)。

**先问 Q1「加个仓位够不够」,再问 Q2「换谁」。** 两层结构:
Tier A = 席位组 5 家(可据此上线);Tier B = 全部 43 家(侦察,绝不判定)。

约束:大商所席位数据只有 3 年(2023-08-11 起)、43 家池子 —— 选择偏差是主要敌人。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
TIER_A = ("东证期货", "国泰君安", "华泰期货", "永安期货", "东吴期货")
INCUMBENT = "华泰期货"
FEE = 2.0
PLACEBO_N = 1000
PLACEBO_MIN = 60
BONF = 4                      # Tier A 的换人候选数,事前写死
rng = np.random.default_rng(20260906)

H.use("JM")
price = H.clean_price(pd.read_csv(D / "jm_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "jm_seat.csv.gz"))
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
IDX = mkt.index
mkt = mkt.assign(ret_settle=mkt["settle"].pct_change())
g, log, cuts = H.rolling_groups(seat, price, IDX)
rdf, _ = H.retail_series(seat, IDX)
raw = H.signal_series(seat, g)
MULT = float(H.RULES["multiplier"])
HALVES = ((2023, 2024), (2025, 2026))


def main_net(member: str) -> pd.Series:
    sub = seat[seat["member_key"] == member]
    s = pd.Series(np.nan, index=IDX)
    for c in dict.fromkeys(mkt["main"]):
        if not isinstance(c, str):
            continue
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = IDX[mkt["main"] == c]
        s.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
    return s


def run(sig: pd.Series, *, sizing=True, fill="open", const=None, extra_fee=True):
    """`extra_fee=False` = 线上满仓口径(只扣翻转的 0.05%,不扣调仓费)。

    G0 校准必须用它 —— 线上 `seat_follow_payload` 在没配 sizing 时走的就是
    `pos.shift(2)*ret_open - turn*0.001`,**没有 2 元/手/边那一项**。
    第一次跑本脚本时我在满仓臂上也扣了调仓费,累计就差了 0.9pp,**G0 当场把它抓了出来**。
    """
    pos = np.sign(sig)
    pos[pos == 0] = np.nan
    pos = pos.ffill()
    if const is not None:
        w = pd.Series(float(const), index=IDX)
    elif sizing:
        w = (sig.abs() / H.rolling_top_mean(sig, win=H.FOLLOW_SIZING_WIN,
                                            min_periods=60)).clip(upper=1.0)
    else:
        w = pd.Series(1.0, index=IDX)
    w = w.reindex(IDX).fillna(0.0).clip(0.0, 1.0)
    lag = 1 if fill == "settle_t" else 2
    ret = mkt["ret_open"] if fill == "open" else mkt["ret_settle"]
    turn = (pos.shift(lag) != pos.shift(lag + 1)).astype(float)
    wl = w.shift(lag).fillna(0.0)
    expo = (pos * w).fillna(0.0)
    extra = expo.diff().abs().fillna(expo.abs()).shift(lag).fillna(0.0)
    d = pos.shift(lag) * ret * wl - turn * 0.001 * wl
    if extra_fee:
        d = d - extra * FEE / (mkt["settle"].ffill() * MULT)
    d = d.dropna()
    flips = int((pos != pos.shift(1)).fillna(False).sum())
    return d, pos, w, flips


def perf(d):
    eq = (1 + d).cumprod()
    return {"cum": (float(eq.iloc[-1]) - 1) * 100,
            "sharpe": float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else float("nan"),
            "dd": float((eq / eq.cummax() - 1).min()) * 100}


def sharpe(d):
    d = d.dropna()
    return float(d.mean() / d.std() * np.sqrt(242)) if len(d) > 2 and d.std() > 0 else float("nan")


# 主引擎(共振进场 + 机构出场),给 G4 用
sig_m = H.attach_inst_exit(raw, seat, mkt, g)
tr_m, pos_m, MAIN = H.replay(sig_m, mkt, rdf, op, st)
YEARS = (IDX[-1] - IDX[0]).days / 365.25

print("=" * 96)
print("焦煤第二引擎换人(PLAN_JM_FOLLOW2_v1)")
print(f"样本 {IDX[0].date()}~{IDX[-1].date()},{len(IDX)} 个交易日 ≈ {YEARS:.1f} 年")
print(f"主引擎单跑 夏普 {sharpe(MAIN):.2f}")

# ---- G0 校准:华泰满仓 ----
sig_ht = main_net(INCUMBENT)
d0, pos0, _, f0 = run(sig_ht, sizing=False, extra_fee=False)   # 线上口径
p0 = perf(d0)
g0 = (abs(p0["cum"] - 150.0) < 0.06 and abs(p0["sharpe"] - 0.97) < 0.006
      and abs(p0["dd"] + 32.7) < 0.06 and f0 == 69)
print(f"\n[G0 校准] 华泰满仓 = {p0['cum']:+.1f}% / {p0['sharpe']:.2f} / {p0['dd']:.1f}% / "
      f"{f0} 翻(线上 +150.0 / 0.97 / −32.7 / 69)→ {'**过**' if g0 else '**不过 —— 项目作废**'}")

# ---- Q1:华泰 + 200 日缩仓 ----
print("\n[Q1] 先问「加个仓位够不够」—— 华泰不换人,只加 200 日缩仓")
d1, pos1, w1, f1 = run(sig_ht, sizing=True)
p1 = perf(d1)
held = (pos1.abs() > 0).reindex(d1.index).fillna(False)
avg_w1 = float(w1.shift(2).reindex(d1.index).fillna(0)[held].mean())
dc1, _, _, _ = run(sig_ht, const=avg_w1)
print(f"  {'臂':<20}{'累计':>10}{'夏普':>7}{'回撤':>9}{'翻转':>7}{'平均敞口':>10}")
print(f"  {'现役(满仓·线上口径)':<15}{p0['cum']:>+9.1f}%{p0['sharpe']:>7.2f}{p0['dd']:>8.1f}%{f0:>7}{'100%':>10}")
# 公平对照:满仓也按同一套费率扣(缩仓臂扣了调仓费,满仓臂不扣就是让它占便宜)
d0f = run(sig_ht, sizing=False)[0]
p0f = perf(d0f)
print(f"  {'现役(满仓·同费率)':<16}{p0f['cum']:>+9.1f}%{p0f['sharpe']:>7.2f}{p0f['dd']:>8.1f}%{f0:>7}{'100%':>10}")
print(f"  {'华泰 + 200日缩仓':<17}{p1['cum']:>+9.1f}%{p1['sharpe']:>7.2f}{p1['dd']:>8.1f}%{f1:>7}{avg_w1:>10.0%}")
print(f"  {'   └ 等效缩仓对照':<17}{perf(dc1)['cum']:>+9.1f}%{sharpe(dc1):>7.2f}"
      f"{perf(dc1)['dd']:>8.1f}%{'':>7}{avg_w1:>10.0%}")
q1_g1 = p1["sharpe"] >= sharpe(dc1)
q1_hs = [sharpe(d1[(d1.index.year >= lo) & (d1.index.year <= hi)]) for lo, hi in HALVES]
q1_g2 = all(np.isfinite(x) and x > 0 for x in q1_hs)
comb1 = (MAIN.reindex(IDX).fillna(0) * .5 + d1.reindex(IDX).fillna(0) * .5)
q1_g4 = sharpe(comb1) > sharpe(MAIN)
q1_g5 = sharpe(run(sig_ht, fill="close")[0]) > 0
print(f"  G1 胜过等效缩仓 {p1['sharpe']:.2f} vs {sharpe(dc1):.2f} → {'过' if q1_g1 else '否'}"
      f" · G2 半样本 {q1_hs[0]:+.2f}/{q1_hs[1]:+.2f} → {'过' if q1_g2 else '否'}"
      f" · G4 组合 {sharpe(comb1):.2f} vs {sharpe(MAIN):.2f} → {'过' if q1_g4 else '否'}"
      f" · G5 → {'过' if q1_g5 else '否'}")
print(f"  运营者的两条抱怨:回撤 {p0['dd']:.1f}% → {p1['dd']:.1f}%;"
      f"翻转 {f0} → {f1}(**方向没变,缩仓治不了翻转次数**,只是每次的仓位更小)")

# ---- Q2:Tier A 四个换人候选 ----
print("\n[Q2 · Tier A] 席位组另外 4 家(带 200 日缩仓)")
print(f"  {'席位':<10}{'累计':>10}{'夏普':>7}{'回撤':>9}{'翻转':>7}{'敞口':>7}"
      f"{'等效缩仓':>10}{'G1':>4}{'半样本':>15}{'G2':>4}{'G_R':>5}")
cand = {}
for m in TIER_A:
    if m == INCUMBENT:
        continue
    s = main_net(m)
    if s.notna().sum() < 150:
        print(f"  {m:<10}样本不足,跳过")
        continue
    d, pos, w, f = run(s)
    p = perf(d)
    hd = (pos.abs() > 0).reindex(d.index).fillna(False)
    aw = float(w.shift(2).reindex(d.index).fillna(0)[hd].mean())
    dc, _, _, _ = run(s, const=aw)
    g1 = p["sharpe"] >= sharpe(dc)
    hs = [sharpe(d[(d.index.year >= lo) & (d.index.year <= hi)]) for lo, hi in HALVES]
    g2 = all(np.isfinite(x) and x > 0 for x in hs)
    gr = (p["sharpe"] >= p0["sharpe"] and p["dd"] >= p0["dd"] and f < f0)
    cand[m] = dict(sig=s, d=d, p=p, flips=f, g1=g1, g2=g2, gr=gr, hs=hs, aw=aw)
    print(f"  {m:<10}{p['cum']:>+9.1f}%{p['sharpe']:>7.2f}{p['dd']:>8.1f}%{f:>7}{aw:>7.0%}"
          f"{sharpe(dc):>10.2f}{'过' if g1 else '否':>4}"
          f"{f'{hs[0]:+.2f}/{hs[1]:+.2f}':>15}{'过' if g2 else '否':>4}{'过' if gr else '否':>5}")

print(f"\n  G_R 三条硬指标(对现役 夏普 ≥{p0['sharpe']:.2f}、回撤 ≥{p0['dd']:.1f}%、翻转 <{f0}):")
for m, k in cand.items():
    print(f"    {m:<10}夏普 {k['p']['sharpe']:>5.2f} {'✓' if k['p']['sharpe'] >= p0['sharpe'] else '✗'}   "
          f"回撤 {k['p']['dd']:>6.1f}% {'✓' if k['p']['dd'] >= p0['dd'] else '✗'}   "
          f"翻转 {k['flips']:>3} {'✓' if k['flips'] < f0 else '✗'}")

# ---- G4 / G5 / G6 只跑还活着的候选 ----
alive = [m for m, k in cand.items() if k["g1"] and k["g2"] and k["gr"]]
print(f"\n[G4/G5/G6] 只对 G1+G2+G_R 都过的候选跑:{alive or '无 —— 全部出局'}")
for m in alive:
    k = cand[m]
    comb = (MAIN.reindex(IDX).fillna(0) * .5 + k["d"].reindex(IDX).fillna(0) * .5)
    k["g4"] = sharpe(comb) > sharpe(MAIN)
    k["g5"] = sharpe(run(k["sig"], fill="close")[0]) > 0
    vals, n, hits = k["sig"].values, len(IDX), 0
    for _ in range(PLACEBO_N):
        sft = int(rng.integers(PLACEBO_MIN, n - PLACEBO_MIN))
        if sharpe(run(pd.Series(np.roll(vals, sft), index=IDX))[0]) >= k["p"]["sharpe"]:
            hits += 1
    k["p_val"] = (hits + 1) / (PLACEBO_N + 1)
    k["g6"] = k["p_val"] < 0.05 / BONF
    print(f"  {m:<10}G4 组合 {sharpe(comb):.2f} vs {sharpe(MAIN):.2f} {'过' if k['g4'] else '否'} · "
          f"G5 {'过' if k['g5'] else '否'} · G6 p={k['p_val']:.4f} "
          f"(Bonferroni 需 <{0.05 / BONF:.4f}) {'过' if k['g6'] else '否'}")

# ---- Tier B 侦察 ----
print("\n[Tier B 侦察] 全部席位(≥400 行)—— **描述性,绝不判定**")
cnt = seat.groupby("member_key").size()
pool = [m for m in cnt[cnt >= 400].index if m not in TIER_A]
rows = []
for m in pool:
    s = main_net(m)
    if s.notna().sum() < 150:
        continue
    d, pos, w, f = run(s)
    p = perf(d)
    inpos = float((pos.reindex(IDX).fillna(0) != 0).mean())
    # 自身盈亏(可见口径推算):全合约净持仓 × 结算变动 × 点值
    sub = seat[seat["member_key"] == m]
    nv = sub.pivot_table(index="trade_date", values="net_off", aggfunc="sum")
    nv = nv.iloc[:, 0].reindex(IDX).ffill()
    pnl = float((nv.shift(1) * mkt["settle"].diff() * MULT).sum() / 1e8)
    rows.append((m, p["cum"], p["sharpe"], p["dd"], f, inpos, pnl))
rows.sort(key=lambda r: -r[2])
print(f"  池子 {len(rows)} 家。按夏普排序的前 8 家:")
print(f"    {'席位':<12}{'累计':>10}{'夏普':>7}{'回撤':>9}{'翻转':>7}{'在场':>7}{'自身盈亏':>11}")
for r in rows[:8]:
    print(f"    {r[0]:<12}{r[1]:>+9.1f}%{r[2]:>7.2f}{r[3]:>8.1f}%{r[4]:>7}{r[5]:>7.0%}{r[6]:>10.1f}亿")
print(f"  对照:现役华泰满仓 {p0['sharpe']:.2f} / 缩仓 {p1['sharpe']:.2f}")
sh = np.array([r[2] for r in rows])
print(f"  **选择偏差解剖**:{len(sh)} 家的夏普 中位 {np.median(sh):.2f}、"
      f"四分位 {np.percentile(sh, 25):.2f}~{np.percentile(sh, 75):.2f}、最高 {sh.max():.2f}")
print(f"  在 {len(sh)} 家里挑最高,等价于做了 {len(sh)} 次比较;"
      f"要压住它,单家需要的 p 值门槛是 {0.05 / len(sh):.5f}")

print("\n" + "=" * 96)
print(f"G0 {'过' if g0 else '不过'}")
print(f"Q1(华泰+缩仓):G1 {'过' if q1_g1 else '否'} · G2 {'过' if q1_g2 else '否'} · "
      f"G4 {'过' if q1_g4 else '否'} · G5 {'过' if q1_g5 else '否'}")
full = [m for m in alive if all(cand[m].get(x) for x in ("g4", "g5", "g6"))]
print(f"Q2(换人):Tier A 里六关全过的候选 = {full or '无'}")
print("处置按预注册第六节:有候选全过 → 交拍板换人(同时报 Q1 那格);"
      "无候选但 Q1 过 → 只加仓位不换人;两者都不过 → 维持现状关账。")
