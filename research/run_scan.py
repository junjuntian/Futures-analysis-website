# -*- coding: utf-8 -*-
"""Task 5:席位海选。295 家全部跑"增多"事件研究 + 训练/验证分离。

留任条件(防假协议):
- 全样本 N>=40 且 t20>=2.5
- 训练窗(<2020)与验证窗(>=2020)h20 方向收益均为正(样本各>=10)
- 2020 后才有数据的席位无法过分割检验,单独列出并标注
"""
import sys

import numpy as np
import pandas as pd

import aulib
from run_profile import forward_returns

pd.set_option("display.width", 220)
SPLIT = pd.Timestamp("2020-01-01")


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    md = pd.read_pickle(aulib.OUT / "member_day.pkl")
    fwd = forward_returns(cont)
    oi = cont["oi_total"]

    rows = []
    for m, s in md.groupby("member"):
        s = s.set_index("trade_date").sort_index()
        if len(s) < 120:
            continue
        flow = (s["dnet"] / oi.reindex(s.index)).dropna()
        thr = flow.abs().rolling(250, min_periods=120).quantile(0.80).shift(1)
        hit = flow[(flow.abs() >= thr) & thr.notna() & (flow != 0)]
        sub = s.loc[hit.index]
        long_dom = sub["dlong"].abs().fillna(0) >= sub["dshort"].abs().fillna(0)
        idx = sub.index[(sub["dnet"] > 0) & long_dom]  # 增多
        if len(idx) < 40:
            continue
        dr = fwd.reindex(idx)[20].dropna()
        if len(dr) < 40:
            continue
        t20 = dr.mean() / dr.std(ddof=1) * np.sqrt(len(dr)) if dr.std(ddof=1) > 0 else np.nan
        tr, te = dr[dr.index < SPLIT], dr[dr.index >= SPLIT]
        rows.append({"席位": m, "N": len(dr), "均值%": dr.mean() * 100, "命中率%": (dr > 0).mean() * 100,
                     "t20": t20, "训练N": len(tr), "训练均值%": tr.mean() * 100 if len(tr) else np.nan,
                     "验证N": len(te), "验证均值%": te.mean() * 100 if len(te) else np.nan})
    df = pd.DataFrame(rows)

    passed = df[(df["t20"] >= 2.5) & (df["训练N"] >= 10) & (df["验证N"] >= 10)
                & (df["训练均值%"] > 0) & (df["验证均值%"] > 0)].sort_values("t20", ascending=False)
    late = df[(df["t20"] >= 2.5) & (df["训练N"] < 10) & (df["验证N"] >= 20)].sort_values("t20", ascending=False)

    print(f"== 参与统计席位数(增多事件 N>=40): {len(df)} ==")
    print("\n== 通过双重检验(全样本 t20>=2.5 且 训练/验证两段均为正) ==")
    print(passed.round(2).to_string(index=False))
    print("\n== 仅 2020 后有数据、无法过分割检验但全样本显著(单独观察) ==")
    print(late.round(2).to_string(index=False))
    print("\n== 运营者八席位在全表中的位置 ==")
    foc = df[df["席位"].isin(aulib.FOCUS)].sort_values("t20", ascending=False)
    print(foc.round(2).to_string(index=False))

    df.to_pickle(aulib.OUT / "scan_all.pkl")
    print("\n已写出 out/scan_all.pkl")


if __name__ == "__main__":
    sys.exit(main())
