# -*- coding: utf-8 -*-
"""PLAN_RETAIL_RESELECT_v1(v1.1)的跑数脚本 —— 只为玻璃与纯碱重选散户反向名单。

**先读 PLAN_RETAIL_RESELECT_v1.md。** 口径、闸门、结局处置都在那边事前钉死。

要点:
  · 选人窗口 2021-01-01 ~ 2024-12-31;样本外 2025-01-01 起,挑人时绝不许看;
  · 判据照抄旧规则三条:alpha 后 20%(百分位>80)、净多占比>50%、日均|净持仓|≥2000;
  · H1 逐品种各选前 3(主口径);H2 玻纯共用一份(对照);
  · 评估 = 把名单装进 retail_seed,在样本外窗口上跑完整引擎回放。

用法:CSV_DIR=research/data python research/run_retail_reselect.py
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import hog_money as H  # noqa: E402

DATA = Path(os.environ.get("CSV_DIR", "research/data"))
CUR = ["东方财富", "平安期货", "徽商期货"]
PICK_END = pd.Timestamp("2025-01-01")     # 选人窗口的右端(不含)
OOS = pd.Timestamp("2025-01-01")          # 样本外起点
RNG = random.Random(20260905)

C = {}
for code, stem in (("FG", "fg"), ("SA", "sa")):
    H.use(code)
    price = H.clean_price(pd.read_csv(DATA / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(DATA / f"{stem}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        groups, log = H.apply_group_overrides(groups, log, cuts,
                                              H.RULES["group_overrides"], seat, price)
    if H.RULES.get("freeze_since"):
        groups, log, cuts = H.freeze_groups(groups, log, cuts, H.RULES["freeze_since"])
    sig = H.signal_series(seat, groups)
    if H.RULES["signal_source"] == "cost":
        sig = H.attach_cost_signal(sig, seat, mkt, groups)
    C[code] = {"price": price, "seat": seat, "mkt": mkt, "op": op, "st": st, "sig": sig}
    print(f"{code} 预处理完成,{len(mkt)} 天")


def screen(code, lo=None, hi=PICK_END):
    """在 [lo, hi) 窗口内算三条判据,返回 DataFrame(按 alpha 百分位降序)。"""
    c = C[code]
    H.use(code)
    seat, price = c["seat"], c["price"]
    idx = c["mkt"].index
    idx = idx[(idx < hi)] if lo is None else idx[(idx >= lo) & (idx < hi)]
    a = H.alpha_upto(seat, price, hi, lo=lo)
    if not len(a):
        return pd.DataFrame()
    n = len(a)
    bd = seat.groupby(["member_key", "trade_date"])["net"].sum()
    rows = []
    for m in a.index:
        if m not in bd.index.get_level_values(0):
            continue
        s = bd.loc[m]
        s = s[s.index.isin(idx)]
        if not len(s):
            continue
        rows.append({
            "席位": m,
            "alpha百分位": (int(a.index.get_loc(m)) + 1) / n * 100,
            "净多占比": float((s > 0).mean() * 100),
            "日均持仓": float(s.abs().mean()),
            "上榜率": len(s) / len(idx) * 100,
        })
    df = pd.DataFrame(rows)
    df["合格"] = ((df["alpha百分位"] > 80) & (df["净多占比"] > 50)
                  & (df["日均持仓"] >= 2000) & (df["上榜率"] >= 60))
    return df.sort_values("alpha百分位", ascending=False).reset_index(drop=True)


def oos(seed, code):
    """把 seed 装进 retail_seed,只看样本外窗口的表现。"""
    c = C[code]
    H.use(code)
    H.RULES["retail_seed"] = list(seed)
    rdf, _h = H.retail_series(c["seat"], c["mkt"].index)
    _t, _p, daily = H.replay(c["sig"], c["mkt"], rdf, c["op"], c["st"])
    d = daily[daily.index >= OOS].fillna(0)
    eq = (1 + d).cumprod()
    sh = float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan
    return {"累计%": round((eq.iloc[-1] - 1) * 100, 1), "夏普": round(sh, 2),
            "回撤%": round(float((eq / eq.cummax() - 1).min()) * 100, 1), "daily": d}


print(f"\n=== 选人窗口 2021-01-01 ~ 2024-12-31;样本外 {OOS.date()} 起 ===\n")
SCR, PICK = {}, {}
for code, name in (("FG", "玻璃"), ("SA", "纯碱")):
    df = screen(code, lo=pd.Timestamp("2021-01-01"))
    SCR[code] = df
    ok = df[df["合格"]]
    PICK[code] = list(ok["席位"].head(3))
    print(f"--- {name} {code}:合格 {len(ok)} 家,取前 3 ---")
    print(df.head(10).round(1).to_string(index=False))
    print(f"  → 选中:{'、'.join(PICK[code]) if len(PICK[code])==3 else '不足 3 家:'+str(PICK[code])}")
    for m in ("中信建投",) + tuple(CUR):
        r = df[df["席位"] == m]
        if len(r):
            r = r.iloc[0]
            print(f"    {m}: alpha {r['alpha百分位']:.0f}%  净多 {r['净多占比']:.0f}%  "
                  f"日均 {r['日均持仓']:,.0f}  合格={bool(r['合格'])}")
    print()

# 共用版(H2)
both = set(SCR["FG"][SCR["FG"]["合格"]]["席位"]) & set(SCR["SA"][SCR["SA"]["合格"]]["席位"])
sc = {m: (float(SCR["FG"].set_index("席位").loc[m, "alpha百分位"])
          + float(SCR["SA"].set_index("席位").loc[m, "alpha百分位"])) for m in both}
SHARED = sorted(sc, key=sc.get, reverse=True)[:3]
print(f"共用版(H2):两品种都合格 {len(both)} 家 → 选中 {'、'.join(SHARED) if len(SHARED)==3 else SHARED}\n")

print("=== 样本外表现 ===")
print(f"{'方案':<26}{'玻璃累计':>10}{'玻璃夏普':>10}{'玻璃回撤':>10}"
      f"{'纯碱累计':>10}{'纯碱夏普':>10}{'纯碱回撤':>10}")
RES = {}
plans = [("现行三家", {"FG": CUR, "SA": CUR}),
         ("H1 逐品种", {"FG": PICK["FG"], "SA": PICK["SA"]})]
if len(SHARED) == 3:
    plans.append(("H2 玻纯共用", {"FG": SHARED, "SA": SHARED}))
for tag, seeds in plans:
    a, b = oos(seeds["FG"], "FG"), oos(seeds["SA"], "SA")
    RES[tag] = (a, b)
    print(f"{tag:<24}{a['累计%']:>+10.1f}{a['夏普']:>10}{a['回撤%']:>10}"
          f"{b['累计%']:>+10.1f}{b['夏普']:>10}{b['回撤%']:>10}")

cur_a, cur_b = RES["现行三家"]
new_a, new_b = RES["H1 逐品种"]
print("\n【G1 样本外胜出】两品种夏普都 ≥ 现行,且至少一个累计高 ≥10pp")
g1 = (new_a["夏普"] >= cur_a["夏普"] and new_b["夏普"] >= cur_b["夏普"]
      and max(new_a["累计%"] - cur_a["累计%"], new_b["累计%"] - cur_b["累计%"]) >= 10)
print(f"  玻璃夏普 {new_a['夏普']} vs {cur_a['夏普']}   纯碱夏普 {new_b['夏普']} vs {cur_b['夏普']}")
print(f"  累计差 玻璃 {new_a['累计%']-cur_a['累计%']:+.1f}pp  "
      f"纯碱 {new_b['累计%']-cur_b['累计%']:+.1f}pp  → {'过' if g1 else '不过'}")

print("\n【G2 安慰剂】各品种随机抽三家 200 次,新名单需在该品种前 10%")
for code, name in (("FG", "玻璃"), ("SA", "纯碱")):
    pool = list(SCR[code][SCR[code]["上榜率"] >= 60]["席位"])
    draws = []
    for _ in range(200):
        draws.append(oos(RNG.sample(pool, 3), code)["夏普"])
    draws = np.array([d for d in draws if np.isfinite(d)])
    mine = (new_a if code == "FG" else new_b)["夏普"]
    pct = (draws < mine).mean() * 100
    print(f"  {name}:池 {len(pool)} 家,新名单夏普 {mine} → 强于 {pct:.0f}% 的随机组合"
          f"  {'过' if pct >= 90 else '不过'}")

print("\n【G3 逐年(样本外)】2025 / 2026,新名单 ≥ 现行需 2/2")
for code, name, cur_r, new_r in (("FG", "玻璃", cur_a, new_a), ("SA", "纯碱", cur_b, new_b)):
    for y in (2025, 2026):
        c1 = cur_r["daily"]; c2 = new_r["daily"]
        v1 = (1 + c1[c1.index.year == y]).prod() - 1
        v2 = (1 + c2[c2.index.year == y]).prod() - 1
        print(f"  {name} {y}: 现行 {v1*100:+.1f}%  新 {v2*100:+.1f}%  "
              f"{'✓' if v2 >= v1 else '✗'}")

print("\n【G4 选人窗口稳健】2021~2023 / 2021~2025 选出的三家,至少两家不变")
for code, name in (("FG", "玻璃"), ("SA", "纯碱")):
    base = set(PICK[code])
    for end, lab in ((pd.Timestamp("2024-01-01"), "2021~2023"),
                     (pd.Timestamp("2026-01-01"), "2021~2025")):
        d2 = screen(code, lo=pd.Timestamp("2021-01-01"), hi=end)
        p2 = list(d2[d2["合格"]]["席位"].head(3))
        keep = len(base & set(p2))
        print(f"  {name} {lab}: {'、'.join(p2)}  与主口径重合 {keep}/3 "
              f"{'✓' if keep >= 2 else '✗'}")

print("\n判定按 PLAN_RETAIL_RESELECT_v1 第五节执行,本脚本不下结论。")
