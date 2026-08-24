"""生猪纯验证:焦煤两版策略原样搬运,只按预注册公式缩放手数阈值。

预注册(2026-08-24):
- 缩放系数 = 本品种逐届主导阵营|净|峰值中位 / 焦煤同口径(64,800 手);
  手数阈值 = 焦煤值 x 系数,取整到 50 手;百分比阈值一律不动。
- 席位组:RULES.fixed_members(生猪=DEC-122 固定5家),无则滚动组末值。
- 合约 = main_series 里出现过的主力届。
- 版本A(第一版):反复分批做——每个逢跌加仓区间一笔(区间确认+价<=批次成本),
  出场=阵营|净|<自进场峰值70%(单日)次日开盘;含安慰剂检验。
- 版本B(最新版):一届一方向只做首笔,出场对比 R1卸30 / R4散户到峰止盈 / PERF。
跑法:python research/run_lh_validation.py LH
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "LH"
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
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


def rd(x):
    return max(50.0, round(x * factor / 50) * 50)


ADD_MIN, CONFIRM, S3_ADD, RPEAK = rd(1000), rd(5000), rd(5000), rd(2000)
GAP, TAIL = 3, 10

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
                if ok:
                    opps.append(i)
                    zone_fired = True
        ropp = rw.where(np.sign(rw) == -side).abs().sum(axis=1) if len(rw.columns) else pd.Series(0.0, index=px.index)
        rpeak = ropp.cummax()
        stale5 = pd.Series([(ropp.iloc[max(0, i - 5):i + 1].idxmax() != t) if i >= 5 else False
                            for i, t in enumerate(px.index)], index=px.index)
        s4 = ((rpeak >= RPEAK) & (ropp >= rpeak * 0.95) & stale5).fillna(False)
        streams[(c, side)] = {"px": px, "op": op, "net": net, "idx": idx, "opps": opps, "s4": s4}


def opx_at(S, i):
    j = min(i + 1, len(S["idx"]) - 1)
    p = S["op"].iloc[j]
    return (float(p) if np.isfinite(p) else float(S["px"].iloc[j])), S["idx"][j]


# —— 版本A:反复分批 + 卸30快出 ——
trA = []
for (c, side), S in streams.items():
    px, net, idx = S["px"], S["net"], S["idx"]
    opp_at = set(S["opps"])
    pos = None
    for i, t in enumerate(idx):
        dleft = H.days_to_window_end(c, t)
        if pos and dleft <= H.RULES["exit_before_delivery"]:
            xp, xd = opx_at(S, i)
            trA.append({**pos, "exit_date": xd, "exit_px": xp, "why": "交割"}); pos = None
        if pos:
            nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
            if np.isfinite(nn):
                pos["peak"] = max(pos["peak"], nn)
                if nn < pos["peak"] * 0.70:
                    xp, xd = opx_at(S, i)
                    trA.append({**pos, "exit_date": xd, "exit_px": xp, "why": "卸30"}); pos = None
        if pos is None and i in opp_at and i + 1 < len(idx):
            if H.days_to_window_end(c, idx[i]) > H.RULES["exit_before_delivery"] + 5:
                ep, _ = opx_at(S, i)
                pos = {"contract": c, "side": side, "entry_date": idx[i + 1], "entry_px": ep,
                       "peak": float(net.iloc[i]) if np.isfinite(net.iloc[i]) else 0.0}
    if pos:
        trA.append({**pos, "exit_date": None, "exit_px": float(px.iloc[-1]), "why": "未平"})
A = pd.DataFrame(trA)
A["ret"] = A["side"] * (A["exit_px"] / A["entry_px"] - 1) * 100
closedA = A[A["exit_date"].notna()]

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

# —— 版本B:一届一波段,R1 / R4 / PERF ——
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
        rr = side * (xp / ep - 1) * 100
        trB[r].append({"c": c, "side": "多" if side > 0 else "空",
                       "entry": idx[e_i].date(), "exit": xd.date(), "ret": rr,
                       "why": r if fires[r] is not None else "交割"})

lines = [f"{v['name']} 纯验证(规则照搬焦煤,阈值缩放系数 {factor:.3f})  组: " + "、".join(GRP),
         f"阈值: 加仓日>={ADD_MIN:.0f}手 确认>={CONFIRM:.0f}手 散户峰>={RPEAK:.0f}手  数据至 {settle_w.index[-1].date()}", ""]
x = closedA["ret"]
hold = (pd.to_datetime(closedA["exit_date"]) - pd.to_datetime(closedA["entry_date"])).dt.days
lines.append(f"=== 版本A 反复分批+卸30快出:{len(x)} 笔 ===")
lines.append(f"均值{x.mean():+.2f}%  中位{x.median():+.2f}%  胜率{(x>0).mean()*100:.0f}%  "
             f"t={x.mean()/x.std()*np.sqrt(len(x)):+.2f}  最差{x.min():+.1f}%  合计{x.sum():+.1f}pp  "
             f"持有中位{hold.median():.0f}天  安慰剂p={p_val:.3f}")
closedA2 = closedA.copy()
closedA2["y"] = pd.to_datetime(closedA2["exit_date"]).dt.year
lines.append("逐年: " + "  ".join(f"{y}:{g['ret'].mean():+.1f}%x{len(g)}" for y, g in closedA2.groupby("y")))
lines.append("")
lines.append(f"=== 版本B 一届一波段({len(trB['R1'])} 届)===")
lines.append(f"{'规则':6s}{'均值%':>8s}{'中位%':>8s}{'胜%':>5s}{'最差%':>8s}{'合计pp':>8s}")
for r in ("R1", "R4", "PERF"):
    xx = pd.Series([t["ret"] for t in trB[r]])
    lines.append(f"{r:6s}{xx.mean():>+8.2f}{xx.median():>+8.2f}{(xx>0).mean()*100:>5.0f}{xx.min():>+8.1f}{xx.sum():>+8.1f}")
lines.append("")
lines.append("版本B R4 逐笔:")
for t in trB["R4"]:
    lines.append(f"  {t['c']} {t['side']}  {t['entry']} -> {t['exit']}  {t['ret']:+7.1f}%  {t['why']}")
io.open(OUT / f"lh_validation.txt", "w", encoding="utf-8").write("\n".join(lines))
A.to_csv(OUT / "lh_validation_A.csv", index=False, encoding="utf-8")
print("ok", len(A), len(trB["R1"]))
