# -*- coding: utf-8 -*-
"""PLAN_SA_INST3_v1 的跑数脚本 —— 机构席位组改成固定三家。

**先读 PLAN_SA_INST3_v1.md。**

三条臂:基线(滚动 5 家,生产现状)/ 臂1(固定现 5 家)/ 臂2(固定 国泰君安/永安/东证)。
臂1 存在的唯一理由:把「固定名单」本身的代价从「换谁」里剥出来。

用法:CSV_DIR=research/data python research/run_sa_inst3.py
"""
from __future__ import annotations

import itertools
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import hog_money as H  # noqa: E402

DATA = Path(os.environ.get("CSV_DIR", "research/data"))
TRIO = ["国泰君安", "永安期货", "东证期货"]     # 运营者点名,不许改
POOL_N = 20                                    # 池:上榜天数前 20 家
N_COMBO = 200                                  # 随机三家组合数,预注册写死
RNG = np.random.default_rng(20260906)

C = {}
for code, stem in (("SA", "sa"), ("FG", "fg")):
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
    rdf, _h = H.retail_series(seat, mkt.index)
    days = seat.groupby("member_key")["trade_date"].nunique().sort_values(ascending=False)
    pool = list(days.head(POOL_N).index)
    cur5 = list(groups.dropna().iloc[-1])
    C[code] = {"price": price, "seat": seat, "mkt": mkt, "op": op, "st": st,
               "roll": groups, "rdf": rdf, "pool": pool, "cur5": cur5,
               "decided": mkt.index[-1].strftime("%Y-%m-%d")}
    print(f"{code} 预处理完成({len(mkt)} 天);当前 5 家 = {'/'.join(cur5)}")
    print(f"    池(上榜天数前 {POOL_N}):{'、'.join(pool)}")
    miss = [m for m in TRIO if m not in set(seat['member_key'])]
    assert not miss, f"{code} 数据里没有 {miss}"


def run(code, members=None):
    """members=None → 基线(滚动组);否则固定名单。"""
    c = C[code]
    H.use(code)
    if members is None:
        groups = c["roll"]
    else:
        groups, _log, _cuts = H.fixed_groups(list(members), c["seat"], c["price"],
                                             c["mkt"].index, c["decided"])
    sig = H.signal_series(c["seat"], groups)
    sig = H.attach_cost_signal(sig, c["seat"], c["mkt"], groups)
    trades, _pos, daily = H.replay(sig, c["mkt"], c["rdf"], c["op"], c["st"])
    p = H._perf(daily)
    return {"累计%": p["cum_pct"], "夏普": p["sharpe"] or 0.0,
            "回撤%": p["max_dd_pct"], "笔数": len(trades), "daily": daily}


def show(tag, r):
    return (f"  {tag:<24}{r['累计%']:>+10.1f}%{r['夏普']:>8.2f}"
            f"{r['回撤%']:>9.1f}%{r['笔数']:>7}")


RES = {}
for code, name in (("SA", "纯碱"), ("FG", "玻璃")):
    c = C[code]
    print(f"\n{'='*80}\n=== {name} {code} ===")
    t0 = time.time()
    base = run(code)
    per = time.time() - t0
    a1 = run(code, c["cur5"])
    a2 = run(code, TRIO)
    print(f"  {'臂':<22}{'累计':>11}{'夏普':>8}{'回撤':>10}{'笔数':>7}")
    print(show("基线(滚动 5 家)", base))
    print(show("臂1 固定现 5 家", a1))
    print(show("臂2 固定三家", a2))
    print(f"  (单次回测约 {per:.1f} 秒)")
    RES[code] = {"base": base, "a1": a1, "a2": a2}

    print(f"\n  —— 两个差分必须分开看(预注册第一节)——")
    print(f"    「固定」本身    臂1 − 基线:夏普 {a1['夏普']-base['夏普']:+.3f}  "
          f"累计 {a1['累计%']-base['累计%']:+.1f}pp  回撤 {a1['回撤%']-base['回撤%']:+.1f}pp")
    print(f"    「只留这三家」  臂2 − 臂1 :夏普 {a2['夏普']-a1['夏普']:+.3f}  "
          f"累计 {a2['累计%']-a1['累计%']:+.1f}pp  回撤 {a2['回撤%']-a1['回撤%']:+.1f}pp")
    print(f"    合计            臂2 − 基线:夏普 {a2['夏普']-base['夏普']:+.3f}  "
          f"累计 {a2['累计%']-base['累计%']:+.1f}pp  回撤 {a2['回撤%']-base['回撤%']:+.1f}pp")

    print(f"\n  【G1 人选特别】池内随机 {N_COMBO} 个三家组合,需排进前 25%")
    allc = [t for t in itertools.combinations(sorted(c["pool"]), 3)]
    idx = RNG.choice(len(allc), size=min(N_COMBO, len(allc)), replace=False)
    sharps, rows = [], []
    for j, i in enumerate(idx):
        r = run(code, list(allc[i]))
        sharps.append(r["夏普"])
        rows.append((allc[i], r))
    sharps = np.array(sharps)
    pct = float((sharps < a2["夏普"]).mean())
    print(f"    三家夏普 {a2['夏普']:.2f};{N_COMBO} 个随机组合中位 {np.median(sharps):.2f}、"
          f"最好 {sharps.max():.2f}")
    print(f"    分位 {pct:.0%}(需 ≥75%)→ {'过' if pct >= 0.75 else '不过'}")
    top = sorted(rows, key=lambda x: -x[1]["夏普"])[:5]
    print(f"    池内最好的 5 组(**仅供诊断,预注册第六节禁止改用它们**):")
    for mem, r in top:
        print(f"      {'/'.join(mem):<26}夏普 {r['夏普']:>5.2f}  累计 {r['累计%']:>+7.1f}%  "
              f"回撤 {r['回撤%']:>6.1f}%")
    RES[code]["pct"] = pct

    print(f"\n  【G2 不是「固定」在起作用】臂2 夏普需 ≥ 臂1")
    print(f"    {a2['夏普']:.2f} vs {a1['夏普']:.2f} → {'过' if a2['夏普'] >= a1['夏普'] else '不过'}")

    print(f"\n  【G4 回撤】臂2 回撤需 ≤ 基线(数值上不更负)")
    print(f"    {a2['回撤%']:.1f}% vs {base['回撤%']:.1f}% → "
          f"{'过' if a2['回撤%'] >= base['回撤%'] else '不过'}")

    print(f"\n  【G5 逐年】臂2 ≥ 基线的年份需 ≥ 4/6 比例")
    d, db = a2["daily"].fillna(0), base["daily"].fillna(0)
    yb = ((1 + db).groupby(db.index.year).prod() - 1) * 100
    yr = ((1 + d).groupby(d.index.year).prod() - 1) * 100
    win = 0
    for y in yb.index:
        ok = yr.get(y, -1e9) >= yb[y]
        win += ok
        print(f"    {y}  基线 {yb[y]:+7.1f}%   三家 {yr.get(y, np.nan):+7.1f}%   {'✓' if ok else '✗'}")
    print(f"    {win}/{len(yb)} = {win/len(yb)*100:.0f}%  "
          f"{'过' if win/len(yb) >= 4/6 else '不过'}")

print(f"\n=== G3 两品种同向(臂2 − 基线 的夏普变化符号)===")
dd = {k: RES[k]["a2"]["夏普"] - RES[k]["base"]["夏普"] for k in ("SA", "FG")}
print(f"  纯碱 {dd['SA']:+.3f}   玻璃 {dd['FG']:+.3f}   "
      f"→ {'同向,过' if np.sign(dd['SA']) == np.sign(dd['FG']) else '符号相反,不过'}")

print("\n判定按 PLAN_SA_INST3_v1 第五节执行,本脚本不下结论。"
      "\n提醒:固定名单是拿今天的认知挑的,放回早年并不灵 —— 报告必须照写。")
