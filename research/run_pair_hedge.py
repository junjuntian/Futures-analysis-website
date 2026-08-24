"""跨期表达回测(改进一,DEC-137 候选):对冲多腿豁免份额资格。

预注册(2026-08-24):
- 空头战役照现行(份额资格+跟批,max_units=3)。
- 多头信号(区间确认>=800 + 价<=批次成本,**不查份额资格**)在触发日,若
  空头簿里存在**交割月更早**的合约持仓 -> 作为对冲腿开 1 单位(空近多远结构)。
- 对冲腿出场:自身阵营卸30% / 自身交割纪律 / **空头簿清空**(没有空腿就不再是
  对冲,裸多不合格,平)。三者先到先走。
- 评估:①对冲腿逐笔;②组合逐日等权(空腿+对冲腿)vs 只空;③对照:同期
  全部裸多(无资格无配对)的成绩,看配对条件的选择效应。
跑法:python research/run_pair_hedge.py LH
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
import campaign as C

code = sys.argv[1] if len(sys.argv) > 1 else "LH"
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
GRP = list(H.RULES["fixed_members"])
cfg = H.RULES["campaign"]


def month_key(c):
    return int(c[2:6])


# —— 1. 现行空头簿(含跟批)——
out = C.run(seat, mkt, op, st, GRP, H.RULES)
short_open = {}   # date -> set(contracts)
all_dates = list(mkt.index)
for t in out["trades"]:
    if t["side"] != "short":
        continue
    c = t["contract"]
    px = st[c].dropna()
    e = pd.Timestamp(t["entry_date"])
    fill_in = next((d for d in px.index if d > e), None)
    if fill_in is None:
        continue
    if t["exit_date"] is None:
        fill_out = px.index[-1]
    else:
        x = pd.Timestamp(t["exit_date"])
        fill_out = next((d for d in px.index if d > x), px.index[-1])
    for d in mkt.index[(mkt.index >= fill_in) & (mkt.index <= fill_out)]:
        short_open.setdefault(d, set()).add(c)

# —— 2. 多头信号(无资格),配对条件判在信号日 ——
contracts = [c for c in dict.fromkeys(mkt["main"]) if isinstance(c, str)]
hedge, naked = [], []
for c in contracts:
    if c not in st.columns:
        continue
    px = st[c].dropna()
    if len(px) < 40:
        continue
    opc = (op[c] if c in op.columns else pd.Series(np.nan, index=px.index)).reindex(px.index)
    w = C.camp_frame(seat, c, GRP, px)
    net, vwap = C.camp_series(w, px, +1)
    z_add, z_cost, z_age, z_start = C.zone_scan(px, net, vwap, +1, cfg)
    dleft = pd.Series([H.days_to_window_end(c, t) for t in px.index], index=px.index)
    idx = list(px.index)
    fired = -1
    pos = None      # 对冲腿持仓(每合约一条流,同 campaign)
    naked_pos = None
    for i, t in enumerate(idx):
        def fill(j):
            p = opc.iloc[j]
            return float(p) if np.isfinite(p) else float(px.iloc[j])
        # 对冲腿出场
        if pos is not None:
            nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
            if np.isfinite(nn):
                pos["peak"] = max(pos["peak"], nn)
            reason = None
            if dleft.iloc[i] <= H.RULES["exit_before_delivery"]:
                reason = "交割"
            elif np.isfinite(nn) and pos["peak"] > 0 and nn < pos["peak"] * (1 - cfg["unload"]):
                reason = "卸仓"
            elif not short_open.get(t):
                reason = "空腿清空"
            if reason and i + 1 < len(idx):
                xp = fill(i + 1)
                hedge.append({"c": c, "in": pos["d"], "out": str(idx[i + 1].date()),
                              "ret": (xp / pos["px"] - 1) * 100, "why": reason})
                pos = None
        if naked_pos is not None:
            nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
            if np.isfinite(nn):
                naked_pos["peak"] = max(naked_pos["peak"], nn)
            reason = None
            if dleft.iloc[i] <= H.RULES["exit_before_delivery"]:
                reason = "交割"
            elif np.isfinite(nn) and naked_pos["peak"] > 0 and nn < naked_pos["peak"] * (1 - cfg["unload"]):
                reason = "卸仓"
            if reason and i + 1 < len(idx):
                xp = fill(i + 1)
                naked.append({"c": c, "in": naked_pos["d"], "ret": (xp / naked_pos["px"] - 1) * 100})
                naked_pos = None
        # 信号
        if (z_age.iloc[i] <= cfg["gap"] + cfg["tail"] and z_add.iloc[i] >= cfg["confirm"]
                and dleft.iloc[i] > H.RULES["exit_before_delivery"] + 5 and i + 1 < len(idx)):
            zid = int(z_start.iloc[i])
            if zid > fired:
                pcx = float(px.iloc[i])
                if pcx <= float(z_cost.iloc[i]):
                    fired = zid
                    entry = {"d": str(idx[i].date()), "px": fill(i + 1),
                             "peak": float(net.iloc[i]) if np.isfinite(net.iloc[i]) else 0.0}
                    if naked_pos is None:
                        naked_pos = dict(entry)
                    paired = any(month_key(a) < month_key(c) for a in short_open.get(t, ()))
                    if paired and pos is None:
                        pos = dict(entry)
    if pos is not None:
        hedge.append({"c": c, "in": pos["d"], "out": None,
                      "ret": (float(px.iloc[-1]) / pos["px"] - 1) * 100, "why": "未平"})
    if naked_pos is not None:
        naked.append({"c": c, "in": naked_pos["d"], "ret": (float(px.iloc[-1]) / naked_pos["px"] - 1) * 100})

lines = [f"{v['name']} 对冲多腿回测(空头簿=现行跟批版)", ""]
hc = [h for h in hedge if h["why"] != "未平"]
x = pd.Series([h["ret"] for h in hc])
lines.append(f"对冲多腿(配对开):{len(hc)} 笔已平  均值 {x.mean():+.2f}%  中位 {x.median():+.2f}%  "
             f"胜率 {(x>0).mean()*100:.0f}%  最差 {x.min():+.1f}%  合计 {x.sum():+.1f}pp" if len(hc) else "对冲多腿:0 笔")
for h in hedge:
    lines.append(f"  {h['c']}  {h['in']} -> {h['out'] or '未平'}  {h['ret']:+.2f}%  [{h['why']}]")
nx = pd.Series([n["ret"] for n in naked])
lines.append("")
lines.append(f"对照:全部裸多(无资格无配对){len(nx)} 笔  均值 {nx.mean():+.2f}%  胜率 {(nx>0).mean()*100:.0f}%  合计 {nx.sum():+.1f}pp")
lines.append("")
# 组合效果:空腿逐日 + 对冲腿逐日 等权
def daily_of(trs, sidesign):
    mat = pd.DataFrame(index=mkt.index)
    k = 0
    for t in trs:
        c = t["contract"] if "contract" in t else t["c"]
        px = st[c].dropna()
        e = pd.Timestamp(t["entry_date"] if "entry_date" in t else t["in"])
        i0 = next((j for j, d in enumerate(px.index) if d > e), None)
        if i0 is None:
            continue
        if (t.get("exit_date") or t.get("out")) is None:
            i1 = len(px.index) - 1
        else:
            xdt = pd.Timestamp(t.get("exit_date") or t.get("out"))
            i1 = next((j for j, d in enumerate(px.index) if d >= xdt), len(px.index) - 1)
        col = pd.Series(np.nan, index=mkt.index)
        prev = None
        for j in range(i0, i1 + 1):
            p = float(px.iloc[j])
            if prev is not None and prev > 0:
                col[px.index[j]] = sidesign * (p / prev - 1)
            prev = p
        mat[k] = col
        k += 1
    return mat

mat_s = daily_of([t for t in out["trades"] if t["side"] == "short"], -1)
mat_h = daily_of(hedge, +1)
for name, mat in (("只空(现行)", mat_s), ("空+对冲腿", pd.concat([mat_s, mat_h], axis=1))):
    dly = mat.mean(axis=1, skipna=True).fillna(0.0)
    p = H._perf(dly)
    lines.append(f"{name}: 累计 {p['cum_pct']:+.1f}%  夏普 {p['sharpe']}  回撤 {p['max_dd_pct']:+.1f}%")
io.open(OUT / "pair_hedge_lh.txt", "w", encoding="utf-8").write("\n".join(lines))
print("ok", len(hedge), len(naked))
