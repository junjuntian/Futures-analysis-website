# -*- coding: utf-8 -*-
"""测量:现行进场点在最近 20/30 天 K 线里落在哪 —— 到底是不是「卖在低点、买在高点」。

运营者 2026-09-06:「重新测进场点,进场点的好坏,用最近20-30天的k线来衡量,
看看到底是不是买在高点和低点,先把这个测准。**先不要再测回撤了,没有意义。**」

**这是描述性测量,不产出收益判定,也不改任何规则。** 两节:

一、进场点位置。对每一笔真实成交(entry_px = 次日开盘价,DEC-090 的成交口径),
    算它在**同一合约**过去 N 个交易日高低区间里的位置:
        pos_N = (成交价 − 近 N 日最低) / (近 N 日最高 − 近 N 日最低)
    做空要卖在高位、做多要买在低位,所以统一成
        **进场点得分 = 做空取 pos,做多取 1 − pos**(0~1,越高越好,0.5 = 随机)。
    N ∈ {20, 30},两个都报。
    对照两组席位:现行生产组(滚动 5 家 + 点名换人)与运营者点名的四家。

二、运营者说的「六成仓」现象。他给的两个例子:海通在纯碱 09-03 暴涨前建了两万多手
    净多(自身高位约三万手)、永安在玻璃以 932 均价建了九万手净多(上限约十五万手)。
    量化成:单席位 |净持仓| 达到自身近 500 日最大值的 60% 时,之后 5/10/20 日价格怎么走。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
TRIO4 = ["国泰君安", "海通期货", "东证期货", "永安期货"]   # 运营者点名的四家
NS = (20, 30)
LEVEL = 0.60          # 「六成仓」
MAXWIN = 500          # 席位自身高位的回看窗


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
    return dict(price=price, seat=seat, mkt=mkt, op=op, st=st, roll=g, rdf=rdf)


def trades_of(C, code, members=None):
    H.use(code)
    if members is None:
        g = C["roll"]
    else:
        g = H.fixed_groups(list(members), C["seat"], C["price"], C["mkt"].index,
                           C["mkt"].index[-1].strftime("%Y-%m-%d"))[0]
    sig = H.attach_cost_signal(H.signal_series(C["seat"], g), C["seat"], C["mkt"], g)
    tr, _p, _d = H.replay(sig, C["mkt"], C["rdf"], C["op"], C["st"])
    return tr


def score_trades(C, trades):
    """给每一笔算 pos_20 / pos_30 与进场点得分。区间用**该笔自己的合约**,不用主力串。"""
    px = C["price"].set_index(["contract", "trade_date"]).sort_index()
    out = []
    for t in trades:
        c, d0 = t["contract"], pd.Timestamp(t["entry_date"])
        try:
            sub = px.loc[c]
        except KeyError:
            continue
        sub = sub[sub.index <= d0]
        row = {"entry_date": d0, "side": t["side"], "px": t["entry_px"],
               "ret": t["ret_pct"], "contract": c}
        ok = True
        for n in NS:
            w = sub.tail(n)
            if len(w) < n or not np.isfinite(t["entry_px"]):
                ok = False
                break
            hi, lo = float(w["high_price"].max()), float(w["low_price"].min())
            if not np.isfinite(hi) or not np.isfinite(lo) or hi <= lo:
                ok = False
                break
            p = (t["entry_px"] - lo) / (hi - lo)
            p = min(1.0, max(0.0, p))
            row[f"pos{n}"] = p
            row[f"s{n}"] = p if t["side"] == "short" else 1 - p
        if ok:
            out.append(row)
    return pd.DataFrame(out)


def report(tag, df):
    if df.empty:
        print(f"  {tag}:无可算的成交")
        return
    print(f"  {tag}({len(df)} 笔,做空 {(df.side=='short').sum()} / 做多 {(df.side=='long').sum()})")
    for n in NS:
        s = df[f"s{n}"]
        bad = (s < 0.3).mean()
        good = (s > 0.7).mean()
        print(f"    {n} 日窗口:得分中位 {s.median():.2f}  四分位 {s.quantile(.25):.2f}~{s.quantile(.75):.2f}"
              f"   进在最差一端(<0.30)的占 {bad:.0%}   进在最好一端(>0.70)的占 {good:.0%}")
    for sd, lab in (("short", "做空"), ("long", "做多")):
        x = df[df.side == sd]
        if len(x):
            print(f"    {lab} {len(x)} 笔:pos20 中位 {x['pos20'].median():.2f}"
                  f"(做空该高、做多该低)")
    # 尺子有没有意义:按得分分三档看这一笔后来赚没赚
    print("    —— 得分分档 vs 该笔实际盈亏(只为核验这把尺子有没有意义)——")
    q = pd.cut(df["s20"], [-.01, 1/3, 2/3, 1.01], labels=["差 <0.33", "中", "好 >0.67"])
    for k, x in df.groupby(q, observed=True):
        print(f"      {str(k):<10}{len(x):>4} 笔   中位 {x['ret'].median():+6.2f}%   "
              f"均值 {x['ret'].mean():+6.2f}%   胜率 {(x['ret'] > 0).mean():.0%}")


def seat_level_events(C, code, members):
    """单席位水位 ≥ LEVEL 的事件,之后 5/10/20 日主力价格怎么走。"""
    seat, mkt = C["seat"], C["mkt"]
    idx = mkt.index
    settle = mkt["settle"]
    rows = []
    for m in members:
        r = seat[seat["member_key"] == m]
        if r.empty:
            continue
        off, full = H._pit_pair(r)
        net = off.reindex(idx)                      # 当日可见口径,不用反推值
        mx = net.abs().rolling(MAXWIN, min_periods=120).max()
        lvl = net.abs() / mx
        hit = (lvl >= LEVEL) & net.notna()
        armed = True
        for d in idx:
            if not np.isfinite(lvl.get(d, np.nan)):
                continue
            if hit.get(d, False) and armed:
                i = idx.get_loc(d)
                fwd = {k: (settle.iloc[min(i + k, len(idx) - 1)] / settle.iloc[i] - 1)
                       for k in (5, 10, 20)}
                rows.append({"member": m, "date": d, "dir": "多" if net[d] > 0 else "空",
                             "net": int(net[d]), "peak": int(mx[d]), "lvl": float(lvl[d]),
                             **{f"r{k}": v for k, v in fwd.items()}})
                armed = False
            elif not hit.get(d, False):
                armed = True
    E = pd.DataFrame(rows)
    if E.empty:
        print("  没有事件")
        return E
    print(f"  单席位水位 ≥{LEVEL:.0%} 的事件共 {len(E)} 次"
          f"(净多 {(E.dir=='多').sum()} / 净空 {(E.dir=='空').sum()})")
    for dr, want in (("多", 1), ("空", -1)):
        x = E[E.dir == dr]
        if not len(x):
            continue
        print(f"    净{dr}达六成后:", end="")
        for k in (5, 10, 20):
            hitrate = (np.sign(x[f"r{k}"]) == want).mean()
            print(f"  {k}日 中位 {x[f'r{k}'].median():+.2%}/方向对 {hitrate:.0%}", end="")
        print()
    print(f"    按席位分:")
    for m, x in E.groupby("member"):
        print(f"      {m:<8}{len(x):>3} 次   10 日中位 "
              f"{x['r10'].median():+.2%}   方向对 "
              f"{(np.sign(x['r10']) == np.where(x['dir']=='多', 1, -1)).mean():.0%}")
    return E


for code, name, stem in (("SA", "纯碱", "sa"), ("FG", "玻璃", "fg")):
    C = load(code, stem)
    print(f"\n{'='*94}\n=== {name} {code}  {C['mkt'].index[0].date()}~{C['mkt'].index[-1].date()} ===")
    print("\n【一、进场点落在 K 线的哪里】得分 0~1,越高越好;0.5 相当于随便进")
    report("现行生产组(滚动 5 家 + 点名换人)", score_trades(C, trades_of(C, code)))
    df4 = score_trades(C, trades_of(C, code, TRIO4))
    report(f"运营者点名四家({'/'.join(TRIO4)})", df4)

    if len(df4):
        print("\n    最近 12 笔明细(点名四家):")
        print(f"      {'进场日':<12}{'向':>4}{'合约':>9}{'成交价':>9}{'pos20':>8}{'pos30':>8}{'得分20':>9}{'收益':>9}")
        for _, r in df4.tail(12).iterrows():
            print(f"      {r['entry_date'].date()!s:<12}{('空' if r['side']=='short' else '多'):>4}"
                  f"{r['contract']:>9}{r['px']:>9.0f}{r['pos20']:>8.2f}{r['pos30']:>8.2f}"
                  f"{r['s20']:>9.2f}{r['ret']:>+8.2f}%")

    print(f"\n【二、单席位「六成仓」现象】水位 = |净持仓| / 自身近 {MAXWIN} 日最大值")
    seat_level_events(C, code, TRIO4)
