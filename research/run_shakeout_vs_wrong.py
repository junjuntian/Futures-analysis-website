"""震仓 vs 认错:机构卸仓30%时刻的可见特征对比(焦煤,16届战役)。

每次「阵营|净|首次跌破自进场峰值70%」记一个事件,事后标签:
- 震仓:该日之后价格还创出新的有利极值(战役继续赢);
- 认错:之后再没有新有利极值(战役到此失败/衰竭)。
事件日当时可见的特征(全部 PIT):
  F1 主力户自砍:峰值持仓最大那家 当前/自身峰值 %
  F2 对手阵营5日增仓(手)
  F3 对手阵营浮盈(价对其VWAP,%,正=对手在赚)
  F4 我方被套深度(价对我方VWAP,按我方方向,%,负=被套)
  F5 近5日逆行幅度(%,负=对我不利)
  F6 近3日最大单日逆行(%,运营者「突然涨停」信号)
  F7 组在下届合约同方向近5日净加(手,移仓探测)
  F8 我方/对手 阵营规模比
  F9 散户对手方5日增仓(手)
跑法:python research/run_shakeout_vs_wrong.py JM
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
settle_w = price.pivot_table(index="trade_date", columns="contract", values="settle", aggfunc="first").sort_index()


def next_contract(c):
    yy, mm = int(c[2:4]), int(c[4:6])
    mm += 4
    if mm > 12:
        mm -= 12
        yy += 1
    return f"{code}{yy:02d}{mm:02d}"


def camp_series(c, side):
    sub = seat[(seat["member_key"].isin(GRP)) & (seat["contract"] == c)]
    if sub.empty or c not in settle_w.columns:
        return None
    px = settle_w[c].dropna()
    w = sub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first").reindex(px.index).ffill()
    camp = w.where(np.sign(w) == side)
    net = camp.abs().sum(axis=1)
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
    return {"px": px, "w": w, "camp": camp, "net": net, "vwap": vwap}


events = []
for c in CONTRACTS:
    for side in (+1, -1):
        S = camp_series(c, side)
        if S is None:
            continue
        px, net = S["px"], S["net"]
        chg = net.diff()
        ret5 = px.pct_change(5)
        losing = (px <= S["vwap"]) if side > 0 else (px >= S["vwap"])
        trending = (ret5 < 0) if side > 0 else (ret5 > 0)
        dip = (chg >= ADD_MIN) & (losing | trending)
        idx = list(px.index)
        entry_i = None
        zone_add = zone_cost = 0.0
        zone_last = None
        for i, t in enumerate(idx):
            if zone_last is not None and idx.index(zone_last) < i - (GAP + TAIL):
                zone_add = zone_cost = 0.0; zone_last = None
            if bool(dip.get(t, False)):
                if zone_last is not None and idx.index(zone_last) >= i - (GAP + 1):
                    a = float(chg[t])
                    zone_cost = (zone_cost * zone_add + a * float(px[t])) / (zone_add + a)
                    zone_add += a
                else:
                    zone_add, zone_cost = float(chg[t]), float(px[t])
                zone_last = t
            dleft = H.days_to_window_end(c, t)
            if (entry_i is None and zone_last is not None and zone_add >= CONFIRM
                    and dleft > H.RULES["exit_before_delivery"] + 5 and i + 1 < len(idx)):
                ok = (float(px[t]) <= zone_cost) if side > 0 else (float(px[t]) >= zone_cost)
                if ok:
                    entry_i = i + 1
                    break
        if entry_i is None:
            continue
        end_i = next((i for i in range(entry_i, len(idx))
                      if H.days_to_window_end(c, idx[i]) <= H.RULES["exit_before_delivery"]), len(idx) - 1)
        ep = float(px.iloc[entry_i])
        fav = side * (px.iloc[entry_i:end_i + 1] / ep - 1) * 100
        # 对手阵营与下届
        O = camp_series(c, -side)
        NC = next_contract(c)
        subn = seat[(seat["member_key"].isin(GRP)) & (seat["contract"] == NC)]
        wn = (subn.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first")
                  .ffill()) if len(subn) else pd.DataFrame()
        netn = wn.where(np.sign(wn) == side).abs().sum(axis=1) if len(wn.columns) else pd.Series(dtype=float)
        rsub = seat[(seat["member_key"].isin(RETAIL)) & (seat["contract"] == c)]
        rw = (rsub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first")
                  .reindex(px.index).ffill()) if len(rsub) else pd.DataFrame(index=px.index)
        ropp = rw.where(np.sign(rw) == -side).abs().sum(axis=1) if len(rw.columns) else pd.Series(0.0, index=px.index)
        # 逐日峰值,找卸30事件(去抖:上一事件后要先回到峰值85%以上才计新事件)
        pk = float(net.iloc[entry_i]) if np.isfinite(net.iloc[entry_i]) else 0.0
        armed = True
        # 主力户 = 截至当日自身|净|峰值最大那家(阵营内)
        campw = S["camp"].abs()
        for i in range(entry_i, end_i + 1):
            t = idx[i]
            nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
            if np.isfinite(nn):
                pk = max(pk, nn)
            if not armed and np.isfinite(nn) and nn >= pk * 0.85:
                armed = True
            if armed and np.isfinite(nn) and nn < pk * 0.70:
                armed = False
                later = fav.iloc[i - entry_i + 1:]
                prev_max = float(fav.iloc[:i - entry_i + 1].max())
                label = "震仓" if (len(later) and float(later.max()) > prev_max + 1.0) else "认错"
                # F1 主力户自砍
                upto = campw.iloc[max(0, entry_i - 60):i + 1]
                peaks_m = upto.max()
                lead = peaks_m.idxmax() if peaks_m.notna().any() else None
                f1 = np.nan
                if lead is not None and np.isfinite(campw.iloc[i][lead]) and peaks_m[lead] > 0:
                    f1 = campw.iloc[i][lead] / peaks_m[lead] * 100
                # F2/F3 对手
                f2 = f3 = f8 = np.nan
                if O is not None:
                    onet = O["net"]
                    f2 = float(onet.iloc[i] - onet.iloc[max(0, i - 5)]) if np.isfinite(onet.iloc[i]) else np.nan
                    ov = float(O["vwap"].iloc[i])
                    if np.isfinite(ov) and ov > 0:
                        f3 = -side * (float(px.iloc[i]) / ov - 1) * 100
                    if np.isfinite(onet.iloc[i]) and onet.iloc[i] > 0 and np.isfinite(nn):
                        f8 = nn / float(onet.iloc[i])
                f4 = side * (float(px.iloc[i]) / float(S["vwap"].iloc[i]) - 1) * 100 if np.isfinite(S["vwap"].iloc[i]) else np.nan
                f5 = side * (float(px.iloc[i]) / float(px.iloc[max(0, i - 5)]) - 1) * 100
                r1 = side * px.pct_change().iloc[max(0, i - 2):i + 1] * 100
                f6 = float(r1.min()) if len(r1) else np.nan
                f7 = np.nan
                if len(netn):
                    nn_t = netn[netn.index <= t]
                    if len(nn_t) >= 6:
                        f7 = float(nn_t.iloc[-1] - nn_t.iloc[-6])
                f9 = float(ropp.iloc[i] - ropp.iloc[max(0, i - 5)]) if len(ropp) else np.nan
                prof = float(fav.iloc[i - entry_i])
                events.append({"c": c, "side": "多" if side > 0 else "空", "date": t.date(),
                               "label": label, "prof": prof, "F1主力户%": f1, "F2对手5日增": f2,
                               "F3对手浮盈%": f3, "F4我方套%": f4, "F5五日逆行%": f5,
                               "F6单日最逆%": f6, "F7下届同向5日增": f7, "F8规模比": f8,
                               "F9散户对手5日增": f9})

ev = pd.DataFrame(events)
lines = [f"{v['name']} 震仓 vs 认错:卸30%事件特征表(事件 {len(ev)} 个;标签=之后是否再创有利极值)",
         "", ev.to_string(index=False, float_format=lambda x: f"{x:+.1f}" if np.isfinite(x) else "—"), ""]
lines.append("=== 两类事件的特征中位数对比 ===")
med = ev.groupby("label")[["prof", "F1主力户%", "F2对手5日增", "F3对手浮盈%", "F4我方套%",
                            "F5五日逆行%", "F6单日最逆%", "F7下届同向5日增", "F8规模比", "F9散户对手5日增"]].median()
lines.append(med.to_string(float_format=lambda x: f"{x:+.1f}"))
cnt = ev.groupby("label").size()
lines.append(f"\n事件数: {dict(cnt)}")
io.open(OUT / f"shakeout_vs_wrong_{code.lower()}.txt", "w", encoding="utf-8").write("\n".join(lines))
print("ok", len(ev))
