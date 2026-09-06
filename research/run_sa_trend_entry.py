# -*- coding: utf-8 -*-
"""PLAN_SA_TREND_ENTRY_v1 的跑数脚本。**先读 PLAN_SA_TREND_ENTRY_v1.md。**

不改引擎:
  A 卸仓分母换 30 日 → 自己算一条 `unload30`,喂给**生产的** `cost_entry_frame`;
  B 新增进场门     → 不满足的日子把 `cost_z` 置 nan;
  C 只看近两年     → 序列全历史算(要预热),**回放之后**才截断;
  D 跟随永安       → 日收益 × 永安水位.shift(1)。

用法:CSV_DIR=research/data python research/run_sa_trend_entry.py
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
SINCE = pd.Timestamp("2024-09-01")
UWIN, BIG, MIN_ON = 30, 150_000, 3          # 预注册写死,不许扫
FOLLOW = "永安期货"

C = {}
for code, stem in (("SA", "sa"), ("FG", "fg")):
    H.use(code)
    price = H.clean_price(pd.read_csv(DATA / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(DATA / f"{stem}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    g, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        g, log = H.apply_group_overrides(g, log, cuts, H.RULES["group_overrides"], seat, price)
    if H.RULES.get("freeze_since"):
        g, log, cuts = H.freeze_groups(g, log, cuts, H.RULES["freeze_since"])
    rdf, _ = H.retail_series(seat, mkt.index)
    raw = H.signal_series(seat, g)
    unl = H.unload_series(raw, seat, g)["pct"].reindex(mkt.index)
    cc = H.inst_cost_series(raw, mkt, g)
    prod = H.attach_cost_signal(raw, seat, mkt, g)          # 生产口径,自检用

    net = raw["net"]
    peak30 = net.abs().rolling(UWIN, min_periods=10).max()
    unl30 = (1 - net.abs() / peak30).clip(lower=0)

    # 逐席位净持仓(当日可见口径)→「全部同向」
    cols = {}
    for m in sorted({x for grp in g.dropna().unique() for x in grp}):
        r = seat[seat["member_key"] == m]
        if not r.empty:
            off, _f = H._pit_pair(r)
            cols[m] = off.reindex(mkt.index)
    P = pd.DataFrame(cols)
    same = []
    for d in mkt.index:
        grp = g.get(d)
        if not grp:
            same.append(False)
            continue
        row = P.loc[d, [m for m in grp if m in P.columns]].dropna()
        row = row[row != 0]
        same.append(bool(len(row) >= MIN_ON and len(set(np.sign(row).tolist())) == 1))
    same = pd.Series(same, index=mkt.index)
    gate_b = (same & (net.abs() > BIG)).fillna(False)

    ya = P.get(FOLLOW)
    w = None
    if ya is not None:
        mx = ya.abs().rolling(H.SEAT_LEVEL_WIN, min_periods=H.SEAT_LEVEL_MIN).max()
        w = (ya.abs() / mx).clip(upper=1.0)

    C[code] = dict(raw=raw, prod=prod, unl=unl, unl30=unl30, cc=cc, mkt=mkt,
                   op=op, st=st, rdf=rdf, gate_b=gate_b, same=same, w=w, net=net)
    print(f"{code} 预处理完成({len(mkt)} 天)")


def run(code, a=False, b=False, d=False, window=SINCE):
    c = C[code]
    H.use(code)
    unl = c["unl30"] if a else c["unl"]
    ext = H.cost_entry_frame(c["cc"], c["raw"]["net"], c["mkt"]["settle"],
                             unl, c["raw"]["chg"].reindex(c["mkt"].index))
    cz = ext["cost_z"]
    if b:
        cz = cz.where(c["gate_b"], 0.0)      # 不满足就当没信号(0 = 不进场)
    sig = c["raw"].assign(cost_z=cz.reindex(c["raw"].index),
                          cost_reason=ext["cost_reason"].reindex(c["raw"].index))
    trades, pos, daily = H.replay(sig, c["mkt"], c["rdf"], c["op"], c["st"])
    wt = None
    if d and c["w"] is not None:
        wt = c["w"].reindex(daily.index).shift(1)
        daily = daily * wt.fillna(0.0)
    if window is not None:
        daily = daily[daily.index >= window]
        trades = [t for t in trades if pd.Timestamp(t["entry_date"]) >= window]
        if wt is not None:
            wt = wt[wt.index >= window]
    p = H._perf(daily)
    held = pos.reindex(daily.index).abs() > 0
    avgw = float((wt[held].mean())) if wt is not None and held.any() else 1.0
    return {"累计%": p["cum_pct"], "夏普": p["sharpe"] or 0.0, "回撤%": p["max_dd_pct"],
            "笔数": len(trades), "trades": trades, "daily": daily, "平均仓位": avgw}


# 自检:不加任何改动时必须与生产逐字节一致
for code in ("SA", "FG"):
    H.use(code)          # **必须先切品种**:cost_entry_frame 读 RULES(卸仓上限/轮龄/加仓),
                         # 不切就是拿上一个品种的参数算这一个,自检本身就是错的。
    a = H.cost_entry_frame(C[code]["cc"], C[code]["raw"]["net"], C[code]["mkt"]["settle"],
                           C[code]["unl"], C[code]["raw"]["chg"].reindex(C[code]["mkt"].index))
    assert a["cost_z"].equals(C[code]["prod"]["cost_z"]), f"{code} 基线与生产不一致"
print("自检通过:基线 = 生产口径,逐字节一致\n")


def show(tag, r):
    return (f"  {tag:<22}{r['累计%']:>+9.1f}%{r['夏普']:>7.2f}{r['回撤%']:>8.1f}%"
            f"{r['笔数']:>6}{r['平均仓位']:>10.0%}")


def detail(r):
    for t in r["trades"]:
        print(f"      {t['entry_date']} {('空' if t['side']=='short' else '多')}"
              f" @{t['entry_px']:>6.0f} → {t['exit_date']} @{t['exit_px']:>6.0f}"
              f" {t['ret_pct']:>+7.2f}%  ({t['exit_reason']})")


ARMS_SA = [("基线", dict()), ("臂A 卸仓 30 日", dict(a=True)),
           ("臂B 全同向+15万", dict(b=True)), ("臂AB", dict(a=True, b=True)),
           ("臂ABD 跟随永安", dict(a=True, b=True, d=True))]
ARMS_FG = [("基线", dict()), ("臂A 卸仓 30 日", dict(a=True)),
           ("臂A+D 跟随永安", dict(a=True, d=True))]

RES = {}
for code, name, arms in (("SA", "纯碱", ARMS_SA), ("FG", "玻璃", ARMS_FG)):
    for lab, win in (("近两年(主口径)", SINCE), ("全样本(仅供参考)", None)):
        print(f"\n{'='*84}\n=== {name} {code} · {lab} ===")
        print(f"  {'臂':<20}{'累计':>10}{'夏普':>7}{'回撤':>9}{'笔数':>6}{'平均仓位':>10}")
        got = {}
        for tag, kw in arms:
            r = run(code, window=win, **kw)
            got[tag] = r
            print(show(tag, r))
        if win is SINCE:
            RES[code] = got
            for tag, _kw in arms:
                print(f"\n    —— {tag} 的每一笔 ——")
                detail(got[tag]) if got[tag]["trades"] else print("      无成交")

print(f"\n{'='*84}\n=== 两个差分(纯碱,近两年)===")
g = RES["SA"]
print(f"  两道门     臂AB − 基线 :累计 {g['臂AB']['累计%']-g['基线']['累计%']:+.1f}pp  "
      f"回撤 {g['臂AB']['回撤%']-g['基线']['回撤%']:+.1f}pp  "
      f"笔数 {g['臂AB']['笔数']}→ vs {g['基线']['笔数']}")
print(f"  跟随永安   臂ABD − 臂AB:累计 {g['臂ABD 跟随永安']['累计%']-g['臂AB']['累计%']:+.1f}pp  "
      f"回撤 {g['臂ABD 跟随永安']['回撤%']-g['臂AB']['回撤%']:+.1f}pp")
print(f"  其中 A 单独 {g['臂A 卸仓 30 日']['累计%']-g['基线']['累计%']:+.1f}pp、"
      f"B 单独 {g['臂B 全同向+15万']['累计%']-g['基线']['累计%']:+.1f}pp")

print("\n**本次不设统计闸门**(基线仅 7 笔)。判据按 PLAN_SA_TREND_ENTRY_v1 第四节:"
      "如实报事实,不做验证声明,交运营者拍板。")
