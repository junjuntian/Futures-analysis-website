"""一届一波段:每届合约每方向只取第一次进场,持整轮,考出场指标能否找到顶。

结构(2026-08-24 运营者拍板:一届主力做一轮大波段,不反复进出):
- 进场:该(合约,方向)首个「批次确认>=5000 + 价<=批次成本」信号,次日开盘,唯一一笔。
- 窗口:进场 -> 交割纪律日(止点前10交易日)强制结束。
- 逐届列出:进场后最大有利日(顶)的日期与涨幅;顶±2日哪些指标在响;
  各出场规则的触发日、收益、以及吃到最大波段的百分比(capture)。
- 出场规则(全部次日开盘,单日出;无价格止损):
  R1 卸30:阵营|净|<自进场峰值70%;
  R2 顶信号(有浮盈):浮盈>0 且 (S2 冲高回落 或 S3 高潮加仓);
  R2b 顶信号(浮盈>=5%);
  R3 先到先走:R1 或 R2b;
  R4 散户到峰 S4(有浮盈);
  PERF 完美出顶(参照上限,不可交易)。
跑法:python research/run_campaign_exits.py JM
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

camps = []
for c in CONTRACTS:
    sub = seat[(seat["member_key"].isin(GRP)) & (seat["contract"] == c)]
    if sub.empty or c not in settle_w.columns:
        continue
    px = settle_w[c].dropna()
    w = sub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first").reindex(px.index).ffill()
    rsub = seat[(seat["member_key"].isin(RETAIL)) & (seat["contract"] == c)]
    rw = (rsub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first")
              .reindex(px.index).ffill()) if len(rsub) else pd.DataFrame(index=px.index)
    hi = high_w[c].reindex(px.index)
    lo = low_w[c].reindex(px.index)
    op = open_w[c].reindex(px.index)
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
        # 指标序列
        roll20 = (px.rolling(20).max() if side > 0 else px.rolling(20).min())
        fav_ext20 = (px >= roll20) if side > 0 else (px <= roll20)
        rng_ok = (hi - lo) > 0
        shadow = ((hi - px) / (hi - lo)).where(rng_ok) if side > 0 else ((px - lo) / (hi - lo)).where(rng_ok)
        s2 = (fav_ext20 & (shadow >= 0.5)).fillna(False)
        s3 = ((chg >= 5000) & fav_ext20).fillna(False)
        ropp = rw.where(np.sign(rw) == -side).abs().sum(axis=1) if len(rw.columns) else pd.Series(0.0, index=px.index)
        rpeak = ropp.cummax()
        stale5 = pd.Series([(ropp.iloc[max(0, i - 5):i + 1].idxmax() != t) if i >= 5 else False
                            for i, t in enumerate(px.index)], index=px.index)
        s4 = ((rpeak >= 2000) & (ropp >= rpeak * 0.95) & stale5).fillna(False)
        # 窗口终点
        end_i = next((i for i in range(entry_i, len(idx))
                      if H.days_to_window_end(c, idx[i]) <= H.RULES["exit_before_delivery"]), len(idx) - 1)
        camps.append({"c": c, "side": side, "px": px, "op": op, "net": net, "idx": idx,
                      "entry_i": entry_i, "end_i": end_i,
                      "s2": s2, "s3": s3, "s4": s4})


def exec_px(camp, i):
    """i 日触发 -> i+1 开盘成交(缺开盘用结算);不越过 end_i+1。"""
    j = min(i + 1, len(camp["idx"]) - 1)
    p = camp["op"].iloc[j]
    return (float(p) if np.isfinite(p) else float(camp["px"].iloc[j])), camp["idx"][j]


lines = [f"{v['name']} 一届一波段(进场=每届首个信号)  组: " + "、".join(GRP),
         f"数据至 {settle_w.index[-1].date()}", ""]
rules = ("R1卸30", "R4散户", "R7止血+共振顶", "R8亏撤+共振顶", "R8b亏撤+顶或散户", "PERF")
agg = {r: [] for r in rules}
cap = {r: [] for r in rules}
for camp in camps:
    c, side = camp["c"], camp["side"]
    px, net, idx = camp["px"], camp["net"], camp["idx"]
    e_i, end_i = camp["entry_i"], camp["end_i"]
    ep = camp["op"].iloc[e_i]
    ep = float(ep) if np.isfinite(ep) else float(px.iloc[e_i])
    seg = px.iloc[e_i:end_i + 1]
    fav = side * (seg / ep - 1) * 100
    top_i_rel = int(np.argmax(fav.values))
    top_i = e_i + top_i_rel
    top_ret = float(fav.iloc[top_i_rel])
    # 顶±2 指标
    near = slice(max(0, top_i - 2), min(len(idx), top_i + 3))
    at_top = [nm for nm, s in (("冲高回落", camp["s2"]), ("高潮加仓", camp["s3"]), ("散户到峰", camp["s4"]))
              if bool(s.iloc[near].any())]
    # 各规则触发
    def first_fire(cond):
        for i in range(e_i + 1, end_i + 1):
            if cond(i):
                return i
        return None
    peak = float(net.iloc[e_i]) if np.isfinite(net.iloc[e_i]) else 0.0
    peaks = []
    pk = peak
    for i in range(e_i, end_i + 1):
        nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
        if np.isfinite(nn):
            pk = max(pk, nn)
        peaks.append(pk)
    def unload30(i):
        nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
        return np.isfinite(nn) and nn < peaks[i - e_i] * 0.70
    prof = lambda i: side * (float(px.iloc[i]) / ep - 1) * 100
    fires = {
        "R1卸30": first_fire(unload30),
        "R2顶信号": first_fire(lambda i: prof(i) > 0 and (bool(camp["s2"].iloc[i]) or bool(camp["s3"].iloc[i]))),
        "R2b顶信号5%": first_fire(lambda i: prof(i) >= 5 and (bool(camp["s2"].iloc[i]) or bool(camp["s3"].iloc[i]))),
        "R4散户": first_fire(lambda i: prof(i) > 0 and bool(camp["s4"].iloc[i])),
        "PERF": top_i,
    }
    fires["R3先到"] = min([x for x in (fires["R1卸30"], fires["R2b顶信号5%"]) if x is not None], default=None)
    def top_any(i):
        return bool(camp["s2"].iloc[i]) or bool(camp["s3"].iloc[i]) or bool(camp["s4"].iloc[i])
    def top_conf(i):
        lo_ = max(0, i - 2)
        return sum(bool(camp[k].iloc[lo_:i + 1].any()) for k in ("s2", "s3", "s4")) >= 2
    fires["R6止血+单顶"] = first_fire(lambda i: (unload30(i) and prof(i) < 5) or (prof(i) >= 5 and top_any(i)))
    fires["R7止血+共振顶"] = first_fire(lambda i: (unload30(i) and prof(i) < 5) or (prof(i) >= 5 and top_conf(i)))
    fires["R8亏撤+共振顶"] = first_fire(lambda i: (unload30(i) and prof(i) < 0) or (prof(i) >= 5 and top_conf(i)))
    fires["R8b亏撤+顶或散户"] = first_fire(
        lambda i: (unload30(i) and prof(i) < 0)
        or (prof(i) >= 5 and (top_conf(i) or bool(camp["s4"].iloc[i]))))
    row = f"{c} {'多' if side>0 else '空'}  进 {idx[e_i].date()} @{ep:,.0f}  顶 {idx[top_i].date()} {top_ret:+.1f}%  顶±2指标:[{'、'.join(at_top) if at_top else '无'}]"
    lines.append(row)
    for r in rules:
        i = fires[r]
        if i is None:
            i = end_i  # 没触发 -> 交割纪律强平
            tag = "交割"
        else:
            tag = idx[i].date()
        xp, xd = exec_px(camp, i)
        rr = side * (xp / ep - 1) * 100
        agg[r].append(rr)
        cap[r].append(rr - top_ret)
        dist = i - top_i
        lines.append(f"    {r:12s} 触发 {tag}  出 {xd.date()} @{xp:,.0f}  收益{rr:+7.1f}%  距顶{dist:+d}日  少吃{rr-top_ret:+.1f}pp")
    lines.append("")
lines.append("=== 汇总(%.0f 届战役)===" % len(camps))
lines.append(f"{'规则':14s}{'均值%':>8s}{'中位%':>8s}{'胜%':>5s}{'最差%':>8s}{'合计pp':>8s}{'平均少吃pp':>10s}")
for r in rules:
    x = pd.Series(agg[r])
    lines.append(f"{r:14s}{x.mean():>+8.2f}{x.median():>+8.2f}{(x>0).mean()*100:>5.0f}{x.min():>+8.1f}{x.sum():>+8.1f}{np.mean(cap[r]):>+10.1f}")
io.open(OUT / f"campaign_exits_{code.lower()}.txt", "w", encoding="utf-8").write("\n".join(lines))
print("ok", len(camps))
