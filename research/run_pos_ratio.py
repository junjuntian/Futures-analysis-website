"""逐家滚动仓位比例(聪明钱满仓度)探针 —— 焦煤现行 5 家。

口径(2026-08-24 运营者拍板):
- 每家各算各的:比例 = 该家当前主力合约净持仓 / 该家上一届主力(X-4 月)|净持仓|峰值。
- 分母滚动无前视:上一届合约 |net| 的 cummax,取到 t-1 日为止。
- 分母 < 5000 手 → 该家当期「无有效基准」,不参与统计(东吴 2505 之前那种)。
- 阵营分开:净空阵营 / 净多阵营各一条满仓度;合成主用人头(几家 >=80%),
  辅以加权(阵营 Σ|net| / Σ基准)。
- 分子当日可见口径 net_off(官方行,DEC-108),按(家,合约)内 ffill,掉榜沿用前值。
跑法:python research/run_pos_ratio.py JM
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "JM"
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
roll_a, _, _ = H.rolling_groups(seat, price, mkt.index)
GRP = list(roll_a.dropna().iloc[-1])
MIN_BASE = 5000.0
FULL, HALF = 0.8, 0.5


def prev_main(contract: str) -> str:
    raw = "".join(ch for ch in str(contract) if ch.isdigit())
    yy, mm = int(raw[:2]), int(raw[2:])
    mm -= 4
    if mm <= 0:
        mm += 12
        yy -= 1
    return f"{code}{yy:02d}{mm:02d}"


d = seat[seat["member_key"].isin(GRP)].copy()
vis, peak = {}, {}
for (m, c), sub in d.groupby(["member_key", "contract"]):
    s = sub.sort_values("trade_date").set_index("trade_date")
    vis[(m, c)] = s["net_off"].ffill()      # 当日可见,掉榜沿用前值
    peak[(m, c)] = s["net"].abs().cummax().shift(1)  # 截至 t-1 的峰值

rows = []
for t in mkt.index:
    cur = mkt.loc[t, "main"]
    prv = prev_main(cur)
    rec = {"date": t, "main": cur}
    for m in GRP:
        net = base = np.nan
        vs = vis.get((m, cur))
        if vs is not None:
            vv = vs[vs.index <= t]
            if len(vv):
                net = float(vv.iloc[-1])
        ps = peak.get((m, prv))
        if ps is not None:
            pp = ps[ps.index <= t]
            if len(pp):
                base = float(pp.iloc[-1])
        rec[f"net_{m}"] = net
        rec[f"base_{m}"] = base
    rows.append(rec)
df = pd.DataFrame(rows).set_index("date")

comp = []
for t, r in df.iterrows():
    n_valid = 0
    s_full = s_half = l_full = l_half = 0
    s_abs = s_base = l_abs = l_base = 0.0
    for m in GRP:
        net, base = r[f"net_{m}"], r[f"base_{m}"]
        if not np.isfinite(net) or not np.isfinite(base) or base < MIN_BASE:
            continue
        n_valid += 1
        ratio = abs(net) / base
        if net < 0:
            s_abs += abs(net); s_base += base
            s_full += int(ratio >= FULL); s_half += int(ratio >= HALF)
        elif net > 0:
            l_abs += abs(net); l_base += base
            l_full += int(ratio >= FULL); l_half += int(ratio >= HALF)
    comp.append({"date": t, "main": r["main"], "n_valid": n_valid,
                 "short_full": s_full, "short_half": s_half,
                 "long_full": l_full, "long_half": l_half,
                 "short_w": s_abs / s_base * 100 if s_base else np.nan,
                 "long_w": l_abs / l_base * 100 if l_base else np.nan})
cp = pd.DataFrame(comp).set_index("date")
cp["settle"] = mkt["settle"]
cp.to_csv(OUT / f"pos_ratio_{code.lower()}.csv", encoding="utf-8")

# —— 逐家比例(画图与留档用)——
ratio_cols = {}
for m in GRP:
    net, base = df[f"net_{m}"], df[f"base_{m}"]
    ok = base.ge(MIN_BASE)
    ratio_cols[m] = (net / base).where(ok)   # 有向:负=净空
rt = pd.DataFrame(ratio_cols)
rt.to_csv(OUT / f"pos_ratio_members_{code.lower()}.csv", encoding="utf-8")

# —— 摘要 + 事件表 ——
lines = [f"{v['name']} 逐家滚动仓位比例  组: " + "、".join(GRP),
         f"数据至 {mkt.index[-1].date()}  基准=上一届主力|净持仓|峰值(截至t-1)  MIN_BASE={MIN_BASE:.0f}手",
         ""]
lines.append("=== 逐届主力:净空阵营满仓事件(人头 >=3 家满仓80% 首日 / 加权满仓度峰值)===")
adj = (1 + mkt["ret"].fillna(0)).cumprod()   # 复权连乘,换月安全

def fwd(t, days):
    idx = mkt.index
    i = idx.get_loc(t)
    j = min(i + days, len(idx) - 1)
    return (adj.iloc[j] / adj.iloc[i] - 1) * 100

for c, seg in cp.groupby("main", sort=False):
    if len(seg) < 20:
        continue
    l = f"{c} [{seg.index[0].date()}~{seg.index[-1].date()}]"
    ev = seg[seg["short_full"] >= 3]
    if len(ev):
        t0 = ev.index[0]
        l += f"  净空3家满仓首日 {t0.date()} 后20日 {fwd(t0, 20):+.1f}%"
    else:
        l += "  净空3家满仓: 未出现"
    if seg["short_w"].notna().any():
        tp = seg["short_w"].idxmax()
        l += f" | 加权满仓度峰 {seg['short_w'].max():.0f}%@{tp.date()} 后20日 {fwd(tp, 20):+.1f}%"
    lf = seg[seg["long_full"] >= 2]
    if len(lf):
        l += f" | 净多2家满仓首日 {lf.index[0].date()} 后20日 {fwd(lf.index[0], 20):+.1f}%"
    lines.append(l)

lines.append("")
lines.append("=== 最新一日 ===")
last = cp.iloc[-1]
lines.append(f"{cp.index[-1].date()} 主力 {last['main']}  可评 {last['n_valid']:.0f} 家  "
             f"净空满仓 {last['short_full']:.0f} 家/半仓 {last['short_half']:.0f} 家 加权 {last['short_w']:.0f}%  "
             f"净多满仓 {last['long_full']:.0f} 家/半仓 {last['long_half']:.0f} 家 加权 "
             f"{last['long_w'] if np.isfinite(last['long_w']) else float('nan'):.0f}%")
lines.append("逐家比例(负=净空): " + "  ".join(
    f"{m}:{rt[m].iloc[-1]*100:+.0f}%" if np.isfinite(rt[m].iloc[-1]) else f"{m}:—" for m in GRP))
io.open(OUT / f"pos_ratio_{code.lower()}.txt", "w", encoding="utf-8").write("\n".join(lines))

# —— 画图 ——
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False
fig, ax = plt.subplots(3, 1, figsize=(16, 11), sharex=True,
                       gridspec_kw={"height_ratios": [2, 2, 1]})
ax[0].plot(cp.index, cp["settle"], lw=1.0, color="black")
ax[0].set_title(f"{v['name']} 主力结算价(竖线=换主力)")
switches = cp.index[cp["main"] != cp["main"].shift()]
for s in switches[1:]:
    for a in ax:
        a.axvline(s, color="gray", lw=0.5, alpha=0.5)
ax[1].plot(cp.index, cp["short_w"], lw=1.2, color="tab:green", label="净空阵营加权满仓度%")
ax[1].plot(cp.index, cp["long_w"], lw=1.2, color="tab:red", label="净多阵营加权满仓度%")
ax[1].axhline(80, color="gray", ls="--", lw=0.8)
ax[1].axhline(50, color="gray", ls=":", lw=0.8)
ax[1].set_ylim(0, 200)
ax[1].legend(loc="upper left")
ax[2].bar(cp.index, cp["short_full"], color="tab:green", width=1.0, label="净空满仓(>=80%)家数")
ax[2].bar(cp.index, -cp["long_full"], color="tab:red", width=1.0, label="净多满仓家数(向下)")
ax[2].set_ylim(-5, 5)
ax[2].legend(loc="upper left")
fig.tight_layout()
fig.savefig(OUT / f"pos_ratio_{code.lower()}.png", dpi=110)
print("ok")
