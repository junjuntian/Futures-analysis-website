# -*- coding: utf-8 -*-
"""事件四分类:增多 / 减空(回补) / 增空 / 减多(兑现)。

上一轮发现 ΔNet<0 混合"主动增空"与"获利减多",信号含义完全不同,拆开重跑。
分类规则:事件触发同前(|ΔNet/OI| ≥ 滚动250日80分位);主导腿 = |Δlong| vs |Δshort| 谁大。
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_profile import forward_returns, H_LIST

pd.set_option("display.width", 220)


def classify_events(md: pd.DataFrame, cont: pd.DataFrame, members, q=0.80, window=250, min_hist=120):
    fwd = forward_returns(cont)
    oi = cont["oi_total"]
    rows = []
    ev_store = []
    for m in members:
        s = md[md["member"] == m].set_index("trade_date").sort_index()
        if len(s) < min_hist:
            continue
        flow = (s["dnet"] / oi.reindex(s.index)).dropna()
        thr = flow.abs().rolling(window, min_periods=min_hist).quantile(q).shift(1)
        hit = flow[(flow.abs() >= thr) & thr.notna() & (flow != 0)]
        sub = s.loc[hit.index]
        long_dom = sub["dlong"].abs().fillna(0) >= sub["dshort"].abs().fillna(0)
        cls = np.where(sub["dnet"] > 0, np.where(long_dom, "增多", "减空"),
                       np.where(~long_dom, "增空", "减多"))
        for c in ["增多", "减空", "增空", "减多"]:
            idx = sub.index[cls == c]
            if len(idx) < 10:
                continue
            sign = 1 if c in ("增多", "减空") else -1
            f = fwd.reindex(idx)
            for h in H_LIST:
                dr = sign * f[h].dropna()
                if len(dr) < 10:
                    continue
                t = dr.mean() / dr.std(ddof=1) * np.sqrt(len(dr)) if dr.std(ddof=1) > 0 else np.nan
                rows.append({"席位": m, "类": c, "h": h, "N": len(dr),
                             "均值%": dr.mean() * 100, "命中率%": (dr > 0).mean() * 100, "t值": t})
        ev_store.append(pd.DataFrame({"member": m, "trade_date": sub.index, "cls": cls,
                                      "flow": hit.reindex(sub.index)}))
    ev = pd.concat(ev_store, ignore_index=True) if ev_store else pd.DataFrame()
    return pd.DataFrame(rows), ev


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")

    es, ev = classify_events(md, cont, aulib.FOCUS)
    for c in ["增多", "减空", "增空", "减多"]:
        sub = es[es["类"] == c]
        if sub.empty:
            continue
        piv = sub.pivot_table(index="席位", columns="h", values=["N", "均值%", "命中率%", "t值"])
        print(f"\n===== {c} =====")
        print(piv.round(2).to_string())

    ev.to_pickle(aulib.OUT / "events_classified.pkl")
    print("\n已写出 out/events_classified.pkl")


if __name__ == "__main__":
    sys.exit(main())
