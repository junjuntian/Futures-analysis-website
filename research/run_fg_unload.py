# -*- coding: utf-8 -*-
"""玻璃卸仓门 30% 复验(预注册 PLAN_FG_UNLOAD_v1)。

`cost_unload_max = 0.30` 是**全局默认值**,出处 DEC-127 只测了生猪,
原文自称「曲面不单调、噪音级」。运营者:生猪趋势简单、玻璃震荡偏多,照搬会有问题。

**门槛由分布算,不由回测挑**(见预注册第三节)。主判据在机制上,不在总分上 ——
上一个项目 FG_LEVEL_RECHECK 刚因「汇总翻倍、拆到单笔是三笔赢家」被否。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
HALF = 2020
NOISE_Q = 0.75          # H_次:噪音带分位,事前写死


def build(code: str, stem: str) -> dict:
    """备料。**每次都先 use(code)** —— RULES 是全局的。生猪走 fixed_groups。"""
    H.use(code)
    price = H.clean_price(pd.read_csv(D / f"{stem}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{stem}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    idx = mkt.index
    if H.RULES.get("fixed_members"):
        g, log, cuts = H.fixed_groups(H.RULES["fixed_members"], seat, price, idx, "2026-08-23")
    else:
        g, log, cuts = H.rolling_groups(seat, price, idx)
        if H.RULES.get("group_overrides"):
            g, log = H.apply_group_overrides(g, log, cuts, H.RULES["group_overrides"], seat, price)
        if H.RULES.get("freeze_since"):
            g, log, cuts = H.freeze_groups(g, log, cuts, H.RULES["freeze_since"])
    rdf, _ = H.retail_series(seat, idx)
    raw = H.signal_series(seat, g)
    unl = H.unload_series(raw, seat, g)["pct"].reindex(idx)
    return dict(seat=seat, mkt=mkt, idx=idx, g=g, raw=raw, rdf=rdf, op=op, st=st, unl=unl)


def arm(C: dict, code: str, umax: float, *, seed="keep"):
    H.use(code)
    H.RULES["cost_unload_max"] = umax
    if seed != "keep":
        H.RULES["unload_regroup_seed"] = seed
    sig = H.attach_cost_signal(C["raw"], C["seat"], C["mkt"], C["g"])
    tr, pos, dl = H.replay(sig, C["mkt"], C["rdf"], C["op"], C["st"])
    if H.RULES.get("sizing"):
        w = H.sizing_weights(C["raw"]["net"]).reindex(C["idx"])
        dl = H.apply_sizing(dl, pos, w, C["mkt"]["settle"], H.RULES["multiplier"])
    return tr, dl


def sharpe(dl: pd.Series) -> float:
    d = dl.dropna()
    return float(d.mean() / d.std() * np.sqrt(242)) if len(d) > 2 and d.std() > 0 else float("nan")


def tell(tag, ts, indent="    "):
    if not ts:
        print(f"{indent}{tag:<28}0 笔")
        return None
    r = np.array([t["ret_pct"] for t in ts], dtype=float)
    top3 = np.sort(r)[:-3].mean() if len(r) > 3 else float("nan")
    print(f"{indent}{tag:<28}{len(ts):>3} 笔  平均 {r.mean():>+7.2f}%  中位 {np.median(r):>+7.2f}%  "
          f"胜率 {float((r > 0).mean()):>5.0%}  去前三赢家 {top3:>+7.2f}%")
    return dict(n=len(ts), mean=float(r.mean()), win=float((r > 0).mean()))


def vr(ret: pd.Series, q=20, base=5) -> float:
    """方差比:q 日收益方差 /(q/base × base 日收益方差)。>1 偏趋势,<1 偏震荡。"""
    r = ret.dropna()
    a = r.rolling(base).sum().dropna().var()
    b = r.rolling(q).sum().dropna().var()
    return float(b / (a * q / base)) if a > 0 else float("nan")


FG = build("FG", "fg")
LH = build("LH", "lh")
print("=" * 88)
print("玻璃卸仓门 30% 复验(PLAN_FG_UNLOAD_v1)")
print(f"玻璃 {FG['idx'][0].date()}~{FG['idx'][-1].date()} {len(FG['idx'])} 日 · "
      f"生猪 {LH['idx'][0].date()}~{LH['idx'][-1].date()} {len(LH['idx'])} 日")

# ---- G_F:先验运营者的前提 ----
print("\n[G_F 前提检验] 玻璃是不是真比生猪更震荡(描述性,不作否决)")
print(f"  {'':<10}{'卸仓中位':>10}{'四分位':>16}{'>30% 天数占比':>14}{'方向持续中位':>14}{'方差比 VR':>11}")
for tag, C in (("玻璃", FG), ("生猪", LH)):
    u = C["unl"].dropna()
    side = np.sign(C["raw"]["net"].reindex(C["idx"]))
    runs, cur, prev = [], 0, None
    for v in side:
        if not np.isfinite(v) or v == 0:
            continue
        if prev is not None and v != prev:
            runs.append(cur)
            cur = 0
        cur += 1
        prev = v
    runs.append(cur)
    v_ = vr(C["mkt"]["ret_open"])
    print(f"  {tag:<10}{u.median():>10.0%}{f'{u.quantile(.25):.0%}~{u.quantile(.75):.0%}':>16}"
          f"{float((u > 0.30).mean()):>14.0%}{np.median(runs):>13.0f} 日{v_:>11.2f}")
# 均值回复:卸仓涨过 30% 之后 20 日内又回到 30% 以下的比例
for tag, C in (("玻璃", FG), ("生猪", LH)):
    u = C["unl"]
    over = (u > 0.30) & (u.shift(1) <= 0.30)
    back = [(u.iloc[i + 1:i + 21] <= 0.30).any() for i in np.flatnonzero(over.fillna(False).values)
            if i + 1 < len(u)]
    print(f"  {tag}:卸仓上穿 30% 共 {len(back)} 次,其中 20 日内又跌回 30% 以下的占 "
          f"{np.mean(back) if back else float('nan'):.0%}")

# ---- 门槛:算出来,不许挑 ----
u_lh = LH["unl"].dropna()
u_fg = FG["unl"].dropna()
P = float((u_lh <= 0.30).mean())
X = float(u_fg.quantile(P))
side_fg = np.sign(FG["raw"]["net"].reindex(FG["idx"]))
same = side_fg.eq(side_fg.shift(1)).fillna(False)
X2 = float(FG["unl"][same].dropna().quantile(NOISE_Q))
print(f"\n[门槛] H_主 分位映射:生猪 0.30 位于自身卸仓分布的第 {P:.1%} 分位 → "
      f"玻璃同分位 X = **{X:.1%}**")
print(f"       H_次 噪音带:玻璃「机构方向未变期间」卸仓的第 {NOISE_Q:.0%} 分位 X2 = **{X2:.1%}**")

# ---- 背景汇总(事后知情,不作判据)----
print("\n[背景] 全样本汇总 —— **事后知情,不作判据**")
print(f"  {'臂':<22}{'笔数':>6}{'累计':>10}{'夏普':>7}{'回撤':>9}")
res = {}
for tag, um in (("现行 30%", 0.30), (f"H_主 X={X:.0%}", X), (f"H_次 X2={X2:.0%}", X2)):
    tr, dl = arm(FG, "FG", um)
    p = H._perf(dl)
    res[tag] = (tr, dl, um)
    print(f"  {tag:<20}{len(tr):>6}{p['cum_pct']:>+9.1f}%{p['sharpe']:>7.2f}{p['max_dd_pct']:>8.1f}%")

tr_a, dl_a, _ = res["现行 30%"]
base_win = float(np.mean([t["ret_pct"] > 0 for t in tr_a]))

# ---- G_A:放宽后多出来的单子本身赚不赚钱 ----
print(f"\n[G_A 核心] 放宽之后多出来的单子(对照:现行 {len(tr_a)} 笔胜率 {base_win:.0%},"
      f"要求 ≥ {base_win - 0.10:.0%})")
ga = {}
for tag in (f"H_主 X={X:.0%}", f"H_次 X2={X2:.0%}"):
    trb = res[tag][0]
    da = {t["entry_date"] for t in tr_a}
    add = [t for t in trb if t["entry_date"] not in da]
    lost = [t for t in tr_a if t["entry_date"] not in {t2["entry_date"] for t2 in trb}]
    r = tell(f"{tag} 多出来", add)
    tell(f"{tag} 反而消失的", lost)
    ga[tag] = bool(r and r["mean"] > 0 and r["win"] >= base_win - 0.10)
g_a = ga[f"H_主 X={X:.0%}"]
print(f"    G_A(代表格 H_主):{'**过**' if g_a else '**不过**'}")

# ---- G_B:卸仓比例这个变量本身有没有信息 ----
print("\n[G_B] 把门全开(100%),按进场日的卸仓比例切四分位 —— 低卸仓该比高卸仓赚")
tr_open, _ = arm(FG, "FG", 1.0)
rows = [(float(FG["unl"].get(pd.Timestamp(t["entry_date"]), np.nan)), t["ret_pct"])
        for t in tr_open]
df = pd.DataFrame(rows, columns=["unl", "ret"]).dropna()
df["q"] = pd.qcut(df["unl"], 4, labels=["Q1 最低", "Q2", "Q3", "Q4 最高"])
for q, gsub in df.groupby("q", observed=True):
    print(f"    {q!s:<10}卸仓 {gsub['unl'].min():>4.0%}~{gsub['unl'].max():>4.0%}  "
          f"{len(gsub):>3} 笔  平均 {gsub['ret'].mean():>+7.2f}%  胜率 {float((gsub['ret'] > 0).mean()):>5.0%}")
# 本机没有 scipy(仓里也一贯手写,见 research/statlib.py),秩相关自己算。
rho = float(df["unl"].rank().corr(df["ret"].rank()))
lo = df[df["q"] == "Q1 最低"]["ret"].mean()
hi = df[df["q"] == "Q4 最高"]["ret"].mean()
g_b = bool(lo > hi and rho < 0)
print(f"    Spearman(卸仓, 收益) = {rho:+.3f};最低组 {lo:+.2f}% vs 最高组 {hi:+.2f}%")
print(f"    G_B:{'**过**' if g_b else '**不过** —— 卸仓比例对玻璃没有信息'}")

# ---- G_C:半样本同向 ----
print(f"\n[G_C] 半样本(切点 {HALF})——「X − 现行」的夏普变化符号要一致")
dl_x = res[f"H_主 X={X:.0%}"][1]
signs = []
for lo_y, hi_y, tag in ((2013, HALF - 1, f"2013–{HALF-1}"), (HALF, 2026, f"{HALF}–2026")):
    ma = dl_a[(dl_a.index.year >= lo_y) & (dl_a.index.year <= hi_y)]
    mb = dl_x[(dl_x.index.year >= lo_y) & (dl_x.index.year <= hi_y)]
    sa, sb = sharpe(ma), sharpe(mb)
    signs.append(np.sign(sb - sa))
    print(f"  {tag:<12}现行 {sa:>6.2f}   X {sb:>6.2f}   差 {sb - sa:>+6.2f}")
g_c = len(set(signs)) == 1 and signs[0] != 0
print(f"    G_C:{'**过**' if g_c else '**不过**'}")

# ---- G_D:换组不清零口径开/关,G_A 不许翻 ----
print("\n[G_D] `unload_regroup_seed`(DEC-230)开/关,G_A 的结论不许翻")
g_d = True
for seed in (True, False):
    ta, _ = arm(FG, "FG", 0.30, seed=seed)
    tb, _ = arm(FG, "FG", X, seed=seed)
    da = {t["entry_date"] for t in ta}
    r = tell(f"seed={seed} 多出来", [t for t in tb if t["entry_date"] not in da])
    bw = float(np.mean([t["ret_pct"] > 0 for t in ta]))
    ok = bool(r and r["mean"] > 0 and r["win"] >= bw - 0.10)
    g_d &= (ok == g_a)
    print(f"    → G_A 在此口径下:{'过' if ok else '不过'}")
arm(FG, "FG", 0.30, seed=True)
print(f"    G_D:{'**过**' if g_d else '**不过**'}")

# ---- G_E:别的品种一字不动 ----
print("\n[G_E] 其他品种逐字节不变(焦煤尤其 —— 它的 inst 出场也读这个键)")
g_e = True
for code, stem, want in (("SA", "sa", (22, 116.3, 1.07)), ("JM", "jm", None),
                         ("LH", "lh", None), ("JD", "jd", None), ("I", "i", None)):
    C2 = build(code, stem)
    H.use(code)
    assert H.RULES["cost_unload_max"] == 0.30, f"{code} 的全局默认值被动过了"
    sig = C2["raw"]
    if H.RULES["signal_source"] == "cost":
        sig = H.attach_cost_signal(sig, C2["seat"], C2["mkt"], C2["g"])
    if H.RULES["exit_mode"] == "inst":
        sig = H.attach_inst_exit(sig, C2["seat"], C2["mkt"], C2["g"])
    if H.RULES.get("long_mode") == "unload_bounce":
        sig = H.attach_bounce_long(sig, C2["seat"], C2["mkt"], C2["g"])
    tr, pos, dl = H.replay(sig, C2["mkt"], C2["rdf"], C2["op"], C2["st"])
    if H.RULES.get("sizing"):
        w = H.sizing_weights(C2["raw"]["net"]).reindex(C2["idx"])
        dl = H.apply_sizing(dl, pos, w, C2["mkt"]["settle"], H.RULES["multiplier"])
    p = H._perf(dl)
    flag = ""
    if want:
        ok = (len(tr) == want[0] and abs(p["cum_pct"] - want[1]) < 0.05
              and abs(p["sharpe"] - want[2]) < 0.005)
        g_e &= ok
        flag = "  ✓与记录一致" if ok else "  ✗与记录不符"
    print(f"  {code:<4}{len(tr):>4} 笔  {p['cum_pct']:>+8.1f}%  夏普 {p['sharpe']:>5.2f}  "
          f"回撤 {p['max_dd_pct']:>6.1f}%{flag}")
print(f"    G_E:{'**过**' if g_e else '**不过** —— 停手查 bug'}")

# ---- 2026 单独一栏 ----
print("\n[参考] 2026 年(样本极小,不作依据)")
for tag in ("现行 30%", f"H_主 X={X:.0%}", f"H_次 X2={X2:.0%}"):
    ts = [t for t in res[tag][0] if t["entry_date"][:4] == "2026"]
    print(f"  {tag:<16}{len(ts)} 笔" + "".join(
        f"\n      {t['entry_date']} {t['side']:<5} → {t.get('exit_date')} "
        f"{t['ret_pct']:+.2f}% {t.get('exit_reason')}" for t in ts))

# ---- 敏感性(不作判据)----
print("\n[敏感性] 其他档位 —— **不作判据**(不许拿它挑一档上线)")
for um in (0.40, 0.50, 0.60, 0.70, 1.00):
    t_, d_ = arm(FG, "FG", um)
    p_ = H._perf(d_)
    print(f"  ≤{um:.0%}  {len(t_):>3} 笔  {p_['cum_pct']:>+8.1f}%  夏普 {p_['sharpe']:>5.2f}  "
          f"回撤 {p_['max_dd_pct']:>6.1f}%")
arm(FG, "FG", 0.30)

print("\n" + "=" * 88)
print(f"闸门:G_A {'过' if g_a else '不过'} · G_B {'过' if g_b else '不过'} · "
      f"G_C {'过' if g_c else '不过'} · G_D {'过' if g_d else '不过'} · G_E {'过' if g_e else '不过'}")
print("处置按预注册第五节:G_A+G_B+G_C 全过 → 玻璃单独配 X;G_A 不过 → 保留 30% 关账;"
      "G_B 不过 → 写明「卸仓比例对玻璃没信息」但仍不改;G_C/G_D 不过 → 待拍板。")
