# -*- coding: utf-8 -*-
"""玻璃 / 纯碱 四家聪明钱的第二引擎完整回测(预注册 PLAN_FGSA_FOLLOW4_v1)。

4 家(海通/永安/东证/国泰君安)× 2 品种 = 8 格。**玻璃×永安是现役,作校准格**。
规则照抄线上:主力合约净持仓方向 + DEC-234 的 200 日分数仓位,一个旋钮不新增。

运营者的质疑(「有夜盘,当晚 21:00 就能进,不必等次日早盘」)对应 G5:
换 T+1 收盘成交重跑,看是不是全靠跳空。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
MEMBERS = ("海通期货", "永安期货", "东证期货", "国泰君安")
FEE = 2.0
PLACEBO_N = 1000
PLACEBO_MIN = 60
rng = np.random.default_rng(20260906)


def build(code: str, stem: str) -> dict:
    H.use(code)
    price = H.clean_price(pd.read_csv(D / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{stem}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    idx = mkt.index
    g, log, cuts = H.rolling_groups(seat, price, idx)
    if H.RULES.get("group_overrides"):
        g, log = H.apply_group_overrides(g, log, cuts, H.RULES["group_overrides"], seat, price)
    if H.RULES.get("freeze_since"):
        g, log, cuts = H.freeze_groups(g, log, cuts, H.RULES["freeze_since"])
    rdf, _ = H.retail_series(seat, idx)
    raw = H.signal_series(seat, g)
    mkt = mkt.assign(ret_settle=mkt["settle"].pct_change())
    return dict(code=code, seat=seat, mkt=mkt, idx=idx, g=g, raw=raw, rdf=rdf, op=op, st=st)


def main_net(C: dict, member: str) -> pd.Series:
    """该席位在**当日主力合约**上的可见净持仓,按合约 ffill —— 与线上逐字同法。"""
    sub = C["seat"][C["seat"]["member_key"] == member]
    sig = pd.Series(np.nan, index=C["idx"])
    for c in dict.fromkeys(C["mkt"]["main"]):
        if not isinstance(c, str):
            continue
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = C["idx"][C["mkt"]["main"] == c]
        sig.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
    return sig


def daily_of(C: dict, sig: pd.Series, code: str, *, fill="open", weight=True, const=None):
    """按线上口径算净值。fill: open=T+1 开盘(现行) / settle_t=T 结算(上界) / close=T+1 收盘。"""
    H.use(code)
    mult = float(H.RULES["multiplier"])
    mkt = C["mkt"]
    pos = np.sign(sig)
    pos[pos == 0] = np.nan
    pos = pos.ffill()
    if const is not None:
        w = pd.Series(float(const), index=C["idx"])
    elif weight:
        w = (sig.abs() / H.rolling_top_mean(sig, win=H.FOLLOW_SIZING_WIN,
                                            min_periods=60)).clip(upper=1.0)
    else:
        w = pd.Series(1.0, index=C["idx"])
    w = w.reindex(C["idx"]).fillna(0.0).clip(0.0, 1.0)
    lag = 1 if fill == "settle_t" else 2
    ret = mkt["ret_open"] if fill == "open" else mkt["ret_settle"]
    turn = (pos.shift(lag) != pos.shift(lag + 1)).astype(float)
    wl = w.shift(lag).fillna(0.0)
    expo = (pos * w).fillna(0.0)
    extra = expo.diff().abs().fillna(expo.abs()).shift(lag).fillna(0.0)
    d = (pos.shift(lag) * ret * wl - turn * 0.001 * wl
         - extra * FEE / (mkt["settle"].ffill() * mult))
    return d.dropna(), pos, w


def perf(d: pd.Series) -> dict:
    eq = (1 + d).cumprod()
    return {"cum": (float(eq.iloc[-1]) - 1) * 100,
            "sharpe": float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else float("nan"),
            "dd": float((eq / eq.cummax() - 1).min()) * 100}


def sharpe(d: pd.Series) -> float:
    d = d.dropna()
    return float(d.mean() / d.std() * np.sqrt(242)) if len(d) > 2 and d.std() > 0 else float("nan")


def main_engine_daily(C: dict, code: str) -> pd.Series:
    H.use(code)
    sig = C["raw"]
    if H.RULES["signal_source"] == "cost":
        sig = H.attach_cost_signal(sig, C["seat"], C["mkt"], C["g"])
    tr, pos, dl = H.replay(sig, C["mkt"], C["rdf"], C["op"], C["st"])
    if H.RULES.get("sizing"):
        w = H.sizing_weights(C["raw"]["net"]).reindex(C["idx"])
        dl = H.apply_sizing(dl, pos, w, C["mkt"]["settle"], H.RULES["multiplier"])
    return dl


FG = build("FG", "fg")
SA = build("SA", "sa")
MAIN = {"FG": main_engine_daily(FG, "FG"), "SA": main_engine_daily(SA, "SA")}
HALVES = {"FG": ((2013, 2019), (2020, 2026)), "SA": ((2020, 2022), (2023, 2026))}

print("=" * 96)
print("玻璃 / 纯碱 四家聪明钱第二引擎(PLAN_FGSA_FOLLOW4_v1)")
print(f"玻璃 {FG['idx'][0].date()}~{FG['idx'][-1].date()} · 纯碱 {SA['idx'][0].date()}~{SA['idx'][-1].date()}")

cells = {}
for code, C, name in (("FG", FG, "玻璃"), ("SA", SA, "纯碱")):
    print(f"\n{'=' * 96}\n=== {name} {code} ===")
    print(f"  {'席位':<10}{'累计':>10}{'夏普':>7}{'回撤':>9}{'平均敞口':>10}"
          f"{'等效缩仓夏普':>14}{'G1':>5}{'半样本夏普':>18}{'G2':>5}")
    for m in MEMBERS:
        sig = main_net(C, m)
        if sig.notna().sum() < 200:
            print(f"  {m:<10}样本不足,跳过")
            continue
        d, pos, w = daily_of(C, sig, code)
        p = perf(d)
        held = (pos.abs() > 0).reindex(d.index).fillna(False)
        avg_w = float((w.shift(2).reindex(d.index).fillna(0))[held].mean())
        dc, _, _ = daily_of(C, sig, code, const=avg_w)
        g1 = p["sharpe"] >= sharpe(dc)
        hs = []
        for lo, hi in HALVES[code]:
            seg = d[(d.index.year >= lo) & (d.index.year <= hi)]
            hs.append(sharpe(seg))
        g2 = all(np.isfinite(x) and x > 0 for x in hs)
        cells[(code, m)] = dict(d=d, sig=sig, p=p, g1=g1, g2=g2, hs=hs, avg_w=avg_w,
                                const_sharpe=sharpe(dc))
        print(f"  {m:<10}{p['cum']:>+9.1f}%{p['sharpe']:>7.2f}{p['dd']:>8.1f}%{avg_w:>10.0%}"
              f"{sharpe(dc):>14.2f}{'过' if g1 else '否':>5}"
              f"{f'{hs[0]:+.2f} / {hs[1]:+.2f}':>18}{'过' if g2 else '否':>5}")

# ---- G0 校准 ----
c0 = cells[("FG", "永安期货")]["p"]
g0 = (abs(c0["cum"] - 319.0) < 0.06 and abs(c0["sharpe"] - 0.81) < 0.006
      and abs(c0["dd"] - (-26.7)) < 0.06)
print(f"\n[G0 校准] 玻璃×永安 = {c0['cum']:+.1f}% / {c0['sharpe']:.2f} / {c0['dd']:.1f}%"
      f"(线上 +319.0 / 0.81 / −26.7)→ {'**过**' if g0 else '**不过 —— 项目作废**'}")

# ---- G4 组合增益 ----
print("\n[G4 组合增益] 与本品种主引擎 50/50 —— 必须 > 主引擎单跑")
for code in ("FG", "SA"):
    base = sharpe(MAIN[code])
    print(f"  {code} 主引擎单跑 夏普 {base:.2f}")
    for m in MEMBERS:
        k = cells.get((code, m))
        if not k:
            continue
        idx = FG["idx"] if code == "FG" else SA["idx"]
        comb = (MAIN[code].reindex(idx).fillna(0) * 0.5 + k["d"].reindex(idx).fillna(0) * 0.5)
        k["g4"] = sharpe(comb) > base
        print(f"    + {m:<10}组合 {sharpe(comb):>5.2f}   {'过' if k['g4'] else '否'}")

# ---- G5 成交时点 ----
print("\n[G5 成交时点] 三种口径(现行 T+1 开盘 / T 结算=上界 / T+1 收盘=下界)")
print(f"  {'格':<16}{'T 结算(上界)':>16}{'T+1 开盘(现行)':>18}{'T+1 收盘(下界)':>18}{'G5':>5}")
for code, C in (("FG", FG), ("SA", SA)):
    for m in MEMBERS:
        k = cells.get((code, m))
        if not k:
            continue
        s_up = sharpe(daily_of(C, k["sig"], code, fill="settle_t")[0])
        s_now = k["p"]["sharpe"]
        s_dn = sharpe(daily_of(C, k["sig"], code, fill="close")[0])
        k["g5"] = s_dn > 0
        k["s_up"], k["s_dn"] = s_up, s_dn
        print(f"  {code}×{m:<12}{s_up:>14.2f}{s_now:>18.2f}{s_dn:>18.2f}"
              f"{('过' if k['g5'] else '否'):>5}")

# ---- G6 安慰剂 ----
print(f"\n[G6 安慰剂] 席位序列整体随机平移(≥{PLACEBO_MIN} 日,{PLACEBO_N} 次)")
for code, C in (("FG", FG), ("SA", SA)):
    n = len(C["idx"])
    for m in MEMBERS:
        k = cells.get((code, m))
        if not k:
            continue
        vals = k["sig"].values
        hits = 0
        for _ in range(PLACEBO_N):
            s = int(rng.integers(PLACEBO_MIN, n - PLACEBO_MIN))
            fake = pd.Series(np.roll(vals, s), index=C["idx"])
            if sharpe(daily_of(C, fake, code)[0]) >= k["p"]["sharpe"]:
                hits += 1
        k["p_val"] = (hits + 1) / (PLACEBO_N + 1)
        k["g6"] = k["p_val"] < 0.05
        print(f"  {code}×{m:<12}p = {k['p_val']:.3f}   {'过' if k['g6'] else '否'}")

# ---- 汇总 ----
print("\n" + "=" * 96)
print(f"  {'格':<18}{'G1':>5}{'G2':>5}{'G4':>5}{'G5':>5}{'G6':>5}{'全过':>7}")
passed = []
for code in ("FG", "SA"):
    for m in MEMBERS:
        k = cells.get((code, m))
        if not k:
            continue
        allp = all(k.get(x) for x in ("g1", "g2", "g4", "g5", "g6"))
        role = "(现役·校准格)" if (code, m) == ("FG", "永安期货") else ""
        if allp and (code, m) != ("FG", "永安期货"):
            passed.append((code, m))
        print(f"  {code}×{m:<14}{'过' if k['g1'] else '否':>5}{'过' if k['g2'] else '否':>5}"
              f"{'过' if k.get('g4') else '否':>5}{'过' if k.get('g5') else '否':>5}"
              f"{'过' if k.get('g6') else '否':>5}{'**全过**' if allp else '':>7} {role}")
print(f"\n[G3 防 8 选 1] 7 个候选格里全过的有 **{len(passed)}** 格:{passed or '无'}"
      f" → {'**过**' if len(passed) >= 2 else '**不过**'}")
print(f"G0 {'过' if g0 else '不过'} · G3 {'过' if len(passed) >= 2 else '不过'}")
print("处置按预注册第六节:≥2 格全过 → 交拍板;1 格 → 8 选 1 的尖峰,不上;0 格 → 关账。")
