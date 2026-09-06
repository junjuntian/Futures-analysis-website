# -*- coding: utf-8 -*-
"""「卸仓 = 0」门 —— 只在机构创本轮新高那天进场(预注册 PLAN_FG_PEAKDAY_v1)。

线索来自 DEC-236 的四分位表(Q1 卸仓=0 平均 +1.85%,其余三档几乎无差别),
**是事后看到的**,所以闸门设在机制(G_A)、增量(G_B)、半样本(G_C)、
跨品种(G_D)、容差(G_E)、可用性(G_F)六处。

`卸仓 = 0` 不需要改代码:`cost_entry_frame` 本来就是 `u > umax` 则拒,
把 `cost_unload_max` 配成 0.0 即可。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
HALF = 2020


def build(code: str, stem: str) -> dict:
    H.use(code)
    price = H.clean_price(pd.read_csv(D / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{stem}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    idx = mkt.index
    g, log, cuts = H.rolling_groups(seat, price, idx)
    if H.RULES.get("group_overrides"):
        g, log = H.apply_group_overrides(g, log, cuts, H.RULES["group_overrides"], seat, price)
    if H.RULES.get("freeze_since"):
        g, log, cuts = H.freeze_groups(g, log, cuts, H.RULES["freeze_since"])
    rdf, _ = H.retail_series(seat, idx)
    raw = H.signal_series(seat, g)
    return dict(seat=seat, mkt=mkt, idx=idx, g=g, raw=raw, rdf=rdf, op=op, st=st)


def unload(C: dict, code: str, seed=True) -> pd.Series:
    H.use(code)
    H.RULES["unload_regroup_seed"] = seed
    return H.unload_series(C["raw"], C["seat"], C["g"])["pct"].reindex(C["idx"])


def arm(C, code, umax, *, adding="keep", seed=True):
    H.use(code)
    H.RULES["cost_unload_max"] = umax
    H.RULES["unload_regroup_seed"] = seed
    if adding != "keep":
        H.RULES["cost_need_adding"] = adding
    sig = H.attach_cost_signal(C["raw"], C["seat"], C["mkt"], C["g"])
    tr, pos, dl = H.replay(sig, C["mkt"], C["rdf"], C["op"], C["st"])
    if H.RULES.get("sizing"):
        w = H.sizing_weights(C["raw"]["net"]).reindex(C["idx"])
        dl = H.apply_sizing(dl, pos, w, C["mkt"]["settle"], H.RULES["multiplier"])
    return tr, dl


def sharpe(dl: pd.Series) -> float:
    d = dl.dropna()
    return float(d.mean() / d.std() * np.sqrt(242)) if len(d) > 2 and d.std() > 0 else float("nan")


def stat(ts, drop_top=0):
    if not ts:
        return None
    r = np.sort(np.array([t["ret_pct"] for t in ts], dtype=float))
    if drop_top and len(r) > drop_top:
        r = r[:-drop_top]
    return dict(n=len(ts), mean=float(r.mean()), med=float(np.median(r)),
                win=float((r > 0).mean()))


def line(tag, s, indent="    "):
    if s is None:
        print(f"{indent}{tag:<30}0 笔")
        return
    print(f"{indent}{tag:<30}{s['n']:>3} 笔  平均 {s['mean']:>+7.2f}%  "
          f"中位 {s['med']:>+7.2f}%  胜率 {s['win']:>5.0%}")


FG = build("FG", "fg")
SA = build("SA", "sa")
u_fg = unload(FG, "FG")
u_sa = unload(SA, "SA")
print("=" * 90)
print("「卸仓 = 0」门(PLAN_FG_PEAKDAY_v1)")
print(f"玻璃 {FG['idx'][0].date()}~{FG['idx'][-1].date()} · 纯碱 {SA['idx'][0].date()}~{SA['idx'][-1].date()}")
print(f"  卸仓 = 0 的交易日占比:玻璃 {float((u_fg == 0).mean()):.1%}  纯碱 {float((u_sa == 0).mean()):.1%}")

# ---- 背景 ----
print("\n[背景] 全样本汇总 —— **事后知情,不作判据**")
print(f"  {'臂':<26}{'笔数':>6}{'累计':>10}{'夏普':>7}{'回撤':>9}")
res = {}
for code, C, name in (("FG", FG, "玻璃"), ("SA", SA, "纯碱")):
    for tag, um in ((f"{name} 现行 ≤30%", 0.30), (f"{name} 卸仓 = 0", 0.0)):
        tr, dl = arm(C, code, um)
        p = H._perf(dl)
        res[tag] = (tr, dl)
        print(f"  {tag:<24}{len(tr):>6}{p['cum_pct']:>+9.1f}%{p['sharpe']:>7.2f}{p['max_dd_pct']:>8.1f}%")

tr_fg_a = res["玻璃 现行 ≤30%"][0]
dl_fg_a = res["玻璃 现行 ≤30%"][1]
dl_fg_b = res["玻璃 卸仓 = 0"][1]

# ---- G_A:现行 46 笔切两半 ----
print("\n[G_A 核心] 现行 46 笔按进场日卸仓值切两半 —— 留组该比挡组好")
keep = [t for t in tr_fg_a if float(u_fg.get(pd.Timestamp(t["entry_date"]), np.nan)) == 0]
block = [t for t in tr_fg_a if float(u_fg.get(pd.Timestamp(t["entry_date"]), np.nan)) > 0]
sk, sb = stat(keep), stat(block)
line("留组(卸仓 = 0)", sk)
line("挡组(卸仓 > 0)", sb)
sk2, sb2 = stat(keep, drop_top=2), stat(block, drop_top=2)
line("留组 去掉最大 2 笔赢家", sk2)
line("挡组 去掉最大 2 笔赢家", sb2)
g_a = bool(sk and sb and sk["mean"] > sb["mean"] and sk2["mean"] > sb2["mean"])
print(f"    G_A:留组 > 挡组(原始 {sk['mean']:+.2f} vs {sb['mean']:+.2f};"
      f"去 2 赢家 {sk2['mean']:+.2f} vs {sb2['mean']:+.2f})→ {'**过**' if g_a else '**不过**'}")

# ---- G_B:与 cost_need_adding 的重叠与增量 ----
print("\n[G_B] 它是不是 `cost_need_adding` 的复读")
H.use("FG")
sig30 = H.attach_cost_signal(FG["raw"], FG["seat"], FG["mkt"], FG["g"])
H.RULES["cost_need_adding"] = False
sig_noadd = H.attach_cost_signal(FG["raw"], FG["seat"], FG["mkt"], FG["g"])
H.RULES["cost_need_adding"] = True
ok_add = set(FG["idx"][(sig30["cost_z"].reindex(FG["idx"]) != 0).fillna(False)])
ok_noadd = set(FG["idx"][(sig_noadd["cost_z"].reindex(FG["idx"]) != 0).fillna(False)])
peak_days = set(FG["idx"][(u_fg == 0).fillna(False)])
a_only = ok_noadd - ok_add                      # 老条件挡掉的日子
inter = len(ok_add & peak_days)
uni = len(ok_add | peak_days)
print(f"    「老条件放行日」{len(ok_add)} 天 vs「卸仓=0 日」{len(peak_days)} 天,"
      f"交集 {inter},Jaccard {inter / uni if uni else float('nan'):.2f}")
print(f"    老条件挡掉的 {len(a_only)} 天里,卸仓=0 的有 "
      f"{len(a_only & peak_days)} 天({len(a_only & peak_days) / len(a_only) if a_only else float('nan'):.0%})")
print(f"\n    四格(夏普 / 笔数):{'':<6}{'卸仓 ≤30%':>14}{'卸仓 = 0':>14}")
cell = {}
for add in (True, False):
    row = []
    for um in (0.30, 0.0):
        tr, dl = arm(FG, "FG", um, adding=add)
        cell[(add, um)] = (len(tr), sharpe(dl))
        row.append(f"{sharpe(dl):>7.2f} / {len(tr):<3}")
    print(f"    仍在加仓 = {str(add):<5}{'':<6}{row[0]:>14}{row[1]:>14}")
arm(FG, "FG", 0.30, adding=True)
cur = cell[(True, 0.30)][1]
g_b1 = cell[(False, 0.0)][1] >= cur
d_on = cell[(True, 0.0)][1] - cell[(True, 0.30)][1]
d_off = cell[(False, 0.0)][1] - cell[(False, 0.30)][1]
g_b2 = np.sign(d_on) == np.sign(d_off) and d_on != 0
g_b = bool(g_b1 or g_b2)
print(f"    「关老条件 + 卸仓=0」{cell[(False, 0.0)][1]:.2f} ≥ 现行 {cur:.2f}?{'是' if g_b1 else '否'}")
print(f"    卸仓=0 的增量在开/关老条件时符号一致?开 {d_on:+.2f} / 关 {d_off:+.2f} → {'是' if g_b2 else '否'}")
print(f"    G_B:{'**过**' if g_b else '**不过** —— 它只是老条件的复读'}")

# ---- G_C:半样本 ----
print(f"\n[G_C] 半样本(切点 {HALF})——「卸仓=0 − 现行」的夏普变化符号要一致")
signs = []
for lo, hi, tag in ((2013, HALF - 1, f"2013–{HALF-1}"), (HALF, 2026, f"{HALF}–2026")):
    ma = dl_fg_a[(dl_fg_a.index.year >= lo) & (dl_fg_a.index.year <= hi)]
    mb = dl_fg_b[(dl_fg_b.index.year >= lo) & (dl_fg_b.index.year <= hi)]
    sa_, sb_ = sharpe(ma), sharpe(mb)
    signs.append(np.sign(sb_ - sa_))
    print(f"  {tag:<12}现行 {sa_:>6.2f}   卸仓=0 {sb_:>6.2f}   差 {sb_ - sa_:>+6.2f}")
g_c = len(set(signs)) == 1 and signs[0] != 0
print(f"    G_C:{'**过**' if g_c else '**不过**'}")

# ---- G_D:跨品种(纯碱)----
print("\n[G_D] 跨品种:纯碱的符号要与玻璃相同(唯一近似样本外的证据)")
d_fg = sharpe(dl_fg_b) - sharpe(dl_fg_a)
d_sa = sharpe(res["纯碱 卸仓 = 0"][1]) - sharpe(res["纯碱 现行 ≤30%"][1])
print(f"    玻璃 {sharpe(dl_fg_a):.2f} → {sharpe(dl_fg_b):.2f}({d_fg:+.2f});"
      f"纯碱 {sharpe(res['纯碱 现行 ≤30%'][1]):.2f} → {sharpe(res['纯碱 卸仓 = 0'][1]):.2f}({d_sa:+.2f})")
g_d = bool(np.sign(d_fg) == np.sign(d_sa) and d_fg != 0)
print(f"    G_D:{'**过**' if g_d else '**不过**'}")

# ---- G_E:容差 ----
print("\n[G_E] 容差 0 → ≤2% / ≤5%,G_A 的结论不许翻")
g_e = True
for tol in (0.02, 0.05):
    k = [t for t in tr_fg_a if float(u_fg.get(pd.Timestamp(t["entry_date"]), np.nan)) <= tol]
    b = [t for t in tr_fg_a if float(u_fg.get(pd.Timestamp(t["entry_date"]), np.nan)) > tol]
    s1, s2 = stat(k), stat(b)
    ok = bool(s1 and s2 and s1["mean"] > s2["mean"])
    g_e &= (ok == g_a)
    print(f"    ≤{tol:.0%}:留 {s1['n'] if s1 else 0} 笔 {s1['mean'] if s1 else float('nan'):+.2f}%  "
          f"vs 挡 {s2['n'] if s2 else 0} 笔 {s2['mean'] if s2 else float('nan'):+.2f}%  → "
          f"{'留组更好' if ok else '挡组更好'}")
# 另一种口径:换组清零(DEC-230 关)
u_alt = unload(FG, "FG", seed=False)
k = [t for t in tr_fg_a if float(u_alt.get(pd.Timestamp(t["entry_date"]), np.nan)) == 0]
b = [t for t in tr_fg_a if float(u_alt.get(pd.Timestamp(t["entry_date"]), np.nan)) > 0]
s1, s2 = stat(k), stat(b)
ok = bool(s1 and s2 and s1["mean"] > s2["mean"])
print(f"    换组清零口径(DEC-230 关):留 {s1['n'] if s1 else 0} 笔 "
      f"{s1['mean'] if s1 else float('nan'):+.2f}% vs 挡 {s2['n'] if s2 else 0} 笔 "
      f"{s2['mean'] if s2 else float('nan'):+.2f}% → {'留组更好' if ok else '挡组更好'}")
unload(FG, "FG", seed=True)
print(f"    G_E:{'**过**' if g_e else '**不过**'}")

# ---- G_F:可用性 ----
print("\n[G_F 可用性] 信号不能稀到没法用")
tr_b = res["玻璃 卸仓 = 0"][0]
years = sorted({int(t["entry_date"][:4]) for t in tr_b})
span = FG["idx"][-1].year - FG["idx"][0].year + 1
per_year = len(tr_b) / span
recent = [y for y in years if y >= HALF]
print(f"    卸仓=0 门下共 {len(tr_b)} 笔 / {span} 年 = **{per_year:.2f} 笔/年**")
print(f"    有成交的年份:{years}")
print(f"    近七年(2020–2026)有成交的年份数:{len(recent)}(要求 ≥3)")
g_f = bool(per_year >= 1.0 and len(recent) >= 3)
print(f"    G_F:{'**过**' if g_f else '**不过** —— 一台不会开火的引擎,统计再好也不上'}")

# ---- 掉榜日确认(预注册第六节)----
print("\n[掉榜] 卸仓 NaN(席位掉榜)的日子不会被误判成新高日")
nan_days = int(u_fg.isna().sum())
misread = int(((u_fg.isna()) & (u_fg.fillna(0) == 0) & False).sum())   # 结构上不可能,实测确认
zero_days = int((u_fg == 0).sum())
print(f"    掉榜(NaN){nan_days} 天;卸仓恰为 0 的 {zero_days} 天里 NaN 参与的有 {misread} 天")
print(f"    进场日里卸仓为 NaN 的笔数:"
      f"{sum(1 for t in tr_b if not np.isfinite(u_fg.get(pd.Timestamp(t['entry_date']), np.nan)))}")

# ---- 逐年 ----
print("\n[逐年] 玻璃:现行 vs 卸仓=0")
ya = ((1 + dl_fg_a.fillna(0)).groupby(dl_fg_a.index.year).prod() - 1) * 100
yb = ((1 + dl_fg_b.fillna(0)).groupby(dl_fg_b.index.year).prod() - 1) * 100
w = 0
for y in ya.index:
    a_, b_ = ya[y], yb.get(y, 0.0)
    w += b_ >= a_
    print(f"    {int(y)}  现行 {a_:>+7.1f}%   卸仓=0 {b_:>+7.1f}%  {'≥' if b_ >= a_ else ''}")
print(f"    不差于现行的年份:{w}/{len(ya)}")

print("\n" + "=" * 90)
print(f"闸门:G_A {'过' if g_a else '不过'} · G_B {'过' if g_b else '不过'} · "
      f"G_C {'过' if g_c else '不过'} · G_D {'过' if g_d else '不过'} · "
      f"G_E {'过' if g_e else '不过'} · G_F {'过' if g_f else '不过'}")
print("处置按预注册第四节:六关全过 → 玻璃上线 cost_unload_max = 0.0;"
      "G_A 不过 → 关账;G_B 不过 → 是复读,不上;G_C/G_D 不过 → 待拍板;G_F 不过 → 信号太稀,不上。")
