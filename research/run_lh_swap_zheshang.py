# -*- coding: utf-8 -*-
"""生猪:把浙商换成候选,用引擎现行策略全流程回放(2026-08-31 运营者要看候选)。

框架与 run_lh_fixed_groups.py 一字不差(DEC-122 当时就是这么比的),只换第五人。
**结论怎么读**:候选是按 2026 年的规模+盈亏挑的,再拿 2026 年的回放去评它们,
天然占便宜 —— 所以下面同时报**全样本**与**近一年**,只有两段都不输现状的才值得考虑。
跑法:python research/run_lh_swap_zheshang.py
"""
import io
import pathlib
import sys

import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H

D = pathlib.Path("research/data")
OUT = pathlib.Path("research/out")
price = H.clean_price(pd.read_csv(D / "lh_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "lh_seat.csv.gz"))
H.use("LH")
H.CURRENT = {"code": "LH", **H.VARIETIES["LH"]}
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
rdf, _ = H.retail_series(seat, mkt.index)

BASE4 = ("国泰君安", "东证期货", "东吴期货", "永安期货")
CANDIDATES = ["浙商期货", "国投期货", "中粮期货", "格林大华", "南华期货", "中州期货", "创元期货"]


def run(members):
    groups = pd.Series([tuple(members)] * len(mkt.index), index=mkt.index, dtype=object)
    sig = H.signal_series(seat, groups)
    if H.RULES["signal_source"] == "cost":
        sig = H.attach_cost_signal(sig, seat, mkt, groups)
    if H.RULES["exit_mode"] == "inst":
        sig = H.attach_inst_exit(sig, seat, mkt, groups)
    if H.RULES["long_mode"] == "unload_bounce":
        sig = H.attach_bounce_long(sig, seat, mkt, groups)
    trades, _, daily = H.replay(sig, mkt, rdf, op, st)
    out = {}
    for name, lo in (("全样本", None), ("近一年", pd.Timestamp("2025-08-22"))):
        d = daily if lo is None else daily[daily.index >= lo]
        p = H._perf(d)
        tr = [t for t in trades if t.get("exit_date") and (lo is None or pd.Timestamp(t["exit_date"]) >= lo)]
        wins = [t for t in tr if (t.get("ret_pct") or 0) > 0]
        out[name] = dict(cum=p["cum_pct"], sharpe=p["sharpe"], dd=p["max_dd_pct"],
                         n=len(tr), win=round(len(wins) / len(tr) * 100) if tr else 0)
    return out


L = ["生猪第五人替换回放(前四家固定:国泰君安/东证/东吴/永安)", ""]
L.append(f"{'第五人':<8}{'全样本累计':>10}{'夏普':>7}{'回撤':>8}{'笔数':>5} | {'近一年累计':>10}{'夏普':>7}{'回撤':>8}{'笔数':>5}{'胜率':>6}")
L.append("-" * 84)
results = {}
for m in CANDIDATES:
    r = run(list(BASE4) + [m])
    results[m] = r
    a, b = r["全样本"], r["近一年"]
    tag = "  ← 现状" if m == "浙商期货" else ""
    L.append(f"{m:<8}{a['cum']:>+10.1f}{a['sharpe']:>7.2f}{a['dd']:>8.1f}{a['n']:>5}"
             f" | {b['cum']:>+10.1f}{b['sharpe']:>7.2f}{b['dd']:>8.1f}{b['n']:>5}{b['win']:>5}%{tag}")

base = results["浙商期货"]
L.append("")
L.append("与现状(浙商)相比 —— 两段都不输才算候选:")
for m, r in results.items():
    if m == "浙商期货":
        continue
    d_all = r["全样本"]["sharpe"] - base["全样本"]["sharpe"]
    d_yr = r["近一年"]["sharpe"] - base["近一年"]["sharpe"]
    verdict = "**两段都更好**" if d_all > 0 and d_yr > 0 else ("两段都更差" if d_all < 0 and d_yr < 0 else "一好一坏,不构成理由")
    L.append(f"  {m:<8} 夏普差 全样本 {d_all:+.2f} / 近一年 {d_yr:+.2f}  → {verdict}")
io.open(OUT / "lh_swap.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
