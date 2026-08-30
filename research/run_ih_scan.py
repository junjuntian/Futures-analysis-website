# -*- coding: utf-8 -*-
"""IH 全席位侦察 + 制度分段(PLAN_IH_MODEL_v1 续章,2026-08-30)。

首轮套装 A/B 全不支持后的两条预注册续路:
1. 制度分段:2019-04-22 松绑为唯一允许的外生断点,滚动组五家在其后的子样本重跑;
2. 全席位侦察(REPORT_JM_SINGLE_SEAT_v1 / REPORT_SA_SEAT_SCAN_v1 同法):
   144 家归一会员全扫 T+1,这是**假设生成**不是验收 —— 入选者再走五闸+校正。
跑法:python research/run_ih_scan.py
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / "ih_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "ih_seat.csv.gz"))
H.use("IH")
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
mains = [c for c in dict.fromkeys(mkt["main"]) if isinstance(c, str)]
CUT = pd.Timestamp("2019-04-22")   # 预注册唯一断点:第二次松绑

L = [f"IH 全席位侦察 + 制度分段(数据至 {mkt.index[-1].date()})", ""]


def member_pos(member):
    sub = seat[seat["member_key"] == member]
    sig = pd.Series(np.nan, index=mkt.index)
    for c in mains:
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = mkt.index[mkt["main"] == c]
        sig.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
    p = np.sign(sig)
    p[p == 0] = np.nan
    return p.ffill(), sig


def perf(daily):
    dd = pd.Series(daily).dropna()
    if len(dd) < 60:
        return np.nan, np.nan, np.nan, 0
    eq = (1 + dd).cumprod()
    mdd = float((eq / eq.cummax() - 1).min()) * 100
    sh = float(dd.mean() / dd.std() * np.sqrt(242)) if dd.std() > 0 else np.nan
    return (float(eq.iloc[-1]) - 1) * 100, sh, mdd, len(dd)


# ---- 1. 制度分段:滚动组五家,2019-04-22 后 ----
roll, _, _ = H.rolling_groups(seat, price, mkt.index)
GRP = list(roll.dropna().iloc[-1])
L.append(f"── 1. 制度分段(≥{CUT.date()},滚动组五家)──")
for m in GRP:
    pos, _ = member_pos(m)
    base = (pos.shift(2) * mkt["ret_open"]).loc[CUT:]
    cum, sh, mdd, n = perf(base)
    d_ = base.dropna()
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in d_.groupby(d_.index.year)}
    pos_years = sum(1 for x in ys.values() if x > 0)
    L.append(f"  {m}: 累计 {cum:+.1f}%  夏普 {sh:.2f}  回撤 {mdd:+.1f}%  正年 {pos_years}/{len(ys)}")
L.append("")

# ---- 2. 全席位侦察(144 家,全期与分段各排一版)----
members = sorted(seat["member_key"].unique())
rows = []
for m in members:
    pos, sig = member_pos(m)
    presence = float(pos.notna().mean())
    scale = float(sig.abs().median())
    if presence < 0.5 or not np.isfinite(scale) or scale < 200:
        continue   # 常年不在榜/规模太小的不看:跟不动
    full = perf(pos.shift(2) * mkt["ret_open"])
    late = perf((pos.shift(2) * mkt["ret_open"]).loc[CUT:])
    d_ = (pos.shift(2) * mkt["ret_open"]).dropna()
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in d_.groupby(d_.index.year)}
    pos_years = sum(1 for x in ys.values() if x > 0)
    flips = int((pos != pos.shift()).sum())
    rows.append({"m": m, "full_sh": full[1], "full_cum": full[0],
                 "late_sh": late[1], "late_cum": late[0], "late_mdd": late[2],
                 "pos_years": pos_years, "nyears": len(ys), "flips": flips,
                 "scale": scale, "presence": presence})

L.append(f"── 2. 全席位侦察(在场≥50% 且 |净|中位≥200 手,共 {len(rows)} 家)──")
L.append("按 2019-04 后夏普排,前 12:")
for r in sorted(rows, key=lambda x: -(x["late_sh"] if np.isfinite(x["late_sh"]) else -9))[:12]:
    L.append(f"  {r['m']:<8} 后段夏普 {r['late_sh']:+.2f} 累计 {r['late_cum']:+.1f}% 回撤 {r['late_mdd']:+.1f}%"
             f" | 全期夏普 {r['full_sh']:+.2f} 正年 {r['pos_years']}/{r['nyears']}"
             f" 翻转 {r['flips']} 规模 {r['scale']:,.0f}")
L.append("")
L.append("按全期夏普排,前 8:")
for r in sorted(rows, key=lambda x: -(x["full_sh"] if np.isfinite(x["full_sh"]) else -9))[:8]:
    L.append(f"  {r['m']:<8} 全期夏普 {r['full_sh']:+.2f} 累计 {r['full_cum']:+.1f}%"
             f" 正年 {r['pos_years']}/{r['nyears']} 规模 {r['scale']:,.0f}")

io.open(OUT / "ih_scan.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
