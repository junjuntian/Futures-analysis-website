# -*- coding: utf-8 -*-
"""生猪席位组重扫(2026-08-31 运营者:浙商都没持仓了,为什么在名单里)。

**这次加两条当初没看的维度** —— 浙商正是栽在这两条上:
  1. **可用性**:上榜率。它 2026 年只有 52% 的交易日在榜,信号一半时间是断的;
  2. **影响力**:净持仓规模。它中位 612 手,而国泰君安/永安是 1.8~2.0 万手,
     差 30 倍 —— 占合计的 1.8%,在与不在几乎不改变信号。
当初(DEC-122)按择时收益前 5 选,这两条都没进筛子。

判定不靠单一排名(IH 那轮的教训:事后挑最强 = 选择偏差),每家同时报
2025 与 2026 两段,一致才算数。跑法:cd research && PYTHONPATH=. python run_lh_seat_rescan.py
"""
import io
import pathlib

import numpy as np
import pandas as pd
import run_lh_phase1 as P

OUT = pathlib.Path(__file__).resolve().parent / "out"
CURRENT = ["国泰君安", "东证期货", "东吴期货", "永安期货", "浙商期货"]

df, main = P.build()
alpha_all = P.seat_alpha(df)
days_all = main.index
y2025 = days_all[(days_all >= "2025-01-01") & (days_all < "2026-01-01")]
y2026 = days_all[days_all >= "2026-01-01"]

rows = []
for m, g in df.groupby("member_key"):
    by_day = g.groupby("trade_date")["net"].sum()
    on25 = by_day.index.isin(y2025).sum()
    on26 = by_day.index.isin(y2026).sum()
    recent = by_day[by_day.index >= "2026-01-01"]
    if m not in alpha_all.index:
        continue
    a = alpha_all.loc[m]
    rows.append({
        "m": m,
        "pnl": float(a.get("pnl", np.nan)),
        "alpha": float(a.get("alpha", np.nan)),
        "size26": float(recent.abs().median()) if len(recent) else 0.0,
        "on25": on25 / max(len(y2025), 1) * 100,
        "on26": on26 / max(len(y2026), 1) * 100,
        "last": by_day.index.max().date() if len(by_day) else None,
    })
t = pd.DataFrame(rows).set_index("m")

L = ["生猪席位重扫(数据至 %s)" % days_all[-1].date(), ""]
L.append("现有五家(DEC-122 运营者拍板固定):")
for m in CURRENT:
    if m in t.index:
        r = t.loc[m]
        L.append(f"  {m:<6} 上榜 2025:{r.on25:>3.0f}% 2026:{r.on26:>3.0f}%  "
                 f"2026中位持仓 {r.size26:>7,.0f} 手  总盈亏 {r.pnl:>+7.2f}亿  择时 {r.alpha:>+6.2f}亿")
L.append("")

# 候选筛:2026 年上榜率 ≥80%(信号不能断)且 持仓中位 ≥2000 手(要有影响力)
cand = t[(t.on26 >= 80) & (t.size26 >= 2000)].copy()
cand["rank_sum"] = cand["pnl"].rank(ascending=False) + cand["size26"].rank(ascending=False)
L.append(f"候选筛(2026 上榜率≥80% 且 2026 中位持仓≥2000 手):{len(cand)} 家")
L.append(f"{'席位':<8}{'上榜25':>7}{'上榜26':>7}{'持仓26':>9}{'总盈亏':>9}{'择时':>8}  在册?")
L.append("-" * 62)
for m, r in cand.sort_values("rank_sum").head(15).iterrows():
    tag = "★现有" if m in CURRENT else ""
    L.append(f"{m:<8}{r.on25:>6.0f}%{r.on26:>6.0f}%{r.size26:>9,.0f}{r.pnl:>+9.2f}{r.alpha:>+8.2f}  {tag}")
L.append("")
L.append("对照:浙商期货 " + (f"上榜 2026 仅 {t.loc['浙商期货'].on26:.0f}%、持仓中位 {t.loc['浙商期货'].size26:,.0f} 手 → 两条筛子都不过"
                            if "浙商期货" in t.index else "无数据"))
io.open(OUT / "lh_seat_rescan.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
