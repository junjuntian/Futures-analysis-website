"""进场质量 + 阶段高点解剖 + 多指标加权分批出场(焦煤)。

三部分(2026-08-24,运营者要求:出场多指标加权、分~3天撤出、看高点日的
加仓/上影/散户持仓与均价):
A. 进场质量(与出场无关):全部进场机会的 5/10/20 日走势、20日内最大有利/不利偏移。
B. 阶段高点解剖:战役窗口内 ±5 日局部有利极值日,数各指标当日命中率 vs 全部日,给提升倍数。
   指标:S1 卸仓脚(|净|<峰值90% 或 单日净减>=3000);S2 冲高回落(有利20日新高 且 上影>=0.5);
   S3 高潮加仓(单日加>=5000 且 有利20日新高);S4 散户对手到峰(>=95%运行峰(>=2000) 且5日不创新高);
   S5 散户深套(价对散户均价不利>=5%,状态量只解剖不进触发)。
C. 出场对比(入场冻结 confirm=5000):
   E1  卸30%单日,次日开盘一次性出(基线);
   E1s 同触发,分3天撤(次日起连续3个开盘价均价);
   W2  加权:滚动3日内 S1~S4 至少两类命中 -> 分3天撤;
   W2d W2 触发或卸30%兜底,先到先走,分3天撤。
跑法:python research/run_exit_weighted.py JM
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "JM"
CONFIRM, ADD_MIN, GAP, TAIL = 5000.0, 1000.0, 3, 10
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
raw = pd.read_csv(D / f"{code.lower()}_price.csv.gz")
price = H.clean_price(raw)
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
roll, _, _ = H.rolling_groups(seat, price, mkt.index)
GRP = list(roll.dropna().iloc[-1])
RETAIL = [m for m in H.RULES["retail_seed"] if m in set(seat["member_key"])]
CONTRACTS = ["JM2401", "JM2405", "JM2409", "JM2501", "JM2505",
             "JM2509", "JM2601", "JM2605", "JM2609", "JM2701"]


def piv(col):
    x = raw.copy()
    x["trade_date"] = pd.to_datetime(x["trade_date"])
    x[col] = x[col].replace(0, np.nan)
    return x.pivot_table(index="trade_date", columns="contract", values=col, aggfunc="first").sort_index()


settle_w = price.pivot_table(index="trade_date", columns="contract", values="settle", aggfunc="first").sort_index()
open_w, high_w, low_w = piv("open_price"), piv("high_price"), piv("low_price")

streams = {}
for c in CONTRACTS:
    sub = seat[(seat["member_key"].isin(GRP)) & (seat["contract"] == c)]
    if sub.empty or c not in settle_w.columns:
        continue
    px = settle_w[c].dropna()
    w = sub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first").reindex(px.index).ffill()
    rsub = seat[(seat["member_key"].isin(RETAIL)) & (seat["contract"] == c)]
    rw = (rsub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first")
              .reindex(px.index).ffill()) if len(rsub) else pd.DataFrame(index=px.index)
    hi = high_w[c].reindex(px.index) if c in high_w.columns else pd.Series(np.nan, index=px.index)
    lo = low_w[c].reindex(px.index) if c in low_w.columns else pd.Series(np.nan, index=px.index)
    op = open_w[c].reindex(px.index) if c in open_w.columns else pd.Series(np.nan, index=px.index)
    for side in (+1, -1):
        net = w.where(np.sign(w) == side).abs().sum(axis=1)
        chg = net.diff()
        ret5 = px.pct_change(5)
        vwap = pd.Series(np.nan, index=px.index)
        q, cst = 0.0, np.nan
        for t in px.index:
            n, p = float(net.get(t, np.nan)), float(px[t])
            if np.isfinite(n):
                dn = n - q
                if dn > 0:
                    cst = p if not np.isfinite(cst) or q <= 0 else (cst * q + dn * p) / (q + dn)
                q = n
            vwap[t] = cst
        losing = (px <= vwap) if side > 0 else (px >= vwap)
        trending = (ret5 < 0) if side > 0 else (ret5 > 0)
        dip = (chg >= ADD_MIN) & (losing | trending)
        idx = list(px.index)
        opps = []
        zone_add = zone_cost = 0.0
        zone_last = None
        zone_fired = False
        for i, t in enumerate(idx):
            if zone_last is not None and idx.index(zone_last) < i - (GAP + TAIL):
                zone_add = zone_cost = 0.0; zone_last = None; zone_fired = False
            if bool(dip.get(t, False)):
                if zone_last is not None and idx.index(zone_last) >= i - (GAP + 1):
                    a = float(chg[t])
                    zone_cost = (zone_cost * zone_add + a * float(px[t])) / (zone_add + a)
                    zone_add += a
                else:
                    zone_add, zone_cost = float(chg[t]), float(px[t])
                    zone_fired = False
                zone_last = t
            dleft = H.days_to_window_end(c, t)
            if (not zone_fired and zone_last is not None and zone_add >= CONFIRM
                    and dleft > H.RULES["exit_before_delivery"] + 5
                    and idx.index(zone_last) >= i - (GAP + TAIL) and i + 1 < len(idx)):
                ok = (float(px[t]) <= zone_cost) if side > 0 else (float(px[t]) >= zone_cost)
                if ok:
                    opps.append({"i": i, "batch_cost": zone_cost})
                    zone_fired = True
        # 散户对手方合计与均价
        ropp = rw.where(np.sign(rw) == -side).abs().sum(axis=1) if len(rw.columns) else pd.Series(0.0, index=px.index)
        rvwap = pd.Series(np.nan, index=px.index)
        q2, c2 = 0.0, np.nan
        for t in px.index:
            n = float(ropp.get(t, np.nan))
            p = float(px[t])
            if np.isfinite(n):
                dn = n - q2
                if dn > 0:
                    c2 = p if not np.isfinite(c2) or q2 <= 0 else (c2 * q2 + dn * p) / (q2 + dn)
                q2 = n
            rvwap[t] = c2
        # —— 指标(逐日布尔)——
        fav_ext20 = pd.Series(False, index=px.index)
        roll20 = (px.rolling(20).max() if side > 0 else px.rolling(20).min())
        fav_ext20 = (px >= roll20) if side > 0 else (px <= roll20)
        rng_ok = (hi - lo) > 0
        shadow = ((hi - px.combine(hi, min)) / (hi - lo)) if side > 0 else ((px.combine(lo, max) - lo) / (hi - lo))
        # 上面写复杂了:上影 =(high-close)/(high-low)以收盘近似;短侧镜像
        cl = px  # 用结算近似收盘(展示层才用 close;两者差异小)
        shadow = ((hi - cl) / (hi - lo)).where(rng_ok) if side > 0 else ((cl - lo) / (hi - lo)).where(rng_ok)
        peakrun = net.cummax()
        s1 = (net < peakrun * 0.90) | (chg <= -3000)
        s2 = fav_ext20 & (shadow >= 0.5)
        s3 = (chg >= 5000) & fav_ext20
        rpeak = ropp.cummax()
        stale5 = pd.Series([(ropp.iloc[max(0, i - 5):i + 1].idxmax() != t) if i >= 5 else False
                            for i, t in enumerate(px.index)], index=px.index)
        s4 = (rpeak >= 2000) & (ropp >= rpeak * 0.95) & stale5
        s5 = (side * (px / rvwap - 1) >= 0.05)
        streams[(c, side)] = {"px": px, "op": op, "net": net, "idx": idx, "opps": opps,
                              "S": pd.DataFrame({"S1": s1.fillna(False), "S2": s2.fillna(False),
                                                 "S3": s3.fillna(False), "S4": s4.fillna(False),
                                                 "S5": s5.fillna(False)})}

lines = [f"{v['name']}  组: " + "、".join(GRP) + f"  数据至 {settle_w.index[-1].date()}", ""]

# ============ A. 进场质量 ============
fw = {5: [], 10: [], 20: []}
mfe, mae = [], []
n_opps = 0
for (c, side), S in streams.items():
    px, idx = S["px"], S["idx"]
    hi = high_w[c].reindex(px.index)
    lo = low_w[c].reindex(px.index)
    for o in S["opps"]:
        i = o["i"]
        if i + 1 >= len(idx):
            continue
        n_opps += 1
        e = float(px.iloc[i + 1])
        for h in (5, 10, 20):
            j = min(i + 1 + h, len(idx) - 1)
            fw[h].append(side * (float(px.iloc[j]) / e - 1) * 100)
        j = min(i + 21, len(idx) - 1)
        win_hi = hi.iloc[i + 1:j + 1]
        win_lo = lo.iloc[i + 1:j + 1]
        if side > 0:
            mfe.append((win_hi.max() / e - 1) * 100)
            mae.append((win_lo.min() / e - 1) * 100)
        else:
            mfe.append((1 - win_lo.min() / e) * 100)
            mae.append((1 - win_hi.max() / e) * 100)
lines.append(f"=== A. 进场质量(全部 {n_opps} 次机会,进场=信号次日结算近似)===")
for h in (5, 10, 20):
    x = pd.Series(fw[h]).dropna()
    lines.append(f"  {h:>2d}日: 均值{x.mean():+.2f}%  中位{x.median():+.2f}%  正比例{(x>0).mean()*100:.0f}%")
mfe, mae = pd.Series(mfe).dropna(), pd.Series(mae).dropna()
lines.append(f"  20日内最大有利偏移 MFE: 中位{mfe.median():+.2f}%  >=2%比例{(mfe>=2).mean()*100:.0f}%  >=5%比例{(mfe>=5).mean()*100:.0f}%")
lines.append(f"  20日内最大不利偏移 MAE: 中位{mae.median():+.2f}%  <=-5%比例{(mae<=-5).mean()*100:.0f}%  <=-10%比例{(mae<=-10).mean()*100:.0f}%")
lines.append("")

# ============ B. 阶段高点解剖 ============
top_hits = {k: 0 for k in ("S1", "S2", "S3", "S4", "S5")}
all_hits = {k: 0 for k in ("S1", "S2", "S3", "S4", "S5")}
n_top = n_all = 0
top2 = all2 = 0
for (c, side), S in streams.items():
    if not S["opps"]:
        continue
    px, idx, SS = S["px"], S["idx"], S["S"]
    i0 = S["opps"][0]["i"]
    iend = next((i for i, t in enumerate(idx)
                 if H.days_to_window_end(c, t) <= H.RULES["exit_before_delivery"]), len(idx) - 1)
    seg = px.iloc[i0:iend + 1]
    if len(seg) < 12:
        continue
    # ±5 日有利极值(整窗都有数据)
    for i in range(i0 + 5, iend - 4):
        wnd = px.iloc[i - 5:i + 6]
        is_top = (px.iloc[i] >= wnd.max()) if side > 0 else (px.iloc[i] <= wnd.min())
        # 近3日窗口的指标命中(高点常在指标日附近而非当日)
        h3 = SS.iloc[max(0, i - 1):i + 2].any()
        n_all += 1
        k2 = sum(bool(h3[k]) for k in ("S1", "S2", "S3", "S4"))
        all2 += k2 >= 2
        for k in top_hits:
            all_hits[k] += bool(h3[k])
        if is_top:
            n_top += 1
            top2 += k2 >= 2
            for k in top_hits:
                top_hits[k] += bool(h3[k])
lines.append(f"=== B. 阶段高点解剖(±5日有利极值日 {n_top} 个,战役内全部日 {n_all};指标按当日±1看)===")
for k, label in (("S1", "卸仓脚(|净|<峰90% 或 日净减>=3000)"), ("S2", "冲高回落(20日新高+上影>=0.5)"),
                 ("S3", "高潮加仓(日加>=5000+20日新高)"), ("S4", "散户对手到峰(>=95%峰+5日滞)"),
                 ("S5", "散户深套(价差>=5%,状态)")):
    pt = top_hits[k] / n_top * 100 if n_top else np.nan
    pa = all_hits[k] / n_all * 100 if n_all else np.nan
    lines.append(f"  {k} {label}: 高点日{pt:.0f}%  平常日{pa:.0f}%  提升x{pt/pa if pa else np.nan:.1f}")
lines.append(f"  S1~S4 任两类同时命中: 高点日{top2/n_top*100:.0f}%  平常日{all2/n_all*100:.0f}%  提升x{(top2/n_top)/(all2/n_all):.1f}")
lines.append("")

# ============ C. 出场对比 ============
def staged_px(S, i):
    """从 i+1 起连续 3 个开盘价均值(不足取可得的);开盘缺失用结算兜底。"""
    idx, px, op = S["idx"], S["px"], S["op"]
    ps = []
    for j in range(i + 1, min(i + 4, len(idx))):
        pj = op.iloc[j]
        ps.append(float(pj) if np.isfinite(pj) else float(px.iloc[j]))
    return float(np.mean(ps)), idx[min(i + 3, len(idx) - 1)]


def one_px(S, i):
    idx, px, op = S["idx"], S["px"], S["op"]
    j = min(i + 1, len(idx) - 1)
    pj = op.iloc[j]
    return (float(pj) if np.isfinite(pj) else float(px.iloc[j])), idx[j]


def run(name):
    trades = []
    for (c, side), S in streams.items():
        px, net, idx, SS = S["px"], S["net"], S["idx"], S["S"]
        opp_at = {o["i"]: o for o in S["opps"]}
        pos = None
        for i, t in enumerate(idx):
            dleft = H.days_to_window_end(c, t)
            if pos and dleft <= H.RULES["exit_before_delivery"]:
                ep, ed = one_px(S, i)
                trades.append({**pos, "exit_px": ep, "exit_date": ed, "why": "交割"}); pos = None
            if pos:
                nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
                if np.isfinite(nn):
                    pos["peak"] = max(pos["peak"], nn)
                trig = None
                if name in ("E1", "E1s") and np.isfinite(nn) and nn < pos["peak"] * 0.70:
                    trig = "卸30"
                if name in ("W2", "W2d"):
                    h3 = SS.iloc[max(0, i - 2):i + 1]
                    k2 = sum(h3[k].any() for k in ("S1", "S2", "S3", "S4"))
                    if k2 >= 2:
                        trig = f"加权{k2}类"
                    elif name == "W2d" and np.isfinite(nn) and nn < pos["peak"] * 0.70:
                        trig = "卸30兜底"
                if name == "W3":
                    row = SS.iloc[i]
                    if bool(row["S2"]) or bool(row["S3"]):
                        trig = "高点信号"
                    elif np.isfinite(nn) and nn < pos["peak"] * 0.70:
                        trig = "卸30兜底"
                if trig and i > pos["i0"]:
                    if name == "E1":
                        ep, ed = one_px(S, i)
                    else:
                        ep, ed = staged_px(S, i)
                    trades.append({**pos, "exit_px": ep, "exit_date": ed, "why": trig}); pos = None
            if pos is None and i in opp_at and i + 1 < len(idx):
                if H.days_to_window_end(c, idx[i]) > H.RULES["exit_before_delivery"] + 5:
                    ep, _ = one_px(S, i)
                    pos = {"contract": c, "side": side, "entry_date": idx[i + 1], "entry_px": ep,
                           "peak": float(net.iloc[i]) if np.isfinite(net.iloc[i]) else 0.0, "i0": i + 1}
        if pos:
            trades.append({**pos, "exit_px": float(px.iloc[-1]), "exit_date": None, "why": "未平"})
    tr = pd.DataFrame(trades)
    tr["ret"] = tr["side"] * (tr["exit_px"] / tr["entry_px"] - 1) * 100
    return tr.sort_values("entry_date")


rng = np.random.default_rng(7)


def placebo_p(closed):
    sims = []
    for k in range(2000):
        tot = []
        for _, r in closed.iterrows():
            px = settle_w[r["contract"]].dropna()
            h = max(1, px.index.get_loc(r["exit_date"]) - px.index.get_loc(r["entry_date"]))
            i0 = rng.integers(0, max(1, len(px) - h - 1))
            tot.append(r["side"] * (float(px.iloc[i0 + h]) / float(px.iloc[i0]) - 1) * 100)
        sims.append(np.mean(tot))
    return float((np.array(sims) >= closed["ret"].mean()).mean())


lines.append("=== C. 出场对比(同一套进场)===")
lines.append(f"{'变体':6s}{'笔':>4s}{'均值%':>8s}{'中位%':>8s}{'胜%':>5s}{'t':>7s}{'持有中位':>8s}{'最差%':>8s}{'合计pp':>8s}{'安慰剂p':>9s}")
res = {}
for name in ("E1", "E1s", "W2", "W2d", "W3"):
    tr = run(name)
    closed = tr[tr["exit_date"].notna()].copy()
    x = closed["ret"]
    hold = (pd.to_datetime(closed["exit_date"]) - pd.to_datetime(closed["entry_date"])).dt.days
    p = placebo_p(closed)
    lines.append(f"{name:6s}{len(x):>4d}{x.mean():>+8.2f}{x.median():>+8.2f}{(x>0).mean()*100:>5.0f}"
                 f"{x.mean()/x.std()*np.sqrt(len(x)):>+7.2f}{hold.median():>8.0f}{x.min():>+8.1f}{x.sum():>+8.1f}{p:>9.3f}")
    closed["y"] = pd.to_datetime(closed["exit_date"]).dt.year
    res[name] = (tr, "  ".join(f"{y}:{g['ret'].mean():+.1f}%x{len(g)}" for y, g in closed.groupby("y")))
lines.append("")
for name in ("E1", "E1s", "W2", "W2d", "W3"):
    lines.append(f"{name} 逐年: {res[name][1]}")
    tr = res[name][0]
    for _, r in tr[tr["exit_date"].isna()].iterrows():
        lines.append(f"  {name} 未平: {r['contract']} {'多' if r['side']>0 else '空'} {r['entry_date'].date()} @{r['entry_px']:.0f} 浮{r['ret']:+.1f}%")
io.open(OUT / f"exit_weighted_{code.lower()}.txt", "w", encoding="utf-8").write("\n".join(lines))
for name in ("E1", "W2d"):
    res[name][0].to_csv(OUT / f"exit_weighted_{code.lower()}_{name}.csv", index=False, encoding="utf-8")
print("ok")
