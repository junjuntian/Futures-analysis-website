"""完整出场首秀(焦煤 16 届,预注册 2026-08-24,运营者拍板):

进场(冻结):每届每方向首个「批次确认>=5000 + 价<=批次成本」,次日开盘,一届一笔。
FULL 出场,先到先走,全部次日开盘:
  ① 涨停闸:单日逆行(结算对结算)<= -5%;
  ② 止盈:浮盈>0 且 散户对手到峰(S4:>=95%运行峰(>=2000) 且 5日不创新高);
  ③ 认错止血:阵营|净|<自进场峰值70% 且 对手阵营浮盈>=3% 且 对手5日增仓>0
     且 下届同向5日净加<5000(移仓豁免);
  ④ 交割纪律强平。
对照:R1 卸30 / R4 散户止盈(无止血)/ PERF 完美出顶。
跑法:python research/run_full_exit.py JM
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
open_w = (raw.assign(trade_date=pd.to_datetime(raw["trade_date"]),
                     _o=raw["open_price"].replace(0, np.nan))
             .pivot_table(index="trade_date", columns="contract", values="_o", aggfunc="first").sort_index())


def next_contract(c):
    yy, mm = int(c[2:4]), int(c[4:6])
    mm += 4
    if mm > 12:
        mm -= 12; yy += 1
    return f"{code}{yy:02d}{mm:02d}"


def side_series(c, side, px):
    sub = seat[(seat["member_key"].isin(GRP)) & (seat["contract"] == c)]
    w = sub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first").reindex(px.index).ffill()
    net = w.where(np.sign(w) == side).abs().sum(axis=1)
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
    return net, vwap


trades = {r: [] for r in ("FULL", "R1", "R4", "PERF")}
why_full = []
for c in CONTRACTS:
    if c not in settle_w.columns:
        continue
    px = settle_w[c].dropna()
    if px.empty:
        continue
    op = open_w[c].reindex(px.index) if c in open_w.columns else pd.Series(np.nan, index=px.index)
    dret = px.pct_change()
    rsub = seat[(seat["member_key"].isin(RETAIL)) & (seat["contract"] == c)]
    rw = (rsub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first")
              .reindex(px.index).ffill()) if len(rsub) else pd.DataFrame(index=px.index)
    nc = next_contract(c)
    subn = seat[(seat["member_key"].isin(GRP)) & (seat["contract"] == nc)]
    wn = (subn.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first")
              .ffill()) if len(subn) else pd.DataFrame()
    for side in (+1, -1):
        net, vwap = side_series(c, side, px)
        onet, ovwap = side_series(c, -side, px)
        netn = wn.where(np.sign(wn) == side).abs().sum(axis=1) if len(wn.columns) else pd.Series(dtype=float)
        chg = net.diff()
        ret5 = px.pct_change(5)
        losing = (px <= vwap) if side > 0 else (px >= vwap)
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
        ep = op.iloc[entry_i]
        ep = float(ep) if np.isfinite(ep) else float(px.iloc[entry_i])
        # S4 散户到峰
        ropp = rw.where(np.sign(rw) == -side).abs().sum(axis=1) if len(rw.columns) else pd.Series(0.0, index=px.index)
        rpeak = ropp.cummax()
        stale5 = pd.Series([(ropp.iloc[max(0, i - 5):i + 1].idxmax() != t) if i >= 5 else False
                            for i, t in enumerate(px.index)], index=px.index)
        s4 = ((rpeak >= 2000) & (ropp >= rpeak * 0.95) & stale5).fillna(False)
        prof = lambda i: side * (float(px.iloc[i]) / ep - 1) * 100
        pk = float(net.iloc[entry_i]) if np.isfinite(net.iloc[entry_i]) else 0.0
        peaks = []
        p_ = pk
        for i in range(entry_i, end_i + 1):
            nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
            if np.isfinite(nn):
                p_ = max(p_, nn)
            peaks.append(p_)

        def unload30(i):
            nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
            return np.isfinite(nn) and nn < peaks[i - entry_i] * 0.70

        def full_trig(i):
            if side * float(dret.iloc[i]) <= -0.05:
                return "涨停闸"
            if prof(i) > 0 and bool(s4.iloc[i]):
                return "散户止盈"
            if unload30(i):
                oprof = -side * (float(px.iloc[i]) / float(ovwap.iloc[i]) - 1) * 100 if np.isfinite(ovwap.iloc[i]) else np.nan
                ochg5 = float(onet.iloc[i] - onet.iloc[max(0, i - 5)]) if np.isfinite(onet.iloc[i]) else np.nan
                nx5 = np.nan
                if len(netn):
                    nn_t = netn[netn.index <= idx[i]]
                    if len(nn_t) >= 6:
                        nx5 = float(nn_t.iloc[-1] - nn_t.iloc[-6])
                if (np.isfinite(oprof) and oprof >= 3 and np.isfinite(ochg5) and ochg5 > 0
                        and not (np.isfinite(nx5) and nx5 >= 5000)):
                    return "认错止血"
            return None

        seg = px.iloc[entry_i:end_i + 1]
        fav = side * (seg / ep - 1) * 100
        top_i = entry_i + int(np.argmax(fav.values))
        fires = {"R1": None, "R4": None, "FULL": None, "PERF": top_i}
        fullwhy = None
        for i in range(entry_i + 1, end_i + 1):
            if fires["R1"] is None and unload30(i):
                fires["R1"] = i
            if fires["R4"] is None and prof(i) > 0 and bool(s4.iloc[i]):
                fires["R4"] = i
            if fires["FULL"] is None:
                w_ = full_trig(i)
                if w_:
                    fires["FULL"] = i
                    fullwhy = w_
        for r in trades:
            i = fires[r]
            why = fullwhy if r == "FULL" and i is not None else ("完美" if r == "PERF" else r)
            if i is None:
                i, why = end_i, "交割"
            j = min(i + 1, len(idx) - 1)
            xp = op.iloc[j]
            xp = float(xp) if np.isfinite(xp) else float(px.iloc[j])
            rr = side * (xp / ep - 1) * 100
            trades[r].append({"c": c, "side": "多" if side > 0 else "空", "entry": idx[entry_i].date(),
                              "exit": idx[j].date(), "ret": rr, "why": why})

lines = [f"{v['name']} 完整出场首秀(16 届)  组: " + "、".join(GRP), ""]
lines.append(f"{'规则':6s}{'均值%':>8s}{'中位%':>8s}{'胜%':>5s}{'最差%':>8s}{'合计pp':>8s}")
for r in ("FULL", "R1", "R4", "PERF"):
    x = pd.Series([t["ret"] for t in trades[r]])
    lines.append(f"{r:6s}{x.mean():>+8.2f}{x.median():>+8.2f}{(x>0).mean()*100:>5.0f}{x.min():>+8.1f}{x.sum():>+8.1f}")
lines.append("")
lines.append("FULL 逐笔:")
for t in trades["FULL"]:
    lines.append(f"  {t['c']} {t['side']}  {t['entry']} -> {t['exit']}  {t['ret']:+7.1f}%  {t['why']}")
io.open(OUT / f"full_exit_{code.lower()}.txt", "w", encoding="utf-8").write("\n".join(lines))
print("ok")
