# -*- coding: utf-8 -*-
"""PLAN_SA_COVER_TURN_v1 的跑数脚本 —— 大额减空是回补还是转向。

**先读 PLAN_SA_COVER_TURN_v1.md。** 定义、假设、闸门、结局处置都在那边事前钉死。

口径要点(与预注册逐条对应):
  · 席位组 国泰君安/东证/永安,`net_off`,逐日 多单−空单;
  · 事件 = 净持仓单日上升 ≥ 30,000 手 且 事件日仍净空;
  · 标签窗口 20 个交易日:曾回到 ≤ net[t−1] → 回补;始终没回到且 net[t+20] > net[t]
    → 转向;中间态**并入回补**(保守);
  · H1 = 事件后 3 日内没跌回 net[t−1] 且未创新低于 net[t]。

用法:CSV_DIR=research/data python research/run_sa_cover_turn.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import hog_money as H  # noqa: E402

DATA = Path(os.environ.get("CSV_DIR", "research/data"))
THREE = ["国泰君安", "东证期货", "永安期货"]
FIVE = THREE + ["瑞达期货", "华泰期货"]
LABEL_WIN = 20        # 标签窗口
CONFIRM = 3           # H1 确认窗口,预注册写死不许改
RNG = np.random.default_rng(20260905)

H.use("SA")
_price = H.clean_price(pd.read_csv(DATA / "sa_price.csv.gz"))
_raw = pd.read_csv(DATA / "sa_seat.csv.gz")
_mkt = H.main_series(_price)
IDX = _mkt.index[_mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
PX = _mkt["settle"].reindex(IDX)


def legs(members):
    r = _raw.copy()
    r["trade_date"] = pd.to_datetime(r["trade_date"])
    r["member_key"] = r["member"].astype(str).str.split("（").str[0]
    r = r[r.member_key.isin(members) & r.rank_type.isin(["long", "short"])]
    r = r[~r["is_variety_total"].astype(str).str.lower().isin(["t", "true", "1"])]
    w = (r.pivot_table(index="trade_date", columns="rank_type",
                       values="quantity", aggfunc="sum")
         .reindex(columns=["long", "short"]).reindex(IDX))
    return w["long"], w["short"]


def events(members, thresh=30000):
    """返回逐事件的字典列表,含标签与 H1 条件。"""
    L, S = legs(members)
    net = (L - S)
    dN, dL = net.diff(), L.diff()
    out = []
    arr = net.to_numpy(dtype=float)
    for i in range(1, len(IDX) - LABEL_WIN):
        if not (np.isfinite(dN.iloc[i]) and dN.iloc[i] >= thresh):
            continue
        if not (np.isfinite(arr[i]) and arr[i] < 0):
            continue
        n0, nt = arr[i - 1], arr[i]
        win = arr[i + 1:i + 1 + LABEL_WIN]
        if not np.isfinite(win).all():
            continue
        came_back = bool((win <= n0).any())
        turn = (not came_back) and (win[-1] > nt)
        conf = win[:CONFIRM]
        h1 = bool((conf > n0).all() and (conf >= nt).all())
        out.append({
            "日期": IDX[i].date(), "i": i, "转向": turn, "H1": h1,
            "净变化": float(dN.iloc[i]), "多单变化": float(dL.iloc[i]),
            "加多占比": float(dL.iloc[i] / dN.iloc[i]) if dN.iloc[i] else np.nan,
            "当日涨跌": float(PX.iloc[i] / PX.iloc[i - 1] - 1) * 100
            if np.isfinite(PX.iloc[i]) and np.isfinite(PX.iloc[i - 1]) else np.nan,
            "事前百分位": float((arr[:i] < nt).mean() * 100),
        })
    return pd.DataFrame(out)


def fisher(a, b, c, d):
    """2x2 Fisher 精确检验(双尾),不引第三方库。"""
    from math import comb
    n = a + b + c + d
    r1, c1 = a + b, a + c
    def p(x):
        return comb(r1, x) * comb(n - r1, c1 - x) / comb(n, c1)
    lo = max(0, c1 - (n - r1))
    hi = min(r1, c1)
    p0 = p(a)
    return sum(p(x) for x in range(lo, hi + 1) if p(x) <= p0 + 1e-12)


def rate(df, mask=None):
    sub = df if mask is None else df[mask]
    return (sub["转向"].mean() * 100 if len(sub) else np.nan), len(sub)


def g1(df, tag="", verbose=True):
    base, n_all = rate(df)
    hit, n_hit = rate(df, df["H1"])
    miss, n_miss = rate(df, ~df["H1"])
    a = int(df[df["H1"]]["转向"].sum())
    b = n_hit - a
    c = int(df[~df["H1"]]["转向"].sum())
    d = n_miss - c
    p = fisher(a, b, c, d) if min(n_hit, n_miss) else np.nan
    if verbose:
        print(f"  {tag}事件 {n_all} 次,基准转向率 {base:.0f}%")
        print(f"    H1 成立 {n_hit:>3} 次 → 转向率 {hit:.0f}%")
        print(f"    H1 不成立 {n_miss:>3} 次 → 转向率 {miss:.0f}%")
        print(f"    绝对提升 {hit - base:+.0f} pp(对基准)/ "
              f"{hit - miss:+.0f} pp(对 H1 不成立组)   Fisher p = {p:.4f}")
    return {"base": base, "hit": hit, "n_all": n_all, "n_hit": n_hit,
            "lift": hit - base, "p": p}


print("=== 主口径:三家(国泰君安/东证/永安),门槛 30,000 手 ===")
df = events(THREE)
r = g1(df)

print(f"\n【G1 判别力】需 Fisher p<0.05 且绝对提升 ≥15pp")
g1_ok = (r["p"] < 0.05) and (r["lift"] >= 15)
print(f"  p={r['p']:.4f}  提升={r['lift']:+.0f}pp  → {'过' if g1_ok else '不过'}")

print(f"\n【G2 样本量】事件 ≥40 且 H1 组 ≥15")
g2_ok = r["n_all"] >= 40 and r["n_hit"] >= 15
print(f"  事件 {r['n_all']}  H1 组 {r['n_hit']}  → {'过' if g2_ok else '不过'}")

print("\n【G3 逐年不塌】有事件的年份里,H1 组转向率 > 基准 的年份需 ≥2/3")
df["年"] = pd.to_datetime(df["日期"]).dt.year
ok_y = tot_y = 0
for y, sub in df.groupby("年"):
    if len(sub) < 3:
        continue
    b, _ = rate(sub)
    h, nh = rate(sub, sub["H1"])
    tot_y += 1
    good = np.isfinite(h) and h > b
    ok_y += bool(good)
    print(f"  {y}  事件 {len(sub):>2}  基准 {b:.0f}%  H1组 "
          f"{'—' if not np.isfinite(h) else f'{h:.0f}%'}(n={nh})  "
          f"{'✓' if good else '✗'}")
g3_ok = tot_y > 0 and ok_y / tot_y >= 2 / 3
print(f"  {ok_y}/{tot_y}  → {'过' if g3_ok else '不过'}")

print("\n【G4 门槛稳健】20,000 / 40,000 手下符号不许翻(p 可放宽到 0.10)")
g4_ok = True
for th in (20000, 40000):
    rr = g1(events(THREE, th), tag=f"门槛 {th:,}:")
    same = np.sign(rr["lift"]) == np.sign(r["lift"]) and rr["lift"] > 0
    g4_ok = g4_ok and same and rr["p"] < 0.10
    print(f"    → {'同向且显著' if (same and rr['p'] < 0.10) else '不过'}")

print("\n【G5 安慰剂】把转向标签随机重排 500 次")
obs = r["hit"] - r["base"]
lab = df["转向"].to_numpy()
h1m = df["H1"].to_numpy()
draws = []
for _ in range(500):
    sh = RNG.permutation(lab)
    if h1m.sum() == 0:
        continue
    draws.append(sh[h1m].mean() * 100 - sh.mean() * 100)
draws = np.array(draws)
p5 = float((draws >= obs).mean())
print(f"  实测提升 {obs:+.1f}pp;随机重排里 ≥ 它的比例 p = {p5:.3f}  "
      f"→ {'过' if p5 < 0.05 else '不过'}")

print("\n【G6 五家版同向】三家是运营者事后指定的,查是不是靠剔人剔出来的")
r5 = g1(events(FIVE), tag="五家:")
g6_ok = np.sign(r5["lift"]) == np.sign(r["lift"])
print(f"  → {'符号同向,过' if g6_ok else '符号翻了,不过'}")

print("\n" + "=" * 68)
print("【H2 探索性(不作上线依据,Bonferroni α=0.0125)】")
for name, key, cut in (("加多占比", "加多占比", None),
                       ("当日涨跌", "当日涨跌", 0.0),
                       ("事前百分位", "事前百分位", None),
                       ("事件规模", "净变化", None)):
    v = df[key]
    thr = cut if cut is not None else v.median()
    hi = df[v > thr]
    lo = df[v <= thr]
    if not len(hi) or not len(lo):
        continue
    a, b = int(hi["转向"].sum()), len(hi) - int(hi["转向"].sum())
    c, d = int(lo["转向"].sum()), len(lo) - int(lo["转向"].sum())
    p = fisher(a, b, c, d)
    print(f"  {name:<8} 高组 {a}/{len(hi)} = {a/len(hi)*100:>3.0f}%   "
          f"低组 {c}/{len(lo)} = {c/len(lo)*100:>3.0f}%   p={p:.4f}   "
          f"{'过校正' if p < 0.0125 else '不过'}")

print("\n判定按 PLAN_SA_COVER_TURN_v1 第五节执行,本脚本不下结论。")
