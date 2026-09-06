# -*- coding: utf-8 -*-
"""可行性测量:运营者 2026-09-06 提的「异见席位」阻止门。

原话:「任何一家席位和另外三家持仓相反,而且仓位达到自己最大仓位的 60%,
同时三家合计持仓低于 9 个月最大仓位的 50%,这就不能进场跟随三家的方向做,
这就代表反弹或者转向。」

三条**同时**成立才拦:
  ① 组内恰好 3 家同向、1 家反向(下限:当日在榜且非零的必须是 4 家 —— 掉榜是「不知道」,
     不因不知道而拦。**这个下限是我定的**);
  ② 那一家反向席位的**自身水位** ≥ 60%(|净持仓| ÷ 自身近 500 日最大,DEC-222 口径);
  ③ **同向那三家合计** |净持仓| < 它们合计的近 9 个月(180 交易日)最大值 × 50%。

**只看数,不下结论。**
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
LVL_HOT, TRIO_LOW, TRIO_WIN = 0.60, 0.50, 180


def load(code, stem):
    H.use(code)
    price = H.clean_price(pd.read_csv(D / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{stem}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    g, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        g, log = H.apply_group_overrides(g, log, cuts, H.RULES["group_overrides"], seat, price)
    if H.RULES.get("freeze_since"):
        g, log, cuts = H.freeze_groups(g, log, cuts, H.RULES["freeze_since"])
    rdf, _ = H.retail_series(seat, mkt.index)
    sig = H.attach_cost_signal(H.signal_series(seat, g), seat, mkt, g)
    return dict(price=price, seat=seat, mkt=mkt, g=g, sig=sig, op=op, st=st, rdf=rdf)


def per_seat(seat, dates, members):
    cols = {}
    for m in members:
        r = seat[seat["member_key"] == m]
        if not r.empty:
            off, _f = H._pit_pair(r)
            cols[m] = off.reindex(dates)
    return pd.DataFrame(cols)


for code, name, stem in (("SA", "纯碱", "sa"), ("FG", "玻璃", "fg")):
    C = load(code, stem)
    H.use(code)
    mkt, g, sig = C["mkt"], C["g"], C["sig"]
    idx = mkt.index
    allm = sorted({x for grp in g.dropna().unique() for x in grp})
    P = per_seat(C["seat"], idx, allm)
    LV = {m: (P[m].abs() / P[m].abs().rolling(H.SEAT_LEVEL_WIN,
              min_periods=H.SEAT_LEVEL_MIN).max()) for m in P.columns}

    # 同向三家的合计,需要逐日按「谁是少数派」动态取,所以先把三条件逐日算出来
    rows = []
    trio_sum = pd.Series(np.nan, index=idx)
    for d in idx:
        grp = g.get(d)
        if not grp:
            rows.append((False, False, False, np.nan, np.nan))
            continue
        cols = [m for m in grp if m in P.columns]
        row = P.loc[d, cols].dropna()
        row = row[row != 0]
        if len(row) != len(grp) or len(grp) < 4:
            rows.append((False, False, False, np.nan, np.nan))   # 掉榜/不足 4 家 → 不拦
            continue
        sg = np.sign(row)
        maj = sg.value_counts()
        c1 = (len(maj) == 2 and sorted(maj.values.tolist()) == [1, len(grp) - 1])
        if not c1:
            rows.append((False, False, False, np.nan, np.nan))
            continue
        minority = sg[sg == maj.idxmin()].index[0]
        trio = [m for m in row.index if m != minority]
        lv = LV[minority].get(d, np.nan)
        c2 = bool(np.isfinite(lv) and lv >= LVL_HOT)
        trio_sum[d] = row[trio].sum()
        rows.append((c1, c2, None, lv, trio_sum[d]))

    tmax = trio_sum.abs().rolling(TRIO_WIN, min_periods=20).max()
    frac = trio_sum.abs() / tmax
    R = pd.DataFrame(rows, index=idx, columns=["c1", "c2", "_", "lv", "trio"])
    R["c3"] = (frac < TRIO_LOW).fillna(False)
    R["block"] = R["c1"] & R["c2"] & R["c3"]

    print(f"\n{'='*84}\n=== {name} {code}  {idx[0].date()}~{idx[-1].date()}({len(idx)} 天)===")
    print(f"  ① 3 对 1(且 4 家全在榜)       {R['c1'].mean():.1%} 的天({int(R['c1'].sum())} 天)")
    print(f"  ①+② 少数派自身水位 ≥{LVL_HOT:.0%}  {(R['c1']&R['c2']).mean():.1%} 的天"
          f"({int((R['c1']&R['c2']).sum())} 天)")
    print(f"  **三条全中(会拦)**            {R['block'].mean():.1%} 的天"
          f"({int(R['block'].sum())} 天)")

    tr, _p, _d = H.replay(sig, mkt, C["rdf"], C["op"], C["st"])
    hit = [t for t in tr if R["block"].get(pd.Timestamp(t["entry_date"]), False)]
    print(f"\n  现行 {len(tr)} 笔里,进场日被这道门拦下的:**{len(hit)} 笔**")
    for t in hit:
        print(f"    {t['entry_date']} {('空' if t['side']=='short' else '多')} "
              f"@{t['entry_px']:.0f} → {t['exit_date']} {t['ret_pct']:+.2f}% ({t['exit_reason']})")
    if hit:
        r = [t["ret_pct"] for t in hit]
        print(f"    这些笔合计:中位 {np.median(r):+.2f}%、"
              f"胜 {sum(1 for x in r if x>0)}/{len(r)}")
    print(f"  末日状态:c1={bool(R['c1'].iloc[-1])} c2={bool(R['c2'].iloc[-1])} "
          f"c3={bool(R['c3'].iloc[-1])} → {'拦' if R['block'].iloc[-1] else '不拦'}")

    # —— 机制检验:触发日之后,价格是不是逆着「三家的方向」走 ——
    settle = mkt["settle"]
    ev = [d for d in idx if R["block"].get(d, False)]
    print("")
    print(f"  —— 机制检验:{len(ev)} 个触发日之后,价格相对**三家方向**怎么走 ——")
    print(f"    {'触发日':<12}{'三家向':>7}{'少数派水位':>11}{'三家/9月高位':>13}"
          f"{'后5日':>9}{'后10日':>9}{'后20日':>9}")
    agg = {5: [], 10: [], 20: []}
    for d in ev:
        i = idx.get_loc(d)
        tri = R["trio"].get(d, np.nan)
        sdir = 1 if tri > 0 else -1                    # 三家净多=看涨
        fr = frac.get(d, np.nan)
        cells = []
        for k in (5, 10, 20):
            j = min(i + k, len(idx) - 1)
            r = settle.iloc[j] / settle.iloc[i] - 1
            agg[k].append(r * sdir)                    # 顺着三家方向的收益
            cells.append(f"{r*sdir:>+8.2%}")
        print(f"    {d.date()!s:<12}{('多' if sdir>0 else '空'):>7}"
              f"{R['lv'].get(d, float('nan')):>11.0%}{fr:>13.0%}" + "".join(cells))
    if ev:
        print("    —— 顺着三家方向的收益(**负 = 逆行,即运营者说对了**)——")
        for k in (5, 10, 20):
            a = np.array(agg[k])
            print(f"    后 {k:>2} 日:中位 {np.median(a):+.2%}  均值 {a.mean():+.2%}  "
                  f"**逆行占 {(a < 0).mean():.0%}**({int((a<0).sum())}/{len(a)})")
