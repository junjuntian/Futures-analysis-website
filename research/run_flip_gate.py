# -*- coding: utf-8 -*-
"""方案丙:重仓翻向门。

运营者 2026-09-06 选定:「某一家在主力合约上从净空翻成净多(或反向),
且新方向仓位 ≥ 自身 200 日高位的 60%」→ 不进场。

我细化的三处(原话没说,报告里要写明):
  a. **翻向**= 今天主力净持仓的方向 ≠ **FLIP_BACK 个交易日前**的方向。
     只看单日符号变化会被零附近的抖动刷屏;永安那次是 07-22 −16,971 → 08-21 +94,903,
     跨约 37 个交易日,20 日窗口能覆盖;
  b. **挡的是与翻向者相反的那个方向**(永安翻多 → 挡做空),不是两边都挡。
     运营者原话是「不能进场跟随三家的方向做」,而「三家」就是翻向者的对面;
  c. 主力合约上掉榜那天该席位不判(掉榜是「不知道」不是「没有」)。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
LVL, LVL_WIN, FLIP_BACK = 0.60, 200, 20


def build(code, stem):
    H.use(code)
    price = H.clean_price(pd.read_csv(D / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{stem}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    idx = mkt.index
    g, log, cuts = H.rolling_groups(seat, price, idx)
    g, log = H.apply_group_overrides(g, log, cuts, H.RULES["group_overrides"], seat, price)
    if H.RULES.get("freeze_since"):
        g, log, cuts = H.freeze_groups(g, log, cuts, H.RULES["freeze_since"])
    rdf, _ = H.retail_series(seat, idx)
    sig = H.attach_cost_signal(H.signal_series(seat, g), seat, mkt, g)

    allm = sorted({m for grp in g.dropna().unique() for m in grp})
    off = seat.dropna(subset=["net_off"]).set_index(
        ["member_key", "contract", "trade_date"])["net_off"]
    main = mkt["main"]
    MN = pd.DataFrame({
        m: pd.Series([float(off.get((m, main.get(d), d), np.nan))
                      if isinstance(main.get(d), str) else np.nan for d in idx], index=idx)
        for m in allm})
    return dict(seat=seat, mkt=mkt, idx=idx, g=g, sig=sig, rdf=rdf, op=op, st=st, MN=MN)


def flip_gate(C):
    """返回 (被挡方向 series:-1 挡做空 / +1 挡做多 / 0 不挡, 翻向者)。"""
    idx, g, MN = C["idx"], C["g"], C["MN"]
    lv = {m: (MN[m].abs() / MN[m].abs().rolling(LVL_WIN, min_periods=60).max())
          for m in MN.columns}
    # 「N 日前的方向」用**当时可见**的值,不做任何填充
    prev = {m: np.sign(MN[m]).shift(FLIP_BACK) for m in MN.columns}
    blocked = pd.Series(0, index=idx, dtype=int)
    who = {}
    for d in idx:
        grp = g.get(d) or ()
        for m in grp:
            if m not in MN.columns:
                continue
            v = MN[m].get(d, np.nan)
            p = prev[m].get(d, np.nan)
            if not (np.isfinite(v) and np.isfinite(p)) or v == 0 or p == 0:
                continue
            if np.sign(v) == p:                     # 没翻向
                continue
            w = lv[m].get(d, np.nan)
            if not (np.isfinite(w) and w >= LVL):   # 新方向仓位不够重
                continue
            # 翻向者做多(v>0) → 挡做空(-1);翻向者做空 → 挡做多(+1)
            blocked[d] = -1 if v > 0 else 1
            who[d] = (m, int(v), float(w))
            break
    return blocked, who


for code, name, stem in (("FG", "玻璃", "fg"), ("SA", "纯碱", "sa")):
    C = build(code, stem)
    trades, _p, _dd = H.replay(C["sig"], C["mkt"], C["rdf"], C["op"], C["st"])
    blk, who = flip_gate(C)
    idx, settle = C["idx"], C["mkt"]["settle"]
    print(f"\n{'='*88}\n=== {name} {code}({len(idx)} 天,现行 {len(trades)} 笔)===")
    print(f"  触发 {int((blk != 0).sum())} 天({(blk != 0).mean():.1%})")

    hit = [t for t in trades
           if (blk.get(pd.Timestamp(t["entry_date"]), 0) ==
               (-1 if t["side"] == "short" else 1))]
    print(f"  **会拦下的进场:{len(hit)} 笔**")
    for t in hit:
        d = pd.Timestamp(t["entry_date"])
        m, v, w = who.get(d, ("?", 0, 0))
        print(f"    {t['entry_date']} {('空' if t['side']=='short' else '多')}"
              f" @{t['entry_px']:.0f} → {t['exit_date']} {t['ret_pct']:+.2f}%"
              f" ({t['exit_reason']})   翻向者 {m} {v:,} 手 水位 {w:.0%}")
    if hit:
        r = [t["ret_pct"] for t in hit]
        print(f"    被拦的这些笔:中位 {np.median(r):+.2f}%、均值 {np.mean(r):+.2f}%、"
              f"胜 {sum(1 for x in r if x > 0)}/{len(r)}  "
              f"→ **中位为负 = 拦对了**")

    # 机制检验:触发后价格是否往翻向者的方向走
    ev = [d for d in idx if blk.get(d, 0) != 0]
    if ev:
        agg = {5: [], 10: [], 20: []}
        for d in ev:
            i = idx.get_loc(d)
            sd = -blk[d]                     # 翻向者的方向
            for k in agg:
                j = min(i + k, len(idx) - 1)
                agg[k].append((settle.iloc[j] / settle.iloc[i] - 1) * sd)
        print("  机制检验(顺**翻向者**方向的收益,正 = 翻向者是对的):")
        for k in (5, 10, 20):
            a = np.array(agg[k])
            print(f"    后 {k:>2} 日:中位 {np.median(a):+.2%}  均值 {a.mean():+.2%}  "
                  f"**方向对 {(a > 0).mean():.0%}**({int((a>0).sum())}/{len(a)})")

    if code == "FG":
        aug = [d for d in ev if pd.Timestamp("2026-08-01") <= d <= pd.Timestamp("2026-09-02")]
        print(f"  **2026 年 8 月触发日:{len(aug)} 天**")
        for d in aug:
            m, v, w = who[d]
            print(f"    {d.date()}  翻向者 {m} 主力 {v:,} 手 水位 {w:.0%}  "
                  f"→ 挡{'做空' if blk[d] < 0 else '做多'}")


# ---- 真实回测影响:把门接进 cost_z 再跑一遍(含分数仓位) ----
print("")
print("="*88)
print("真实回测影响(挡住的方向置 0,其余原样;含分数仓位与调仓成本)")
for code, name, stem in (("FG", "玻璃", "fg"), ("SA", "纯碱", "sa")):
    C = build(code, stem)
    H.use(code)
    blk, _who = flip_gate(C)
    base_sig = C["sig"]
    raw_net = base_sig["net"]
    w = H.sizing_weights(raw_net).reindex(C["idx"])

    def perf(sig):
        tr, pos, daily = H.replay(sig, C["mkt"], C["rdf"], C["op"], C["st"])
        sd = H.apply_sizing(daily, pos, w, C["mkt"]["settle"], H.RULES["multiplier"])
        p = H._perf(sd)
        return len(tr), p

    n0, p0 = perf(base_sig)
    cz = base_sig["cost_z"]
    # 挡做空(blk=-1)时把负的 cost_z 置 0;挡做多(blk=+1)时把正的置 0
    kill = ((blk == -1) & (cz < 0)) | ((blk == 1) & (cz > 0))
    n1, p1 = perf(base_sig.assign(cost_z=cz.where(~kill, 0.0)))
    print(f"  {name} {code}:")
    print(f"    现行      {n0:>4} 笔  {p0['cum_pct']:>+8.1f}%  夏普 {p0['sharpe']}  回撤 {p0['max_dd_pct']:.1f}%")
    print(f"    加翻向门  {n1:>4} 笔  {p1['cum_pct']:>+8.1f}%  夏普 {p1['sharpe']}  回撤 {p1['max_dd_pct']:.1f}%")
    print(f"    被挡的信号日 {int(kill.sum())} 天")
