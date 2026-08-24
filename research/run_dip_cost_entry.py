"""「左侧分批 + 批次成本」进场回测(焦煤,PLAN 见 REPORT_DIP_COST_v1 文首)。

预注册口径(2026-08-24,运营者拍板批次成本口径):
- 阵营:现行 5 家里当日净多(净空)的成员;可见口径 net_off(官方行,ffill)。
- 逢跌加仓日(多头):阵营净加 >=1000 手 且(结算<=阵营滚动VWAP 或 近5日跌);
  空头镜像(结算>=VWAP 或 近5日涨)。区间:隔 <=3 交易日并段。
- 区间累计净加 >=5000 手起激活;批次成本 = 区间内逐日加仓 x 当日结算 加权(增量,PIT)。
- 进场:区间激活起至区间结束后 10 交易日内,结算 <= 批次成本(多;空镜像)
  -> 次日开盘按阵营方向进;每区间最多一笔。
- 出场(无价格止损):阵营 |净持仓| 自进场后峰值卸 >=30% -> 次日开盘走;
  或交割纪律(该合约窗口止点前 10 交易日强平);数据尽头未平标 open。
- 计价:该合约自己的开盘/结算(开盘 0/缺失用结算兜底)。
跑法:python research/run_dip_cost_entry.py JM [confirm阈值,默认5000]
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "JM"
CONFIRM = float(sys.argv[2]) if len(sys.argv) > 2 else 5000.0
ADD_MIN, GAP, TAIL, UNLOAD = 1000.0, 3, 10, 0.30
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
roll, _, _ = H.rolling_groups(seat, price, mkt.index)
GRP = list(roll.dropna().iloc[-1])

CONTRACTS = ["JM2401", "JM2405", "JM2409", "JM2501", "JM2505",
             "JM2509", "JM2601", "JM2605", "JM2609", "JM2701"]
settle_w = price.pivot_table(index="trade_date", columns="contract", values="settle", aggfunc="first").sort_index()
open_w = (price.assign(_o=price["open_price"].replace(0, np.nan))
               .pivot_table(index="trade_date", columns="contract", values="_o", aggfunc="first").sort_index())

trades = []
for c in CONTRACTS:
    sub = seat[(seat["member_key"].isin(GRP)) & (seat["contract"] == c)]
    if sub.empty or c not in settle_w.columns:
        continue
    px = settle_w[c].dropna()
    opx = open_w[c] if c in open_w.columns else pd.Series(dtype=float)
    w = sub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first")
    w = w.reindex(px.index).ffill()          # 可见口径,掉榜沿用前值
    for side, camp in ((+1, "多"), (-1, "空")):
        net = w.where(np.sign(w) == side).abs().sum(axis=1)   # 阵营合计|净|
        chg = net.diff()
        ret5 = px.pct_change(5)
        # 阵营滚动 VWAP(加仓按结算加权,减仓不动)
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
        # 状态机:区间/批次成本/进出场
        idx = list(px.index)
        zone_add = zone_cost = 0.0
        zone_last = None; zone_fired = False
        pos = None
        for i, t in enumerate(idx):
            # 交割纪律
            dleft = H.days_to_window_end(c, t)
            if pos and dleft <= H.RULES["exit_before_delivery"]:
                j = min(i + 1, len(idx) - 1)
                ep = opx.get(idx[j], np.nan)
                ep = float(ep) if np.isfinite(ep) else float(px[idx[j]])
                trades.append({**pos, "exit_date": idx[j], "exit_px": ep, "why": "交割"})
                pos = None
            if zone_last is not None and (i - idx.index(zone_last) if zone_last in idx else 99) > GAP + TAIL:
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
            # 出场:卸仓 30%
            if pos:
                pos["peak"] = max(pos["peak"], float(net.get(t, 0)))
                nn = float(net.get(t, np.nan))
                if np.isfinite(nn) and pos["peak"] > 0 and nn < pos["peak"] * (1 - UNLOAD):
                    j = min(i + 1, len(idx) - 1)
                    ep = opx.get(idx[j], np.nan)
                    ep = float(ep) if np.isfinite(ep) else float(px[idx[j]])
                    trades.append({**pos, "exit_date": idx[j], "exit_px": ep, "why": f"卸仓{(1-nn/pos['peak'])*100:.0f}%"})
                    pos = None
            # 进场
            if (pos is None and not zone_fired and zone_last is not None
                    and zone_add >= CONFIRM and dleft > H.RULES["exit_before_delivery"] + 5
                    and idx.index(zone_last) >= i - (GAP + TAIL)):
                ok = (float(px[t]) <= zone_cost) if side > 0 else (float(px[t]) >= zone_cost)
                if ok and i + 1 < len(idx):
                    ep = opx.get(idx[i + 1], np.nan)
                    ep = float(ep) if np.isfinite(ep) else float(px[idx[i + 1]])
                    pos = {"contract": c, "camp": camp, "side": side,
                           "entry_date": idx[i + 1], "entry_px": ep,
                           "batch_cost": round(zone_cost), "peak": float(net.get(t, 0))}
                    zone_fired = True
        if pos:
            trades.append({**pos, "exit_date": None, "exit_px": float(px.iloc[-1]), "why": "未平(数据尽头)"})

tr = pd.DataFrame(trades)
tr["ret"] = tr["side"] * (tr["exit_px"] / tr["entry_px"] - 1) * 100
tr = tr.sort_values("entry_date")
closed = tr[tr["exit_date"].notna()]

lines = [f"{v['name']} 左侧分批+批次成本 进场回测  confirm={CONFIRM:.0f}手  组: " + "、".join(GRP),
         f"出场:卸仓>={UNLOAD:.0%} 或交割纪律;无价格止损。数据至 {settle_w.index[-1].date()}", ""]
lines.append(f"{'合约':8s}{'向':3s}{'进场日':11s}{'进场价':>7s}{'批次成本':>8s}{'出场日':11s}{'出场价':>7s}{'收益%':>8s}  原因")
for _, r in tr.iterrows():
    ed = r["exit_date"].date() if pd.notna(r["exit_date"]) else "—"
    lines.append(f"{r['contract']:8s}{r['camp']:3s}{r['entry_date'].date()!s:11s}{r['entry_px']:>7,.0f}"
                 f"{r['batch_cost']:>8,.0f}{ed!s:11s}{r['exit_px']:>7,.0f}{r['ret']:>+8.1f}  {r['why']}")
x = closed["ret"]
lines.append("")
lines.append(f"已平 {len(x)} 笔: 均值{x.mean():+.2f}%  中位{x.median():+.2f}%  胜率{(x>0).mean()*100:.0f}%  "
             f"t={x.mean()/x.std()*np.sqrt(len(x)):+.2f}  最差{x.min():+.1f}%  最好{x.max():+.1f}%")
for camp in ("多", "空"):
    xx = closed[closed["camp"] == camp]["ret"]
    if len(xx):
        lines.append(f"  {camp}头 {len(xx)} 笔: 均值{xx.mean():+.2f}%  胜率{(xx>0).mean()*100:.0f}%")
# 安慰剂:同合约同方向随机进场日,持同样长度
rng = np.random.default_rng(7)
sims = []
for k in range(2000):
    tot = []
    for _, r in closed.iterrows():
        px = settle_w[r["contract"]].dropna()
        hold = max(1, (px.index.get_loc(r["exit_date"]) - px.index.get_loc(r["entry_date"])))
        i0 = rng.integers(0, max(1, len(px) - hold - 1))
        tot.append(r["side"] * (float(px.iloc[i0 + hold]) / float(px.iloc[i0]) - 1) * 100)
    sims.append(np.mean(tot))
sims = np.array(sims)
lines.append(f"安慰剂(同合约同方向随机日同持长,2000次): 均值{sims.mean():+.2f}%  "
             f"p(>=观测均值)={(sims >= x.mean()).mean():.3f}")
io.open(OUT / f"dip_cost_{code.lower()}_{CONFIRM:.0f}.txt", "w", encoding="utf-8").write("\n".join(lines))
tr.to_csv(OUT / f"dip_cost_trades_{code.lower()}_{CONFIRM:.0f}.csv", index=False, encoding="utf-8")
print("ok", len(tr))
