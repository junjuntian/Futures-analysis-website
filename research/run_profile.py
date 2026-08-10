# -*- coding: utf-8 -*-
"""Task 3:席位影响力档案。

三个产出:
1) 全席位盯市盈亏排行(逐合约、结算价盯市、price_multiplier=1000)——"谁真的赚钱"
2) 八席位事件研究(ΔNet/OI 分位事件 -> 前向 1/3/5/10/20 日方向收益)——"谁的动作能预测价格"
3) 大波段启动窗口的席位行为切片——"趋势启动时它们在不在场"
"""
import sys

import numpy as np
import pandas as pd

import aulib

pd.set_option("display.width", 220)
pd.set_option("display.max_rows", 200)

MULT = 1000  # AU price_multiplier: 1000 克/手,报价元/克
H_LIST = [1, 3, 5, 10, 20]


def member_day_table(seat: pd.DataFrame) -> pd.DataFrame:
    """(member, trade_date) -> long_q short_q net dnet,自算品种汇总。"""
    sub = seat[(~seat["is_variety_total"]) & seat["rank_type"].isin(["long", "short"])]
    g = sub.pivot_table(index=["member", "trade_date"], columns="rank_type",
                        values=["quantity", "change"], aggfunc="sum")
    df = pd.DataFrame(index=g.index)
    for kind, name, col in [("quantity", "long", "long_q"), ("quantity", "short", "short_q"),
                            ("change", "long", "dlong"), ("change", "short", "dshort")]:
        s = g[kind][name] if name in g[kind].columns else pd.Series(np.nan, index=g.index)
        df[col] = s
    df["net"] = df["long_q"].fillna(0) - df["short_q"].fillna(0)
    df["dnet"] = df["dlong"].fillna(0) - df["dshort"].fillna(0)
    return df.reset_index()


def mark_to_market_pnl(seat: pd.DataFrame, price: pd.DataFrame) -> pd.DataFrame:
    """逐合约盯市盈亏。net(member,c,t) 持有到 t 的下一交易日,吃 settle 差价。
    未上榜=仓位按不可见处理(0 贡献),对重仓席位是轻微低估,统一口径下可比。"""
    sub = seat[(~seat["is_variety_total"]) & seat["rank_type"].isin(["long", "short"])]
    net = sub.pivot_table(index=["member", "contract", "trade_date"], columns="rank_type",
                          values="quantity", aggfunc="sum")
    net = (net.get("long", pd.Series(dtype=float)).to_frame("l").join(
        net.get("short", pd.Series(dtype=float)).to_frame("s"), how="outer")
        if isinstance(net, pd.DataFrame) else net)
    net["pos"] = net["l"].fillna(0) - net["s"].fillna(0)
    net = net["pos"].reset_index()

    setl = price.set_index(["contract", "trade_date"])["settlement_price"].sort_index()
    # 每合约的下一交易日结算价差
    fwd = setl.groupby(level=0).diff().groupby(level=0).shift(-1)  # diff(t)=s(t)-s(t-1); shift(-1)->s(t+1)-s(t) 记在 t
    fwd = fwd.rename("fwd_diff").reset_index()
    m = net.merge(fwd, on=["contract", "trade_date"], how="left")
    m["pnl"] = m["pos"] * m["fwd_diff"] * MULT
    m["year"] = m["trade_date"].dt.year
    return m


def pnl_ranking(m: pd.DataFrame, since=None) -> pd.DataFrame:
    d = m if since is None else m[m["trade_date"] >= since]
    g = d.groupby("member").agg(pnl=("pnl", "sum"), days=("trade_date", "nunique"),
                                avg_abs=("pos", lambda x: x.abs().mean()))
    hand = d.groupby(["member", "trade_date"])["pos"].apply(lambda x: x.abs().sum()).groupby("member").mean()
    g["日均手数"] = hand
    g["每手累计盈亏(元)"] = g["pnl"] / g["日均手数"].replace(0, np.nan)
    g["累计盈亏(亿)"] = g["pnl"] / 1e8
    return g.sort_values("pnl", ascending=False)


def forward_returns(cont: pd.DataFrame):
    """R_h(t) = 从 t+1 收盘持有 h 日(t+2..t+1+h 的日收益复利)。"""
    logr = np.log1p(cont["ret"].fillna(0)).to_numpy()
    cum = np.concatenate([[0.0], np.cumsum(logr)])  # cum[i] = sum logr[0..i-1]
    n = len(logr)
    out = {}
    for h in H_LIST:
        v = np.full(n, np.nan)
        hi = np.arange(n) + 1 + h
        ok = hi < n
        idx = np.arange(n)[ok]
        v[idx] = np.exp(cum[hi[ok] + 1] - cum[idx + 2]) - 1.0
        out[h] = v
    return pd.DataFrame(out, index=cont.index)


def event_study(md: pd.DataFrame, cont: pd.DataFrame, members, q=0.80, window=250, min_hist=120):
    fwd = forward_returns(cont)
    oi = cont["oi_total"]
    rows = []
    events_all = {}
    for m in members:
        s = md[md["member"] == m].set_index("trade_date").sort_index()
        if len(s) < min_hist:
            continue
        flow = (s["dnet"] / oi.reindex(s.index)).dropna()
        thr = flow.abs().rolling(window, min_periods=min_hist).quantile(q).shift(1)
        ev = flow[(flow.abs() >= thr) & thr.notna() & (flow != 0)]
        events_all[m] = ev
        for side, sign in [("多", 1), ("空", -1)]:
            e = ev[np.sign(ev) == sign]
            if len(e) < 10:
                continue
            f = fwd.reindex(e.index)
            for h in H_LIST:
                dr = sign * f[h].dropna()
                if len(dr) < 10:
                    continue
                t = dr.mean() / dr.std(ddof=1) * np.sqrt(len(dr)) if dr.std(ddof=1) > 0 else np.nan
                rows.append({"席位": m, "方向": side, "h": h, "N": len(dr),
                             "均值%": dr.mean() * 100, "命中率%": (dr > 0).mean() * 100, "t值": t})
    return pd.DataFrame(rows), events_all


def baseline(cont: pd.DataFrame):
    fwd = forward_returns(cont)
    rows = []
    for h in H_LIST:
        v = fwd[h].dropna()
        rows.append({"h": h, "全样本均值%": v.mean() * 100, "全样本|收益|中位%": v.abs().median() * 100,
                     "上涨占比%": (v > 0).mean() * 100})
    return pd.DataFrame(rows)


def swing_slices(md: pd.DataFrame, cont: pd.DataFrame, zz: pd.DataFrame, members, pre=10, post=10):
    """每个波段起点,窗口内八席位累计 ΔNet(同向为正)。"""
    dates = cont.index.to_list()
    pos = {d: i for i, d in enumerate(dates)}
    md_p = {m: md[md["member"] == m].set_index("trade_date")["dnet"] for m in members}
    recs = []
    for _, leg in zz.iterrows():
        d0 = leg["from"]
        if pd.isna(d0) or d0 not in pos:
            continue
        i = pos[d0]
        w = dates[max(0, i - pre): min(len(dates), i + post + 1)]
        sgn = 1 if leg["type"] == "上涨" else -1
        rec = {"启动日": d0.date(), "类型": leg["type"], "段幅%": (leg["pct"] * 100) if pd.notna(leg["pct"]) else np.nan}
        for m in members:
            s = md_p[m].reindex(w).dropna()
            rec[m] = sgn * s.sum() if len(s) else np.nan
        recs.append(rec)
    return pd.DataFrame(recs)


def main():
    price = aulib.load_price()
    seat = aulib.load_seat()
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    zz = pd.read_pickle(aulib.OUT / "au_zigzag10.pkl")

    md = member_day_table(seat)

    print("== 1) 全席位盯市盈亏排行(全历史 2008-2026,只计榜上可见仓位) ==")
    m = mark_to_market_pnl(seat, price)
    rank_all = pnl_ranking(m)
    show = rank_all.head(20)[["累计盈亏(亿)", "日均手数", "每手累计盈亏(元)", "days"]]
    print(show.to_string())
    print("\n-- 亏损末 10 名 --")
    print(rank_all.tail(10)[["累计盈亏(亿)", "日均手数", "每手累计盈亏(元)", "days"]].to_string())

    print("\n== 近 3 年(2023-08 起,与高盛可比窗口) ==")
    rank_3y = pnl_ranking(m, since=pd.Timestamp("2023-08-01"))
    print(rank_3y.head(15)[["累计盈亏(亿)", "日均手数", "每手累计盈亏(元)", "days"]].to_string())

    print("\n-- 八席位在两个窗口的位置 --")
    for name, r in [("全历史", rank_all), ("近3年", rank_3y)]:
        sub = r.reindex(aulib.FOCUS).dropna(subset=["pnl"])
        sub = sub.assign(名次=[int((r["pnl"] > v).sum() + 1) for v in sub["pnl"]])
        print(f"[{name}]")
        print(sub[["累计盈亏(亿)", "日均手数", "每手累计盈亏(元)", "名次"]].to_string())

    print("\n== 2) 事件研究基线(全样本前向收益) ==")
    print(baseline(cont).to_string(index=False))

    print("\n== 八席位事件研究(|ΔNet/OI| 滚动250日80分位触发,T+1收盘起算) ==")
    es, events = event_study(md, cont, aulib.FOCUS)
    piv = es.pivot_table(index=["席位", "方向", ], columns="h", values=["N", "均值%", "命中率%", "t值"])
    print(piv.round(2).to_string())

    print("\n== 3) 大波段启动窗口(±10日)八席位同向累计增仓(手,正=方向正确) ==")
    sl = swing_slices(md, cont, zz, aulib.FOCUS)
    print(sl.to_string(index=False))
    print("\n-- 波段启动参与率(窗口内同向累计增仓>0 的波段占比) --")
    part = {m: (sl[m] > 0).mean() * 100 for m in aulib.FOCUS}
    print(pd.Series(part).round(1).to_string())

    es.to_pickle(aulib.OUT / "event_study_focus.pkl")
    sl.to_pickle(aulib.OUT / "swing_slices.pkl")
    m.groupby(["member", "year"])["pnl"].sum().to_pickle(aulib.OUT / "pnl_by_year.pkl")
    md.to_pickle(aulib.OUT / "member_day.pkl")
    print("\n已写出 out/event_study_focus.pkl, swing_slices.pkl, pnl_by_year.pkl, member_day.pkl")


if __name__ == "__main__":
    sys.exit(main())
