# -*- coding: utf-8 -*-
"""玻璃进场水位门 ≤60% 复验(预注册 PLAN_FG_LEVEL_RECHECK_v1)。

DEC-231 的水位门是在**旧出场**(散户反向、无门槛)下测的;DEC-232 把散户出场
门槛提到 2.0 之后没人回头复验。本脚本按预注册跑 G_A~G_E,不扫档位。

**判据在机制上不在总分上**:核心问题是「这道门挡掉的那批单子本身赚不赚钱」。
全样本汇总是事后知情(见预注册第六节),只作背景陈述。
"""
import sys
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
HALF = 2020            # 半样本切点:2013–2019 / 2020–2026


def build(code: str, stem: str) -> dict:
    """把一个品种的材料备齐。**每次都先 use(code)** —— RULES 是全局的。"""
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


def arm(C: dict, code: str, *, lvmax, exit_retail_min="keep"):
    """跑一条臂,回 (trades, 分数仓位日收益)。`lvmax=None` = 去掉水位门。"""
    H.use(code)
    H.RULES["entry_level_max"] = lvmax
    if exit_retail_min != "keep":
        H.RULES["exit_retail_min"] = exit_retail_min
    sig = H.attach_cost_signal(C["raw"], C["seat"], C["mkt"], C["g"])
    tr, pos, dl = H.replay(sig, C["mkt"], C["rdf"], C["op"], C["st"])
    if H.RULES.get("sizing"):
        w = H.sizing_weights(C["raw"]["net"]).reindex(C["idx"])
        dl = H.apply_sizing(dl, pos, w, C["mkt"]["settle"], H.RULES["multiplier"])
    return tr, dl


def perf(dl: pd.Series) -> dict:
    return H._perf(dl)


def sharpe(dl: pd.Series) -> float:
    d = dl.dropna()
    return float(d.mean() / d.std() * np.sqrt(242)) if len(d) > 2 and d.std() > 0 else float("nan")


def extra_of(tr_b, tr_a):
    """B 有而 A 没有的进场 —— 就是这道门挡掉的那批。

    **不是干净的集合差**:去掉门之后早开的一笔会顶掉后面本来会开的一笔,
    所以同时报「A 有而 B 没有」的那批,报告里不许只报一半。
    """
    da = {t["entry_date"] for t in tr_a}
    db = {t["entry_date"] for t in tr_b}
    return ([t for t in tr_b if t["entry_date"] not in da],
            [t for t in tr_a if t["entry_date"] not in db])


def tell(tag, ts):
    if not ts:
        print(f"    {tag:<26}0 笔")
        return None
    r = np.array([t["ret_pct"] for t in ts], dtype=float)
    win = float((r > 0).mean())
    print(f"    {tag:<26}{len(ts):>3} 笔  平均 {r.mean():>+7.2f}%  中位 "
          f"{np.median(r):>+7.2f}%  胜率 {win:>5.0%}")
    return dict(n=len(ts), mean=float(r.mean()), win=win)


C = build("FG", "fg")
print("=" * 82)
print("玻璃进场水位门 ≤60% 复验(PLAN_FG_LEVEL_RECHECK_v1)")
print(f"样本 {C['idx'][0].date()} ~ {C['idx'][-1].date()},{len(C['idx'])} 个交易日")

# ---- 背景:全样本汇总(事后知情,不作闸门)----
tr_a, dl_a = arm(C, "FG", lvmax=0.60)
tr_b, dl_b = arm(C, "FG", lvmax=None)
pa, pb = perf(dl_a), perf(dl_b)
print("\n[背景] 全样本汇总 —— **事后知情,不作判据**")
print(f"  {'臂':<20}{'笔数':>6}{'累计':>10}{'夏普':>7}{'回撤':>9}")
print(f"  {'A 现行(门 ≤60%)':<18}{len(tr_a):>6}{pa['cum_pct']:>+9.1f}%{pa['sharpe']:>7.2f}{pa['max_dd_pct']:>8.1f}%")
print(f"  {'B 去掉水位门':<18}{len(tr_b):>6}{pb['cum_pct']:>+9.1f}%{pb['sharpe']:>7.2f}{pb['max_dd_pct']:>8.1f}%")

# ---- G_A:被门挡掉的那批单子,在现行出场下赚不赚钱 ----
add_new, lost_new = extra_of(tr_b, tr_a)
print("\n[G_A 核心] 门挡掉的那批单子(现行出场:散户门槛 2.0)")
ga = tell("B 多出来的进场", add_new)
tell("(A 有而 B 没有的)", lost_new)
base = np.array([t["ret_pct"] for t in tr_a], dtype=float)
base_win = float((base > 0).mean())
print(f"    对照:现行 {len(tr_a)} 笔胜率 {base_win:.0%}(G_A 要求 ≥ {base_win - 0.10:.0%})")
g_a = bool(ga and ga["mean"] > 0 and ga["win"] >= base_win - 0.10)
print(f"    G_A:平均 {ga['mean'] if ga else float('nan'):+.2f}% > 0 且胜率达标 → "
      f"{'**过**' if g_a else '**不过**'}")

# ---- G_B:同一批单子放回旧出场(散户反向、无门槛)----
tr_a_old, _ = arm(C, "FG", lvmax=0.60, exit_retail_min=None)
tr_b_old, _ = arm(C, "FG", lvmax=None, exit_retail_min=None)
arm(C, "FG", lvmax=0.60, exit_retail_min=2.0)          # 复位
add_old, _ = extra_of(tr_b_old, tr_a_old)
print("\n[G_B 区分解释] 同一道门挡掉的单子,放回**旧出场**(散户反向、无门槛)")
gb = tell("旧出场下多出来的进场", add_old)
g_b = bool(gb and gb["mean"] <= 0)
print(f"    G_B:旧出场下平均 ≤ 0 → {'**过**(是交互翻转)' if g_b else '**不过**(那就是 DEC-231 当初测错了)'}")

# ---- G_C:两个半样本同向 ----
print(f"\n[G_C] 半样本(切点 {HALF} 年)——「去掉门 − 现行」的夏普变化符号要一致")
signs = []
for lo, hi, tag in ((2013, HALF - 1, f"2013–{HALF-1}"), (HALF, 2026, f"{HALF}–2026")):
    ma = dl_a[(dl_a.index.year >= lo) & (dl_a.index.year <= hi)]
    mb = dl_b[(dl_b.index.year >= lo) & (dl_b.index.year <= hi)]
    sa, sb = sharpe(ma), sharpe(mb)
    signs.append(np.sign(sb - sa))
    print(f"  {tag:<12}现行 {sa:>6.2f}   去掉 {sb:>6.2f}   差 {sb - sa:>+6.2f}")
g_c = len(set(signs)) == 1 and signs[0] != 0
print(f"    G_C:{'**过**' if g_c else '**不过**'}")

# ---- G_D:水位窗口 180 → 120 / 240,G_A 的结论不许翻 ----
print("\n[G_D] 换水位窗口(180 → 120 / 240),只看 G_A 的结论翻不翻")
orig_entry_level = H.entry_level
g_d = True
for win in (120, 240):
    H.entry_level = partial(orig_entry_level, win=win)      # 默认参数是绑死的,只能换函数
    ta, _ = arm(C, "FG", lvmax=0.60)
    tb, _ = arm(C, "FG", lvmax=None)
    add, _ = extra_of(tb, ta)
    r = tell(f"窗口 {win} 日 多出来的进场", add)
    ok = bool(r and r["mean"] > 0)
    g_d &= ok
    print(f"    → 平均 > 0:{'是' if ok else '否'}")
H.entry_level = orig_entry_level
print(f"    G_D:{'**过**' if g_d else '**不过**'}")

# ---- G_E:纯碱逐字节不变 ----
print("\n[G_E] 纯碱不该受影响(它本来就没开这道门)")
CS = build("SA", "sa")
ts, dls = arm(CS, "SA", lvmax=None)
ps = perf(dls)
print(f"  纯碱 {len(ts)} 笔  {ps['cum_pct']:+.1f}%  夏普 {ps['sharpe']:.2f}  回撤 {ps['max_dd_pct']:.1f}%")
print("  (对照 DEC-232 记录:22 笔 +116.3% / 1.07 / −19.7%)")
g_e = (len(ts) == 22 and abs(ps["cum_pct"] - 116.3) < 0.05
       and abs(ps["sharpe"] - 1.07) < 0.005)
print(f"    G_E:{'**过**' if g_e else '**不过** —— 停手查 bug'}")

# ---- 2025 单独一栏(运营者能对上盘面的那一年)----
print("\n[参考] 2025 年(样本极小,不作依据)")
for tag, tr in (("现行", tr_a), ("去掉门", tr_b)):
    ts25 = [t for t in tr if t["entry_date"][:4] == "2025"]
    print(f"  {tag:<8}{len(ts25)} 笔" + ("".join(
        f"\n      {t['entry_date']} {t['side']:<5} → {t.get('exit_date')} "
        f"{t['ret_pct']:+.2f}% {t.get('exit_reason')}" for t in ts25) or ""))

# ---- 敏感性:其他档位(**明确不作判据**,只是不许藏着)----
print("\n[敏感性] 其他档位 —— **不作判据**(不许拿它挑一档上线)")
for lv in (0.50, 0.70, 0.80, 0.90):
    t_, d_ = arm(C, "FG", lvmax=lv)
    p_ = perf(d_)
    print(f"  ≤{lv:.0%}  {len(t_):>3} 笔  {p_['cum_pct']:>+8.1f}%  夏普 {p_['sharpe']:>5.2f}  回撤 {p_['max_dd_pct']:>6.1f}%")
arm(C, "FG", lvmax=0.60)

print("\n" + "=" * 82)
print(f"闸门:G_A {'过' if g_a else '不过'} · G_B {'过' if g_b else '不过'} · "
      f"G_C {'过' if g_c else '不过'} · G_D {'过' if g_d else '不过'} · "
      f"G_E {'过' if g_e else '不过'}")
print("处置按预注册第四节:G_A + G_C 过 → 建议 entry_level_max 改回 None;"
      "G_A 不过 → 保留 0.60 关账;G_C/G_D 不过 → 不上,待拍板。")
