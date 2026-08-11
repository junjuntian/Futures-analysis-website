# -*- coding: utf-8 -*-
"""金银联动分析(运营者理论:金银比均值回归;机构金多银空=比价对冲;银空可预示金)。

A. 金银比(AU_adj/AG_adj)均值回归:250日z分数极端后,比值与两腿各自的后续走势
B. 八家席位跨品种持仓结构:AU净仓 vs AG净仓 日频相关;金多银空时段占比
C. 用户假说检验:AG 席位事件(增多/增空)对 AU 未来收益的跨品种预测力
D. 应用:金银比分位对现有金/银买点质量的调制(分层统计)
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_optimize import build_events
from run_profile import forward_returns
import v46_silver

pd.set_option("display.width", 250)
GROUP8 = ["中财期货", "中信期货", "海通期货", "国泰君安", "高盛期货", "东证期货", "华泰期货", "国投期货"]
EXTRA_ALIAS = {"国投安信": "国投期货"}


def member_day_au():
    seat = aulib.load_seat("au")
    seat["member"] = seat["member"].replace(EXTRA_ALIAS)
    sub = seat[(~seat["is_variety_total"]) & seat["rank_type"].isin(["long", "short"])]
    g = sub.pivot_table(index=["member", "trade_date"], columns="rank_type",
                        values=["quantity", "change"], aggfunc="sum")
    md = pd.DataFrame(index=g.index)
    for kind, name, col in [("quantity", "long", "long_q"), ("quantity", "short", "short_q"),
                            ("change", "long", "dlong"), ("change", "short", "dshort")]:
        md[col] = g[kind][name] if name in g[kind].columns else np.nan
    md["net"] = md["long_q"].fillna(0) - md["short_q"].fillna(0)
    md["dnet"] = md["dlong"].fillna(0) - md["dshort"].fillna(0)
    return md.reset_index()


def neg_events(md, cont, members):
    oi = cont["oi_total"]
    out = []
    for m in members:
        s = md[md["member"] == m].set_index("trade_date").sort_index()
        if len(s) < 120:
            continue
        flow = (s["dnet"] / oi.reindex(s.index)).dropna()
        thr = flow.abs().rolling(250, min_periods=120).quantile(0.80).shift(1)
        hit = flow[(flow.abs() >= thr) & thr.notna() & (flow < 0)]
        sub = s.loc[hit.index]
        short_dom = sub["dshort"].abs().fillna(0) > sub["dlong"].abs().fillna(0)
        idx = sub.index[short_dom]  # 增空
        if len(idx):
            out.append(pd.DataFrame({"member": m, "trade_date": idx}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["member", "trade_date"])


def main():
    cont_au = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    _, _, cont_ag, md_ag, _ = v46_silver.prep("ag")
    md_ag["member"] = md_ag["member"].replace(EXTRA_ALIAS)
    md_au = member_day_au()

    common = cont_au.index.intersection(cont_ag.index)
    ratio = (cont_au.loc[common, "adj_close"] / cont_ag.loc[common, "adj_close"]).rename("ratio")
    z = ((ratio - ratio.rolling(250).mean()) / ratio.rolling(250).std()).rename("z")

    fwd_au = forward_returns(cont_au).reindex(common)
    fwd_ag = forward_returns(cont_ag).reindex(common)
    ratio_fwd = {h: (ratio.shift(-h) / ratio - 1) for h in (20, 60)}

    print("== A. 金银比均值回归(250日z,2013-2026) ==")
    for lab, mask in [("z > +2(金极贵)", z > 2), ("z +1~+2", (z > 1) & (z <= 2)),
                      ("z -1~-2", (z < -1) & (z >= -2)), ("z < -2(银极贵)", z < -2)]:
        n = mask.sum()
        if n < 20:
            print(f"  {lab}: 样本 {n} 不足")
            continue
        r20 = ratio_fwd[20][mask].dropna()
        r60 = ratio_fwd[60][mask].dropna()
        au20 = fwd_au.loc[mask, 20].dropna()
        ag20 = fwd_ag.loc[mask, 20].dropna()
        print(f"  {lab}: {n}天 | 比值后20日 {r20.mean()*100:+.2f}% 后60日 {r60.mean()*100:+.2f}%"
              f" | AU后20日 {au20.mean()*100:+.2f}% AG后20日 {ag20.mean()*100:+.2f}%")
    print(f"  (全样本基线: AU 20日 {fwd_au[20].mean()*100:+.2f}%, AG 20日 {fwd_ag[20].mean()*100:+.2f}%)")

    print("\n== B. 八家席位 AU-AG 净仓结构 ==")
    for m in GROUP8:
        a = md_au[md_au["member"] == m].set_index("trade_date")["net"].reindex(common)
        g = md_ag[md_ag["member"] == m].set_index("trade_date")["net"].reindex(common)
        both = pd.DataFrame({"au": a, "ag": g}).dropna()
        if len(both) < 100:
            print(f"  {m}: 共同可见日不足({len(both)})")
            continue
        corr = both["au"].corr(both["ag"])
        hedge = ((both["au"] > 0) & (both["ag"] < 0)) | ((both["au"] < 0) & (both["ag"] > 0))
        print(f"  {m}: 共同可见 {len(both)} 日,净仓相关 {corr:+.2f},方向相反(比价/对冲)占 {hedge.mean()*100:.0f}%")

    print("\n== C. 跨品种预测:AG 席位事件 → AU 未来收益 ==")
    ev_ag_long = build_events(md_ag, cont_ag, GROUP8)
    ev_ag_short = neg_events(md_ag, cont_ag, GROUP8)
    for lab, evd in [("AG 增多事件", ev_ag_long), ("AG 增空事件", ev_ag_short)]:
        idx = pd.DatetimeIndex(evd["trade_date"].unique()).intersection(common)
        for h in (10, 20):
            dr = fwd_au.reindex(idx)[h].dropna()
            base = fwd_au[h].mean() * 100
            if len(dr) < 30:
                continue
            t = dr.mean() / dr.std(ddof=1) * np.sqrt(len(dr))
            print(f"  {lab} → AU 后{h}日: N={len(dr)} 均值 {dr.mean()*100:+.2f}%(基线 {base:+.2f}) t={t:+.2f}")

    print("\n== D. 金银比位置对买点质量的调制 ==")
    for name, pkl, fwd_x in [("AU 八家组买点", "final8_au_trades.pkl", None),
                             ("AG 八家组买点", "final8_ag_trades.pkl", None)]:
        tr = pd.read_pickle(aulib.OUT / pkl)
        tr = tr[tr["结果"] != "持有中"].copy()
        tr["z"] = [z.asof(pd.Timestamp(d)) if pd.Timestamp(d) >= z.index[0] else np.nan for d in tr["信号日"]]
        tr = tr.dropna(subset=["z"])
        lo = tr[tr["z"] < 0]
        hi = tr[tr["z"] >= 0]
        print(f"  {name}: 比值低位(z<0)时 {len(lo)} 笔均 {lo['收益%'].mean():+.2f}% | "
              f"高位(z>=0)时 {len(hi)} 笔均 {hi['收益%'].mean():+.2f}%")


if __name__ == "__main__":
    sys.exit(main())
