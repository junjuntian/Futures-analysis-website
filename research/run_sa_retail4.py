# -*- coding: utf-8 -*-
"""PLAN_SA_RETAIL4_v1(v1.1)的跑数脚本 —— 纯碱散户判据加广发,六道闸门。

**先读 PLAN_SA_RETAIL4_v1.md,再读本文件。** 闸门与结局处置都在那边事前钉死,
这里只负责如实算,不负责判定要不要上线。

两条口径纪律(都是踩过的):
  · `replay()` 返回 **(trades, pos, daily)** —— 顺序别记错。把 pos 当 daily
    会算出 −100% 这种自造数字(2026-09-04 踩过一次)。
  · 基线必须与 `run_one` 出的 payload **逐位对上**才继续,见 selfcheck()。
    自己搭的管线和生产管线对不上时,后面所有闸门都没有意义。

用法:
    CSV_DIR=research/data python research/run_sa_retail4.py
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import hog_money as H  # noqa: E402

DATA = Path(os.environ.get("CSV_DIR", "research/data"))
BASE_SEED = ["东方财富", "平安期货", "徽商期货"]
CAND = "广发期货"
CODE = "SA"

_CACHE: dict = {}


def load():
    if "px" not in _CACHE:
        price = H.clean_price(pd.read_csv(DATA / "sa_price.csv.gz"))
        seat = H.clean_seat(pd.read_csv(DATA / "sa_seat.csv.gz"))
        _CACHE["px"], _CACHE["seat"] = price, seat
    return _CACHE["px"], _CACHE["seat"]


def build(seed):
    """跑一次完整回放,返回 (trades, daily)。管线与 run_one 逐步对齐。"""
    H.use(CODE)
    H.RULES["retail_seed"] = list(seed)
    price, seat = load()
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        groups, log = H.apply_group_overrides(
            groups, log, cuts, H.RULES["group_overrides"], seat, price)
    sig = H.signal_series(seat, groups)
    if H.RULES["signal_source"] == "cost":
        sig = H.attach_cost_signal(sig, seat, mkt, groups)
    if H.RULES["exit_mode"] == "inst":
        sig = H.attach_inst_exit(sig, seat, mkt, groups)
    if H.RULES["long_mode"] == "unload_bounce":
        sig = H.attach_bounce_long(sig, seat, mkt, groups)
    rdf, _have = H.retail_series(seat, mkt.index)
    trades, _pos, daily = H.replay(sig, mkt, rdf, op, st)
    return trades, daily


def selfcheck(out_dir: Path):
    """自己搭的管线必须和生产 run_one 出的 payload 对上,否则整份结果作废。"""
    H.use(CODE)
    H.RULES["retail_seed"] = list(BASE_SEED)
    H.SIG_CACHE.clear()
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        H.run_one(CODE, "csv", out_dir)
    pay = json.loads((out_dir / "sa_signals.json").read_text(encoding="utf-8"))
    want = pay["compare"]["strategy"]
    trades, daily = build(BASE_SEED)
    got = H._perf(daily)
    ok = (got["cum_pct"] == want["cum_pct"] and got["sharpe"] == want["sharpe"]
          and got["max_dd_pct"] == want["max_dd_pct"]
          and len(trades) == pay["stats"]["trades"])
    print("【自检】本脚本管线 vs 生产 run_one")
    print(f"  生产 payload : 累计 {want['cum_pct']:+.1f}%  夏普 {want['sharpe']}  "
          f"回撤 {want['max_dd_pct']}%  笔数 {pay['stats']['trades']}")
    print(f"  本脚本重算   : 累计 {got['cum_pct']:+.1f}%  夏普 {got['sharpe']}  "
          f"回撤 {got['max_dd_pct']}%  笔数 {len(trades)}")
    print("  →", "一致,继续" if ok else "**对不上,整份结果作废**")
    if not ok:
        sys.exit(1)
    return pay


def exits_of(trades):
    """出场指纹:(进场日, 合约) -> 出场日。用来数「有多少笔的出场真的变了」。"""
    return {(t["entry_date"], t["contract"]): t["exit_date"] for t in trades}


def changed_exits(a, b):
    ea, eb = exits_of(a), exits_of(b)
    keys = set(ea) | set(eb)
    return sum(1 for k in keys if ea.get(k) != eb.get(k))


def cum_of(trades):
    """逐笔复利累计(%)。用于 G4 去掉最赚 3 笔后的重算。"""
    eq = 1.0
    for t in trades:
        eq *= 1 + t["ret_pct"] / 100.0
    return (eq - 1) * 100


def yearly(daily):
    d = daily.fillna(0)
    return ((1 + d).groupby(d.index.year).prod() - 1) * 100


def sharpe(d):
    d = d.fillna(0)
    return float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else float("nan")


def pool_members(price, seat, idx):
    """G1 候选池(v1.1 定义):品种日上榜率 ≥60% 且 盯市为负。净多只列不筛。"""
    d = seat.merge(price[["contract", "trade_date", "settle"]],
                   on=["contract", "trade_date"], how="inner")
    d = d.sort_values(["member_key", "contract", "trade_date"])
    g = d.groupby(["member_key", "contract"])
    d["pn"] = g["net"].shift()
    d["ps"] = g["settle"].shift()
    gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
    d = d[d.pn.notna() & (gap <= 5)]
    d = d.assign(dpx=lambda x: (x.settle - x.ps) * H.RULES["multiplier"])
    pnl = d.groupby("member_key").apply(lambda s: (s.dpx * s.pn).sum(),
                                        include_groups=False)
    bd = seat.groupby(["member_key", "trade_date"])["net"].sum()
    rows = []
    for m in sorted(set(seat.member_key)):
        s = bd.loc[m]
        s = s[s.index.isin(idx)]
        if not len(s):
            continue
        rows.append((m, len(s) / len(idx) * 100, float((s > 0).mean() * 100),
                     float(pnl.get(m, np.nan)) / 1e8))
    r = pd.DataFrame(rows, columns=["席位", "上榜", "净多", "盯市亿"]).set_index("席位")
    strict = r[(r.上榜 >= 60) & (r.盯市亿 < 0)]
    wide = r[r.上榜 >= 60]
    return r, strict, wide


def main():
    out_dir = Path(os.environ.get("S", "."))
    pay = selfcheck(out_dir)
    print()

    b_tr, b_dy = build(BASE_SEED)
    c_tr, c_dy = build(BASE_SEED + [CAND])
    b_p, c_p = H._perf(b_dy), H._perf(c_dy)

    print("【主数字】纯碱 2020-06 起,同一段,基线在同一段重算")
    print(f"{'':<14}{'累计':>10}{'笔数':>6}{'胜率':>8}{'夏普':>8}{'回撤':>9}")
    for tag, tr, p in (("现行 3 家", b_tr, b_p), ("3 家 + 广发", c_tr, c_p)):
        wr = sum(1 for t in tr if t["ret_pct"] > 0) / len(tr) * 100
        print(f"{tag:<14}{p['cum_pct']:>+9.1f}%{len(tr):>6}{wr:>7.1f}%"
              f"{p['sharpe']:>8}{p['max_dd_pct']:>8.1f}%")
    nchg = changed_exits(b_tr, c_tr)
    print(f"\n【硬条件】实际出场发生变化的笔数 = {nchg}(预注册要求 ≥10,"
          f"否则即使六关全过也只按「证据薄弱」处置)")

    print("\n【G1 候选池安慰剂 · 核心】")
    price, seat = load()
    mkt = H.main_series(price)
    idx = mkt.index[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    r, strict, wide = pool_members(price, seat, idx)
    pool = [m for m in strict.index if m not in BASE_SEED]
    if CAND not in pool:
        print(f"  **{CAND} 不在池内,G1 无法执行** —— 见预注册修订记录")
        sys.exit(1)
    print(f"  池子(品种日上榜≥60% 且 盯市为负,已剔除现行三家)= {len(pool)} 家")
    res = []
    for m in pool:
        tr, dy = build(BASE_SEED + [m])
        p = H._perf(dy)
        res.append({"席位": m, "夏普": p["sharpe"], "累计": p["cum_pct"],
                    "回撤": p["max_dd_pct"], "改动出场笔数": changed_exits(b_tr, tr),
                    "净多": round(float(r.loc[m, "净多"]), 1)})
    df = pd.DataFrame(res).sort_values("夏普", ascending=False).reset_index(drop=True)
    df.index += 1
    print(df.to_string())
    rank = int(df.index[df["席位"] == CAND][0])
    p1 = (rank - 1) / len(pool)
    print(f"\n  {CAND} 排第 {rank}/{len(pool)}  →  p = ({rank}−1)/{len(pool)} = {p1:.3f}"
          f"   {'过' if p1 < 0.05 else '不过'}(闸门 p<0.05)")
    deg = int((df["改动出场笔数"] == 0).sum())
    print(f"  变异量披露(PITFALLS #10):池中有 {deg} 家「一笔出场都没改」,"
          f"属退化样本;非退化 {len(pool)-deg} 家")
    if deg:
        nd = df[df["改动出场笔数"] > 0].reset_index(drop=True)
        nd.index += 1
        r2 = int(nd.index[nd["席位"] == CAND][0])
        print(f"  只在非退化样本里排名:第 {r2}/{len(nd)}  →  "
              f"p = {(r2-1)/len(nd):.3f}")

    print("\n【G2 逐年】(候选 ≥ 基线 的年份需 ≥4/6)")
    yb, yc = yearly(b_dy), yearly(c_dy)
    win = 0
    tot = 0
    for y in sorted(set(yb.index) | set(yc.index)):
        a, c = yb.get(y, np.nan), yc.get(y, np.nan)
        if y == 2020:
            print(f"  {y}  基线 {a:+7.1f}%   候选 {c:+7.1f}%   (半年,不计)")
            continue
        tot += 1
        ok = c >= a
        win += ok
        print(f"  {y}  基线 {a:+7.1f}%   候选 {c:+7.1f}%   {'✓' if ok else '✗'}")
    print(f"  → {win}/{tot}  {'过' if win >= 4 else '不过'}")

    print("\n【G3 后半不塌】(后半夏普 候选 ≥ 基线)")
    mid = b_dy.index[len(b_dy) // 2]
    for tag, dy in (("基线", b_dy), ("候选", c_dy)):
        print(f"  {tag}  前半 {sharpe(dy[dy.index < mid]):+.2f}   "
              f"后半 {sharpe(dy[dy.index >= mid]):+.2f}")
    g3 = sharpe(c_dy[c_dy.index >= mid]) >= sharpe(b_dy[b_dy.index >= mid])
    print(f"  分界 {mid.date()}  → {'过' if g3 else '不过'}")

    print("\n【G4 集中度】(各去掉自己最赚的 3 笔后,候选累计 ≥ 基线)")
    def drop3(tr):
        s = sorted(tr, key=lambda t: t["ret_pct"], reverse=True)
        return cum_of(s[3:])
    db, dc = drop3(b_tr), drop3(c_tr)
    print(f"  原始逐笔复利   基线 {cum_of(b_tr):+.1f}%   候选 {cum_of(c_tr):+.1f}%")
    print(f"  去掉最赚 3 笔  基线 {db:+.1f}%   候选 {dc:+.1f}%   "
          f"{'过' if dc >= db else '不过'}")

    print("\n【G5 走前】每年年初只用此前数据判「该不该加广发」,据此决定下一年")
    years = sorted({d.year for d in b_dy.index})
    seg, picks = [], []
    for y in years[1:]:
        cut = pd.Timestamp(f"{y}-01-01")
        sb = sharpe(b_dy[b_dy.index < cut])
        sc = sharpe(c_dy[c_dy.index < cut])
        use_c = sc > sb
        picks.append((y, "4家" if use_c else "3家", sb, sc))
        d = (c_dy if use_c else b_dy)
        seg.append(d[(d.index >= cut) & (d.index < pd.Timestamp(f"{y+1}-01-01"))])
    for y, w, sb, sc in picks:
        print(f"  {y} 用 {w}   (截至上年末夏普 3家 {sb:+.2f} / 4家 {sc:+.2f})")
    wf = pd.concat(seg)
    base_same = b_dy[b_dy.index >= pd.Timestamp(f"{years[1]}-01-01")]
    wfp, bsp = H._perf(wf), H._perf(base_same)
    print(f"  走前拼接 累计 {wfp['cum_pct']:+.1f}%  夏普 {wfp['sharpe']}")
    print(f"  同段全程 3 家 累计 {bsp['cum_pct']:+.1f}%  夏普 {bsp['sharpe']}")
    g5 = wfp["cum_pct"] >= bsp["cum_pct"]
    print(f"  → {'过' if g5 else '不过'}")

    print("\n【G6 扣成本与延迟】(两种情形夏普 ≥ 基线同情形的 80%)")
    print("  说明:成本与 T+2 都在**逐笔层面**近似,不是重跑 replay ——")
    print("        引擎的换手成本写死在 replay 内部,这里如实标注为近似。")
    def cost2(tr):
        eq = 1.0
        for t in tr:
            eq *= 1 + (t["ret_pct"] - 0.10) / 100.0   # 再扣一个来回 0.05%×2
        return (eq - 1) * 100
    print(f"  成本翻倍  基线 {cost2(b_tr):+.1f}%   候选 {cost2(c_tr):+.1f}%   "
          f"{'过' if cost2(c_tr) >= cost2(b_tr) else '不过'}")
    op, st = H.contract_prices(load()[0])
    def delay(tr):
        eq = 1.0
        for t in tr:
            c = t["contract"]
            if c not in op.columns:
                eq *= 1 + t["ret_pct"] / 100.0
                continue
            s = op[c].dropna()
            nxt = s[s.index > pd.Timestamp(t["exit_date"])]
            if not len(nxt):
                eq *= 1 + t["ret_pct"] / 100.0
                continue
            px = float(nxt.iloc[0])
            sign = 1 if t["side"] == "long" else -1
            eq *= 1 + sign * (px - t["entry_px"]) / t["entry_px"]
        return (eq - 1) * 100
    print(f"  出场 T+2  基线 {delay(b_tr):+.1f}%   候选 {delay(c_tr):+.1f}%   "
          f"{'过' if delay(c_tr) >= delay(b_tr) else '不过'}")

    print("\n" + "=" * 70)
    print("判定按 PLAN_SA_RETAIL4_v1 第五节执行,本脚本不下结论。")


if __name__ == "__main__":
    main()
