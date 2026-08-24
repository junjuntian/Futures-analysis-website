"""建仓区间检测器(焦煤):逐家在指定合约上的分批加仓 -> 聚区间 -> 加权成本。

口径(2026-08-24):
- 逐家逐日 Δnet(同合约相邻可得日,间隔<=5天);多头阵营加仓 = net>0 且 Δ>0,
  空头阵营加仓 = net<0 且 Δ<0(绝对值增大,PITFALLS 四:加减仓按绝对值判)。
- 建仓日:阵营当日合计加仓 >= 500 手;区间 = 建仓日之间隔 <=3 个交易日则并段;
  区间总加仓 >= 5000 手才列出。
- 区间成本 = Σ(当日加仓 x 该合约当日结算价) / Σ加仓 —— 与引擎 inst_cost_series
  同一会计(加仓按结算价加权),但按合约自己的价格算(左侧建仓时它还不是主力)。
- 数据用事后完整 net(描述工具,不是可交易信号;反推行滞后1天在此无碍)。
跑法:python research/run_build_zones.py JM JM2701 [更多合约...]
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "JM"
contracts = sys.argv[2:] or ["JM2505", "JM2509", "JM2601", "JM2605", "JM2609", "JM2701"]
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
roll_a, _, _ = H.rolling_groups(seat, price, mkt.index)
GRP = list(roll_a.dropna().iloc[-1])

MIN_DAILY, MERGE_GAP, MIN_TOTAL = 500.0, 3, 5000.0

d = seat[seat["member_key"].isin(GRP)].copy()
settle_all = price.pivot_table(index="trade_date", columns="contract",
                               values="settle", aggfunc="first").sort_index()

lines = [f"{v['name']} 建仓区间检测  组: " + "、".join(GRP),
         f"数据至 {seat['trade_date'].max().date()}  "
         f"建仓日>= {MIN_DAILY:.0f}手/日,并段间隔<= {MERGE_GAP}日,区间>= {MIN_TOTAL:.0f}手",
         ""]

for c in contracts:
    sub = d[d["contract"] == c]
    if sub.empty:
        lines.append(f"{c}: 无席位数据\n")
        continue
    px = settle_all[c].dropna() if c in settle_all.columns else pd.Series(dtype=float)
    days = sorted(sub["trade_date"].unique())
    # 逐家 Δnet
    adds = {"long": pd.Series(0.0, index=days), "short": pd.Series(0.0, index=days)}
    detail = {"long": {}, "short": {}}   # day -> {member: qty}
    for m, ms in sub.groupby("member_key"):
        s = ms.sort_values("trade_date").set_index("trade_date")["net"]
        prev, prev_day = None, None
        for t, n in s.items():
            if prev is not None and (t - prev_day).days <= 5:
                dn = n - prev
                if n > 0 and dn > 0:       # 多头加仓
                    adds["long"][t] += dn
                    detail["long"].setdefault(t, {})[m] = dn
                elif n < 0 and dn < 0:     # 空头加仓(绝对值增大)
                    adds["short"][t] += -dn
                    detail["short"].setdefault(t, {})[m] = -dn
            prev, prev_day = n, t
    # 阵营最终|净持仓|峰值(给"这段占峰值几成"用)
    camp_peak = {}
    for camp, sgn in (("long", 1), ("short", -1)):
        g = sub.groupby("trade_date")["net"].apply(
            lambda s: s[np.sign(s) == sgn].abs().sum())
        camp_peak[camp] = g.max() if len(g) else np.nan

    lines.append(f"=== {c} [{pd.Timestamp(days[0]).date()}~{pd.Timestamp(days[-1]).date()}]  "
                 f"多头阵营峰值 {camp_peak['long']:.0f}手  空头阵营峰值 {camp_peak['short']:.0f}手 ===")
    for camp, label, side in (("long", "多头", +1), ("short", "空头", -1)):
        a = adds[camp]
        hot = [t for t in a.index if a[t] >= MIN_DAILY]
        # 并段
        zones = []
        for t in hot:
            if zones and len(a.loc[zones[-1][1]:t]) - 1 <= MERGE_GAP:
                zones[-1][1] = t
            else:
                zones.append([t, t])
        for z0, z1 in zones:
            seg = a.loc[z0:z1]
            tot = seg.sum()
            if tot < MIN_TOTAL:
                continue
            pseg = px.reindex(seg.index)
            cost = float((seg * pseg).sum() / tot) if pseg.notna().all() else np.nan
            lo = float(pseg.min()) if pseg.notna().any() else np.nan
            hi = float(pseg.max()) if pseg.notna().any() else np.nan
            # 后市:区间末日起该合约自己 20/40 日(按阵营方向折算)
            fut = px[px.index > z1]
            def f(nd):
                if len(fut) < nd or not np.isfinite(cost):
                    return np.nan
                return side * (float(fut.iloc[nd - 1]) / float(px.asof(z1)) - 1) * 100
            share = tot / camp_peak[camp] * 100 if np.isfinite(camp_peak[camp]) and camp_peak[camp] else np.nan
            # 区间内主要加仓的家
            who = {}
            for t in seg.index:
                for m, q in detail[camp].get(t, {}).items():
                    who[m] = who.get(m, 0) + q
            who_s = "、".join(f"{m}{q/1000:.1f}k" for m, q in
                              sorted(who.items(), key=lambda kv: -kv[1])[:3])
            lines.append(f"  {label} {pd.Timestamp(z0).date()}~{pd.Timestamp(z1).date()}"
                         f"({len(seg)}日)  加{tot:,.0f}手(占峰{share:.0f}%)  "
                         f"成本{cost:,.0f}  价区[{lo:,.0f}~{hi:,.0f}]  "
                         f"后20日{f(20):+.1f}%  后40日{f(40):+.1f}%  主力:{who_s}")
    lines.append("")

io.open(OUT / f"build_zones_{code.lower()}.txt", "w", encoding="utf-8").write("\n".join(lines))
print("ok")
