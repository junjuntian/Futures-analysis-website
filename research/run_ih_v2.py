# -*- coding: utf-8 -*-
"""IH 第二轮:反向 / 行为分层 / 流量(PLAN_IH_MODEL_v2,参数全冻结)。
跑法:python research/run_ih_v2.py"""
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
roll, _, _ = H.rolling_groups(seat, price, mkt.index)
CUT = pd.Timestamp("2019-04-22")
COST = 0.001

L = [f"IH 第二轮:反向/行为分层/流量(数据至 {mkt.index[-1].date()};基准 恒多 +45.2%/0.26)", ""]


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
    return sig


def perf(daily):
    dd = pd.Series(daily).dropna()
    if len(dd) < 60:
        return np.nan, np.nan, np.nan
    eq = (1 + dd).cumprod()
    mdd = float((eq / eq.cummax() - 1).min()) * 100
    sh = float(dd.mean() / dd.std() * np.sqrt(242)) if dd.std() > 0 else np.nan
    return (float(eq.iloc[-1]) - 1) * 100, sh, mdd


def gates(pos, tag):
    """五闸:安慰剂/T+2/扣成本/正年/2019后同向。pos = ±1/0/nan 仓位序列。"""
    base = (pos.shift(2) * mkt["ret_open"]).dropna()
    cum, sh, mdd = perf(base)
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in base.groupby(base.index.year)}
    pos_years = sum(1 for v in ys.values() if v > 0)
    inmkt = float(pos.shift(2).replace(0, np.nan).notna().mean() * 100)
    line = f"{tag}: 复利 {cum:+.1f}%  夏普 {sh:.2f}  回撤 {mdd:+.1f}%  正年 {pos_years}/{len(ys)}  在场 {inmkt:.0f}%"
    if not np.isfinite(sh) or sh <= 0.26:
        return line + "  -> 不赢基准,闸门不跑"
    rng = np.random.default_rng(11)
    arr, n = pos.values, len(pos)
    sh_l = []
    for k in range(500):
        off = int(rng.integers(20, n - 20))
        p2 = pd.Series(np.roll(arr, off), index=pos.index)
        d2 = (p2.shift(2) * mkt["ret_open"]).dropna()
        sh_l.append(float(d2.mean() / d2.std() * np.sqrt(242)) if len(d2) > 60 and d2.std() > 0 else 0.0)
    p_pl = float((np.array(sh_l) >= sh).mean())
    _, sh_t2, _ = perf(pos.shift(3) * mkt["ret_open"])
    turn = (pos.shift(2) != pos.shift(3)).astype(float)
    cum_n, sh_n, _ = perf(pos.shift(2) * mkt["ret_open"] - turn * COST)
    late = (pos.shift(2) * mkt["ret_open"]).loc[CUT:]
    _, sh_late, _ = perf(late)
    ok = (p_pl < 0.05 and np.isfinite(sh_t2) and sh_t2 >= 0.8 * sh and cum_n > 0
          and pos_years >= 2 * len(ys) / 3 and np.isfinite(sh_late) and sh_late > 0)
    line += (f"\n    闸门: 安慰剂 p={p_pl:.3f}  T+2 {sh_t2:.2f}(/{sh:.2f})  扣成本 {cum_n:+.1f}%/{sh_n:.2f}"
             f"  2019后夏普 {sh_late:.2f}  -> {'全过' if ok else '不过'}")
    return line


# ---- 数据底座 ----
grp_sig = H.signal_series(seat, roll)["net"]
all20 = seat.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
all20 = all20.reindex(mkt.index).ffill()

# ---- C1 反向 ----
L.append("── C1 反向 ──")
L.append(gates(-np.sign(grp_sig).replace(0, np.nan).ffill(), "阵营 sign 反向"))
L.append(gates(-np.sign(all20).replace(0, np.nan).ffill(), "全榜 sign 反向"))
q_hi = all20.expanding(min_periods=484).apply(lambda x: (x.iloc[-1] >= np.quantile(x, 0.85)))
q_lo = all20.expanding(min_periods=484).apply(lambda x: (x.iloc[-1] <= np.quantile(x, 0.15)))
# 净空拥挤(净持仓最负=分位最低)→ 做多;净多极值 → 做空
pos_ext = pd.Series(0.0, index=mkt.index)
pos_ext[q_lo.reindex(mkt.index) == 1] = 1.0
pos_ext[q_hi.reindex(mkt.index) == 1] = -1.0
L.append(gates(pos_ext, "拥挤极值(≤0.15分位多/≥0.85空)"))
L.append("")

# ---- C2 行为分层 ----
L.append("── C2 行为分层(88 家按翻转中位分半)──")
members = sorted(seat["member_key"].unique())
info = []
for m in members:
    sig = member_pos(m)
    p = np.sign(sig)
    presence = float(p.notna().mean())
    scale = float(sig.abs().median())
    if presence < 0.5 or not np.isfinite(scale) or scale < 200:
        continue
    flips = int((p != p.shift()).sum())
    info.append((m, flips, sig))
med_flip = float(np.median([f for _, f, _ in info]))
hedgers = [m for m, f, _ in info if f <= med_flip]
speculators = [m for m, f, _ in info if f > med_flip]
L.append(f"翻转中位 {med_flip:.0f}:对冲型 {len(hedgers)} 家 / 投机型 {len(speculators)} 家")
spec_net = sum(s.fillna(0) for m, f, s in info if m in set(speculators))
hedge_net = sum(s.fillna(0) for m, f, s in info if m in set(hedgers))
L.append(gates(np.sign(spec_net).replace(0, np.nan).ffill(), "投机型合计·顺向"))
L.append(gates(-np.sign(hedge_net).replace(0, np.nan).ffill(), "对冲型合计·反向"))
L.append("")

# ---- C3 流量 ----
L.append("── C3 流量(全榜 5 日 Δnet 的 63 日 z,|z|>1 顺向)──")
dnet = all20.diff(5)
z = (dnet - dnet.rolling(63).mean()) / dnet.rolling(63).std()
pos3 = pd.Series(0.0, index=mkt.index)
pos3[z > 1] = 1.0
pos3[z < -1] = -1.0
L.append(gates(pos3, "流量 z"))

io.open(OUT / "ih_v2.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
