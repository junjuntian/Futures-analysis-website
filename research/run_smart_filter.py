"""聪明钱资格过滤重放:阵营历史战役累计盈亏(PIT)>=0 才许跟,JM/LH 全重放。

预注册(2026-08-24,运营者拍板「聪明钱始终赚钱」作前置过滤):
- 资格:该组该方向(多/空阵营为一个"人格")在本品种全部历史合约上的累计盈亏,
  按日结算价计(prev_net x Δsettle x 乘数,只算 sign(prev_net)==side 的行),
  取到进场信号日前一日为止,>=0 即合格(零历史=0,放行)。
- 其余规则与 run_lh_validation.py 完全一致(阈值按品种规模缩放)。
- 版本A:反复分批+卸30快出;版本B:一届一波段(首个**合格**信号),R1/R4/PERF。
跑法:python research/run_smart_filter.py JM LH
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

codes = sys.argv[1:] or ["JM", "LH"]
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
GAP, TAIL = 3, 10
all_lines = []

for code in codes:
    raw = pd.read_csv(D / f"{code.lower()}_price.csv.gz")
    price = H.clean_price(raw)
    seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
    v = H.use(code)
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    if H.RULES.get("fixed_members"):
        GRP = list(H.RULES["fixed_members"])
    else:
        roll, _, _ = H.rolling_groups(seat, price, mkt.index)
        GRP = list(roll.dropna().iloc[-1])
    RETAIL = [m for m in H.RULES["retail_seed"] if m in set(seat["member_key"])]
    CONTRACTS = [c for c in dict.fromkeys(mkt["main"])]
    settle_w = price.pivot_table(index="trade_date", columns="contract", values="settle", aggfunc="first").sort_index()
    open_w = (raw.assign(trade_date=pd.to_datetime(raw["trade_date"]),
                         _o=raw["open_price"].replace(0, np.nan))
                 .pivot_table(index="trade_date", columns="contract", values="_o", aggfunc="first").sort_index())

    # —— 资格序列:逐方向累计战役盈亏(全部合约,不限主力届)——
    dd = seat[seat["member_key"].isin(GRP)].merge(
        price[["contract", "trade_date", "settle"]], on=["contract", "trade_date"], how="inner")
    dd = dd.sort_values(["member_key", "contract", "trade_date"])
    g = dd.groupby(["member_key", "contract"])
    dd["prev_net"] = g["net"].shift()
    dd["prev_settle"] = g["settle"].shift()
    gapd = (dd["trade_date"] - g["trade_date"].shift()).dt.days
    dd = dd[dd["prev_net"].notna() & (gapd <= 5)].copy()
    dd["pnl"] = (dd["settle"] - dd["prev_settle"]) * dd["prev_net"] * H.RULES["multiplier"]
    qual = {}
    for side in (+1, -1):
        rows = dd[np.sign(dd["prev_net"]) == side]
        s = rows.groupby("trade_date")["pnl"].sum().sort_index().cumsum()
        qual[side] = s

    def qualified(side, t):
        s = qual[side]
        ss = s[s.index < t]
        return True if ss.empty else float(ss.iloc[-1]) >= 0

    # —— 缩放系数 ——
    peaks = []
    d_all = seat[seat["member_key"].isin(GRP)]
    for c in CONTRACTS:
        sub = d_all[d_all["contract"] == c]
        if sub.empty:
            continue
        w = sub.pivot_table(index="trade_date", columns="member_key", values="net", aggfunc="first")
        pl = w.where(w > 0).abs().sum(axis=1).max()
        ps = w.where(w < 0).abs().sum(axis=1).max()
        peaks.append(max(pl if np.isfinite(pl) else 0, ps if np.isfinite(ps) else 0))
    factor = float(np.median(peaks)) / 64800.0
    rd = lambda x: max(50.0, round(x * factor / 50) * 50)
    ADD_MIN, CONFIRM, RPEAK = rd(1000), rd(5000), rd(2000)

    streams = {}
    for c in CONTRACTS:
        sub = seat[(seat["member_key"].isin(GRP)) & (seat["contract"] == c)]
        if sub.empty or c not in settle_w.columns:
            continue
        px = settle_w[c].dropna()
        if len(px) < 40:
            continue
        op = open_w[c].reindex(px.index) if c in open_w.columns else pd.Series(np.nan, index=px.index)
        w = sub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first").reindex(px.index).ffill()
        rsub = seat[(seat["member_key"].isin(RETAIL)) & (seat["contract"] == c)]
        rw = (rsub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first")
                  .reindex(px.index).ffill()) if len(rsub) else pd.DataFrame(index=px.index)
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
                    if ok and qualified(side, t):
                        opps.append(i)
                        zone_fired = True
                    elif ok:
                        zone_fired = True   # 不合格也烧掉该区间,防止后市变合格才进(区间已过时效)
            ropp = rw.where(np.sign(rw) == -side).abs().sum(axis=1) if len(rw.columns) else pd.Series(0.0, index=px.index)
            rpeak_s = ropp.cummax()
            stale5 = pd.Series([(ropp.iloc[max(0, i - 5):i + 1].idxmax() != t) if i >= 5 else False
                                for i, t in enumerate(px.index)], index=px.index)
            s4 = ((rpeak_s >= RPEAK) & (ropp >= rpeak_s * 0.95) & stale5).fillna(False)
            streams[(c, side)] = {"px": px, "op": op, "net": net, "idx": idx, "opps": opps, "s4": s4}

    def opx_at(S, i):
        j = min(i + 1, len(S["idx"]) - 1)
        p = S["op"].iloc[j]
        return (float(p) if np.isfinite(p) else float(S["px"].iloc[j])), S["idx"][j]

    # 版本A
    trA = []
    for (c, side), S in streams.items():
        px, net, idx = S["px"], S["net"], S["idx"]
        opp_at = set(S["opps"])
        pos = None
        for i, t in enumerate(idx):
            dleft = H.days_to_window_end(c, t)
            if pos and dleft <= H.RULES["exit_before_delivery"]:
                xp, xd = opx_at(S, i)
                trA.append({**pos, "exit_date": xd, "exit_px": xp}); pos = None
            if pos:
                nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
                if np.isfinite(nn):
                    pos["peak"] = max(pos["peak"], nn)
                    if nn < pos["peak"] * 0.70:
                        xp, xd = opx_at(S, i)
                        trA.append({**pos, "exit_date": xd, "exit_px": xp}); pos = None
            if pos is None and i in opp_at and i + 1 < len(idx):
                if H.days_to_window_end(c, idx[i]) > H.RULES["exit_before_delivery"] + 5:
                    ep, _ = opx_at(S, i)
                    pos = {"contract": c, "side": side, "entry_date": idx[i + 1], "entry_px": ep,
                           "peak": float(net.iloc[i]) if np.isfinite(net.iloc[i]) else 0.0}
        if pos:
            trA.append({**pos, "exit_date": None, "exit_px": float(px.iloc[-1])})
    A = pd.DataFrame(trA)
    if len(A):
        A["ret"] = A["side"] * (A["exit_px"] / A["entry_px"] - 1) * 100
    closedA = A[A["exit_date"].notna()] if len(A) else pd.DataFrame()
    p_val = np.nan
    if len(closedA):
        rng = np.random.default_rng(7)
        sims = []
        for k in range(2000):
            tot = []
            for _, r in closedA.iterrows():
                px = settle_w[r["contract"]].dropna()
                h = max(1, px.index.get_loc(r["exit_date"]) - px.index.get_loc(r["entry_date"]))
                i0 = rng.integers(0, max(1, len(px) - h - 1))
                tot.append(r["side"] * (float(px.iloc[i0 + h]) / float(px.iloc[i0]) - 1) * 100)
            sims.append(np.mean(tot))
        p_val = float((np.array(sims) >= closedA["ret"].mean()).mean())

    # 版本B
    trB = {r: [] for r in ("R1", "R4", "PERF")}
    for (c, side), S in streams.items():
        if not S["opps"]:
            continue
        px, net, idx, s4 = S["px"], S["net"], S["idx"], S["s4"]
        e_i = S["opps"][0] + 1
        if e_i >= len(idx):
            continue
        end_i = next((i for i in range(e_i, len(idx))
                      if H.days_to_window_end(c, idx[i]) <= H.RULES["exit_before_delivery"]), len(idx) - 1)
        ep = S["op"].iloc[e_i]
        ep = float(ep) if np.isfinite(ep) else float(px.iloc[e_i])
        prof = lambda i: side * (float(px.iloc[i]) / ep - 1) * 100
        seg = px.iloc[e_i:end_i + 1]
        fav = side * (seg / ep - 1) * 100
        top_i = e_i + int(np.argmax(fav.values))
        pk = float(net.iloc[e_i]) if np.isfinite(net.iloc[e_i]) else 0.0
        fires = {"R1": None, "R4": None, "PERF": top_i}
        for i in range(e_i + 1, end_i + 1):
            nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
            if np.isfinite(nn):
                pk = max(pk, nn)
                if fires["R1"] is None and nn < pk * 0.70:
                    fires["R1"] = i
            if fires["R4"] is None and prof(i) > 0 and bool(s4.iloc[i]):
                fires["R4"] = i
        for r in trB:
            i = fires[r] if fires[r] is not None else end_i
            xp, xd = opx_at(S, i)
            trB[r].append(side * (xp / ep - 1) * 100)

    q_now = {s: (float(qual[s].iloc[-1]) / 1e8 if len(qual[s]) else 0.0) for s in (+1, -1)}
    all_lines.append(f"===== {v['name']}(缩放 {factor:.3f};多阵营累计 {q_now[+1]:+.1f} 亿 / 空阵营 {q_now[-1]:+.1f} 亿)=====")
    if len(closedA):
        x = closedA["ret"]
        all_lines.append(f"版本A+过滤: {len(x)} 笔  均值{x.mean():+.2f}%  中位{x.median():+.2f}%  胜率{(x>0).mean()*100:.0f}%  "
                         f"t={x.mean()/x.std()*np.sqrt(len(x)):+.2f}  最差{x.min():+.1f}%  合计{x.sum():+.1f}pp  安慰剂p={p_val:.3f}")
        cA = closedA.copy(); cA["y"] = pd.to_datetime(cA["exit_date"]).dt.year
        all_lines.append("  逐年: " + "  ".join(f"{y}:{g['ret'].mean():+.1f}%x{len(g)}" for y, g in cA.groupby("y")))
        sides = closedA.groupby("side")["ret"]
        for s, gg in sides:
            all_lines.append(f"  {'多' if s>0 else '空'}: {len(gg)}笔 均值{gg.mean():+.2f}%")
    else:
        all_lines.append("版本A+过滤: 无成交")
    all_lines.append(f"版本B+过滤({len(trB['R1'])} 届):")
    for r in ("R1", "R4", "PERF"):
        xx = pd.Series(trB[r])
        if len(xx):
            all_lines.append(f"  {r}: 均值{xx.mean():+.2f}%  中位{xx.median():+.2f}%  胜{(xx>0).mean()*100:.0f}%  最差{xx.min():+.1f}%  合计{xx.sum():+.1f}pp")
    all_lines.append("")

io.open(OUT / "smart_filter_replay.txt", "w", encoding="utf-8").write("\n".join(all_lines))
print("ok")
