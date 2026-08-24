"""campaign 引擎 vs 研究蓝本(run_smart_filter2 版本A)逐笔对拍(DEC-133 门禁)。

比什么:每笔的 (合约, 方向, 进场成交日, 出场成交日, 出场原因归类) 必须完全一致;
收益只报差值(研究用简单收益、引擎按仓规矩逐日连乘,做空时天然有小差)。
跑法:仓库根目录 python research/run_campaign_parity.py LH
"""
import sys, pathlib
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
import campaign as C

code = sys.argv[1] if len(sys.argv) > 1 else "LH"
D = pathlib.Path(__file__).resolve().parent / "data"
raw = pd.read_csv(D / f"{code.lower()}_price.csv.gz")
price = H.clean_price(raw)
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
assert H.RULES.get("strategy") == "campaign", "该品种未配 campaign,无从对拍"
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
GRP = list(H.RULES["fixed_members"])
cfg = H.RULES["campaign"]
ADD_MIN, CONFIRM, GAP, TAIL = cfg["add_min"], cfg["confirm"], cfg["gap"], cfg["tail"]

# ============ 研究蓝本(照抄 run_smart_filter2.py 版本A,勿"顺手改进") ============
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
    qual[side] = rows.groupby("trade_date")["pnl"].sum().sort_index().cumsum()


def qualified(side, t):
    a = qual[side]; b = qual[-side]
    aa = a[a.index < t]; bb = b[b.index < t]
    av = float(aa.iloc[-1]) if len(aa) else 0.0
    bv = float(bb.iloc[-1]) if len(bb) else 0.0
    return av >= max(0.0, cfg["share"] * bv)


settle_w = st
open_w = op
CONTRACTS = [c for c in dict.fromkeys(mkt["main"]) if isinstance(c, str)]
ref = []
for c in CONTRACTS:
    sub = seat[(seat["member_key"].isin(GRP)) & (seat["contract"] == c)]
    if sub.empty or c not in settle_w.columns:
        continue
    px = settle_w[c].dropna()
    if len(px) < 40:
        continue
    opc = (open_w[c] if c in open_w.columns else pd.Series(np.nan, index=px.index)).reindex(px.index)
    w = sub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first").reindex(px.index).ffill()
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
                    zone_fired = True
        opp_at = set(opps)
        pos = None
        for i, t in enumerate(idx):
            dleft = H.days_to_window_end(c, t)
            def fill(j):
                p = opc.iloc[j]
                return float(p) if np.isfinite(p) else float(px.iloc[j])
            if pos and dleft <= H.RULES["exit_before_delivery"]:
                j = min(i + 1, len(idx) - 1)
                ref.append({"c": c, "side": side, "in": pos["fill_d"], "out": idx[j], "why": "交割",
                            "ret": side * (fill(j) / pos["px"] - 1) * 100})
                pos = None
            if pos:
                nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
                if np.isfinite(nn):
                    pos["peak"] = max(pos["peak"], nn)
                    if nn < pos["peak"] * 0.70:
                        j = min(i + 1, len(idx) - 1)
                        ref.append({"c": c, "side": side, "in": pos["fill_d"], "out": idx[j], "why": "卸仓",
                                    "ret": side * (fill(j) / pos["px"] - 1) * 100})
                        pos = None
            if pos is None and i in opp_at and i + 1 < len(idx):
                if H.days_to_window_end(c, idx[i]) > H.RULES["exit_before_delivery"] + 5:
                    pos = {"fill_d": idx[i + 1], "px": fill(i + 1),
                           "peak": float(net.iloc[i]) if np.isfinite(net.iloc[i]) else 0.0}
        if pos:
            ref.append({"c": c, "side": side, "in": pos["fill_d"], "out": None, "why": "未平",
                        "ret": side * (float(px.iloc[-1]) / pos["px"] - 1) * 100})

# ============ 引擎侧 ============
camp = C.run(seat, mkt, op, st, GRP, H.RULES)
eng = []
for t in camp["trades"]:
    c = t["contract"]
    px = settle_w[c].dropna()
    sig_d = pd.Timestamp(t["entry_date"])
    later = [d_ for d_ in px.index if d_ > sig_d]
    fill_in = later[0] if later else None
    if t["exit_date"] is None:
        fill_out = None
    else:
        ex_d = pd.Timestamp(t["exit_date"])
        later2 = [d_ for d_ in px.index if d_ > ex_d]
        fill_out = later2[0] if later2 else px.index[-1]
    eng.append({"c": c, "side": +1 if t["side"] == "long" else -1,
                "in": fill_in, "out": fill_out,
                "why": ("未平" if t["exit_reason"] is None and t["exit_date"] is None
                        else "交割" if t["exit_reason"] == "临近交割" else "卸仓"),
                "ret": t["ret_pct"]})

key = lambda r: (r["c"], r["side"], r["in"], r["out"], r["why"])
ref_k = sorted(key(r) for r in ref)
eng_k = sorted(key(r) for r in eng)
print(f"研究蓝本 {len(ref)} 笔 / 引擎 {len(eng)} 笔")
only_ref = [k for k in ref_k if k not in eng_k]
only_eng = [k for k in eng_k if k not in ref_k]
for k in only_ref[:10]:
    print("  只在蓝本:", k)
for k in only_eng[:10]:
    print("  只在引擎:", k)
if only_ref or only_eng:
    print("PARITY_FAIL")
    sys.exit(1)
rr = pd.Series(sorted(r["ret"] for r in ref))
ee = pd.Series(sorted(r["ret"] for r in eng))
print(f"收益(排序后逐位差,口径差异容忍): 最大 {float((rr-ee).abs().max()):.2f}pp  "
      f"蓝本合计 {rr.sum():+.1f}pp / 引擎合计 {ee.sum():+.1f}pp")
print("PARITY_OK")
