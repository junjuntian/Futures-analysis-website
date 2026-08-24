"""出场优化对比(焦煤,入场冻结 = REPORT_DIP_COST_v1 主档 confirm=5000)。

五种出场,同一套进场机会逐笔对比(2026-08-24 预注册):
- E1 基线:阵营|净|自进场后峰值卸 >=30%(单日)-> 次日开盘走。
- E2 深卸仓:卸 >=50% 且连续 2 日 -> 走(「出货到一定程度」才认)。
- E3 滞涨出货:阵营 >=10 交易日不创仓位新高 且 结算已优于批次成本 -> 走
  (「仓位不加了、价格还在走」= 出货开始,赶在卖压前离场)。
- E4 = E2 或 E3 先到先走。
- E5 散户到头(探索性):对手方向散户三家|净|>=运行峰值90% 且 5 日不创新高 -> 走。
全部叠加交割纪律强平;无价格止损。计价:该合约自己的开盘/结算。
跑法:python research/run_dip_cost_exits.py JM
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "JM"
CONFIRM, ADD_MIN, GAP, TAIL = 5000.0, 1000.0, 3, 10
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
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
open_w = (price.assign(_o=price["open_price"].replace(0, np.nan))
               .pivot_table(index="trade_date", columns="contract", values="_o", aggfunc="first").sort_index())

# —— 每个 (合约, 方向) 流:预计算 阵营净持仓 / 进场机会 / 散户对手序列 ——
streams = {}
for c in CONTRACTS:
    sub = seat[(seat["member_key"].isin(GRP)) & (seat["contract"] == c)]
    if sub.empty or c not in settle_w.columns:
        continue
    px = settle_w[c].dropna()
    opx = open_w[c] if c in open_w.columns else pd.Series(dtype=float)
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
        # 进场机会(独立于持仓状态):每个区间首个 结算<=批次成本 的日子
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
        # 散户对手合计|净|
        ropp = rw.where(np.sign(rw) == -side).abs().sum(axis=1) if len(rw.columns) else pd.Series(0.0, index=px.index)
        streams[(c, side)] = {"px": px, "opx": opx, "net": net, "idx": idx,
                              "opps": opps, "ropp": ropp}


def run_variant(name):
    trades = []
    for (c, side), S in streams.items():
        px, opx, net, idx, opps = S["px"], S["opx"], S["net"], S["idx"], S["opps"]
        ropp = S["ropp"]
        opp_at = {o["i"]: o for o in opps}
        pos = None
        below2 = 0
        for i, t in enumerate(idx):
            dleft = H.days_to_window_end(c, t)

            def close(why):
                j = min(i + 1, len(idx) - 1)
                ep = opx.get(idx[j], np.nan)
                ep = float(ep) if np.isfinite(ep) else float(px[idx[j]])
                trades.append({**pos, "exit_date": idx[j], "exit_px": ep, "why": why})

            if pos:
                if dleft <= H.RULES["exit_before_delivery"]:
                    close("交割"); pos = None
            if pos:
                nn = float(net.get(t, np.nan))
                if np.isfinite(nn):
                    if nn > pos["peak"]:
                        pos["peak"] = nn; pos["peak_i"] = i
                fav = (float(px[t]) > pos["batch_cost"]) if side > 0 else (float(px[t]) < pos["batch_cost"])
                trig = None
                if name == "E1" and np.isfinite(nn) and nn < pos["peak"] * 0.70:
                    trig = "卸30"
                if name in ("E2", "E4"):
                    if np.isfinite(nn) and nn < pos["peak"] * 0.50:
                        below2 += 1
                    else:
                        below2 = 0
                    if below2 >= 2:
                        trig = "卸50x2"
                if name in ("E3", "E4") and trig is None:
                    if i - pos["peak_i"] >= 10 and fav:
                        trig = "滞涨"
                if name == "E5":
                    r = float(ropp.get(t, 0.0))
                    pos["rpeak"] = max(pos.get("rpeak", 0.0), r)
                    if r > pos["rpeak"] - 1e-9:
                        pos["rpeak_i"] = i
                    if (pos.get("rpeak", 0) >= 2000 and r >= pos["rpeak"] * 0.9
                            and i - pos.get("rpeak_i", i) >= 5):
                        trig = "散户到头"
                if trig:
                    close(trig); pos = None; below2 = 0
            if pos is None and i in opp_at and i + 1 < len(idx):
                dl = H.days_to_window_end(c, idx[i])
                if dl > H.RULES["exit_before_delivery"] + 5:
                    ep = opx.get(idx[i + 1], np.nan)
                    ep = float(ep) if np.isfinite(ep) else float(px[idx[i + 1]])
                    nn = float(net.get(t, 0))
                    pos = {"contract": c, "side": side, "entry_date": idx[i + 1],
                           "entry_px": ep, "batch_cost": opp_at[i]["batch_cost"],
                           "peak": nn, "peak_i": i}
                    below2 = 0
        if pos:
            trades.append({**pos, "exit_date": None, "exit_px": float(px.iloc[-1]), "why": "未平"})
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
    sims = np.array(sims)
    return float((sims >= closed["ret"].mean()).mean()), float(sims.mean())


lines = [f"{v['name']} 出场对比(入场冻结:confirm=5000,价<=批次成本)  组: " + "、".join(GRP),
         f"数据至 {settle_w.index[-1].date()};散户名单: " + "、".join(RETAIL), ""]
lines.append(f"{'变体':6s}{'笔':>4s}{'均值%':>8s}{'中位%':>8s}{'胜%':>5s}{'t':>7s}{'持有(中位天)':>10s}{'最差%':>8s}{'合计pp':>8s}{'安慰剂p':>9s}")
detail = {}
for name in ("E1", "E2", "E3", "E4", "E5"):
    tr = run_variant(name)
    closed = tr[tr["exit_date"].notna()].copy()
    x = closed["ret"]
    hold = (pd.to_datetime(closed["exit_date"]) - pd.to_datetime(closed["entry_date"])).dt.days
    p, pmean = placebo_p(closed)
    lines.append(f"{name:6s}{len(x):>4d}{x.mean():>+8.2f}{x.median():>+8.2f}{(x>0).mean()*100:>5.0f}"
                 f"{x.mean()/x.std()*np.sqrt(len(x)):>+7.2f}{hold.median():>10.0f}{x.min():>+8.1f}{x.sum():>+8.1f}{p:>9.3f}")
    closed["y"] = pd.to_datetime(closed["exit_date"]).dt.year
    yr = "  ".join(f"{y}:{g['ret'].mean():+.1f}%x{len(g)}" for y, g in closed.groupby("y"))
    detail[name] = (tr, yr)
lines.append("")
for name in ("E1", "E2", "E3", "E4", "E5"):
    lines.append(f"{name} 逐年: {detail[name][1]}")
    tr = detail[name][0]
    op = tr[tr["exit_date"].isna()]
    for _, r in op.iterrows():
        lines.append(f"  {name} 未平: {r['contract']} {'多' if r['side']>0 else '空'} {r['entry_date'].date()} @{r['entry_px']:.0f} 浮{r['ret']:+.1f}%")
io.open(OUT / f"dip_cost_exits_{code.lower()}.txt", "w", encoding="utf-8").write("\n".join(lines))
for name in ("E1", "E2", "E3", "E4", "E5"):
    detail[name][0].to_csv(OUT / f"dip_cost_exits_{code.lower()}_{name}.csv", index=False, encoding="utf-8")
print("ok")
