"""跨期表达验证(PLAN_PAIR_EXPRESS_v1):JM 双向合格战役的共存段与对冲效果。

跑法:仓库根目录 python research/run_pair_express.py JM
JM 参数按其规模注入(系数 1.0:add_min 1000/confirm 5000),share 25%,
跟批 max_units 3 —— 与生猪同一套规则形状,纯研究不部署。
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
import campaign as C

code = sys.argv[1] if len(sys.argv) > 1 else "JM"
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
if H.RULES.get("fixed_members"):
    GRP = list(H.RULES["fixed_members"])
else:
    roll, _, _ = H.rolling_groups(seat, price, mkt.index)
    GRP = list(roll.dropna().iloc[-1])
# JM 的 campaign 参数(系数 1.0),规则形状与生猪相同
H.RULES["campaign"] = {"add_min": 1000.0, "confirm": 5000.0, "gap": 3, "tail": 10,
                       "unload": 0.30, "share": 0.25, "max_units": 3}
H.RULES["strategy"] = "campaign"

out = C.run(seat, mkt, op, st, GRP, H.RULES)
trades = out["trades"]

# —— 重建逐日持仓簿(逐腿逐日收益)——
def leg_daily(t):
    c = t["contract"]
    px = st[c].dropna()
    opc = (op[c] if c in op.columns else pd.Series(np.nan, index=px.index)).reindex(px.index)
    e = pd.Timestamp(t["entry_date"])
    i0 = next((j for j, d in enumerate(px.index) if d > e), None)
    if i0 is None:
        return None
    if t["exit_date"] is None:
        i1 = len(px.index) - 1
    else:
        x = pd.Timestamp(t["exit_date"])
        i1 = next((j for j, d in enumerate(px.index) if d > x), len(px.index) - 1)
    sd = 1.0 if t["side"] == "long" else -1.0
    col = pd.Series(np.nan, index=mkt.index)
    prev = None
    for j in range(i0, i1 + 1):
        p = opc.iloc[j]
        p = float(p) if np.isfinite(p) else float(px.iloc[j])
        if prev is not None and prev > 0:
            col[px.index[j]] = sd * (p / prev - 1)
        prev = p
    return col

legs = []
for t in trades:
    col = leg_daily(t)
    if col is not None:
        legs.append((t, col))

# —— 共存段:同日 空腿(近月)与 多腿(更远月)都在场 ——
def month_key(c):
    return int(c[2:6])

daily_book = {}
for t, col in legs:
    for d in col.dropna().index:
        daily_book.setdefault(d, []).append(t)
coex_days = []
for d, ts in sorted(daily_book.items()):
    shorts = [t for t in ts if t["side"] == "short"]
    longs = [t for t in ts if t["side"] == "long"]
    pairs = [(s["contract"], lg["contract"]) for s in shorts for lg in longs
             if month_key(s["contract"]) < month_key(lg["contract"])]
    if pairs:
        coex_days.append((d, sorted(set(pairs))))

lines = [f"{v['name']} 跨期表达验证  战役 {len(trades)} 笔(空 "
         f"{sum(1 for t in trades if t['side']=='short')}/多 {sum(1 for t in trades if t['side']=='long')})", ""]
if not coex_days:
    lines.append("反向合格战役共存:0 天 —— 判定:不支持,配对层保持纯展示。")
else:
    # 分段
    segs = []
    cur = [coex_days[0]]
    for prev, now in zip(coex_days, coex_days[1:]):
        if (now[0] - prev[0]).days <= 5:
            cur.append(now)
        else:
            segs.append(cur); cur = [now]
    segs.append(cur)
    lines.append(f"共存 {len(coex_days)} 个交易日,{len(segs)} 段:")
    for sg in segs:
        d0, d1 = sg[0][0], sg[-1][0]
        prs = sorted({p for _, ps in sg for p in ps})
        lines.append(f"  {d0.date()} ~ {d1.date()}({len(sg)}日) 对: " +
                     "; ".join(f"空{a}多{b}" for a, b in prs))
    # 共存日的两腿收益相关与组合效果
    idx_co = [d for d, _ in coex_days]
    sh = pd.DataFrame({i: col for i, (t, col) in enumerate(legs) if t["side"] == "short"}).loc[idx_co].mean(axis=1, skipna=True)
    lg = pd.DataFrame({i: col for i, (t, col) in enumerate(legs) if t["side"] == "long"}).loc[idx_co].mean(axis=1, skipna=True)
    both = pd.concat([sh, lg], axis=1).dropna()
    if len(both) >= 10:
        corr = float(both.corr().iloc[0, 1])
        combo = both.mean(axis=1)
        lines.append("")
        lines.append(f"共存日两腿日收益相关: {corr:+.2f}(n={len(both)})")
        for name, ser in (("空腿", both.iloc[:, 0]), ("多腿", both.iloc[:, 1]), ("组合(等权)", combo)):
            eq = (1 + ser.fillna(0)).cumprod()
            dd = float((eq / eq.cummax() - 1).min()) * 100
            lines.append(f"  {name}: 段内累计 {(float(eq.iloc[-1])-1)*100:+.2f}%  日波动 {ser.std()*100:.2f}%  最大回撤 {dd:+.2f}%")
        # 价差视角
        lines.append("")
        for sg in segs:
            d0, d1 = sg[0][0], sg[-1][0]
            a, b = sg[0][1][0]
            if a in st.columns and b in st.columns:
                sp = (st[a] - st[b]).dropna()
                s0, s1 = sp.asof(d0), sp.asof(d1)
                if np.isfinite(s0) and np.isfinite(s1):
                    lines.append(f"  {d0.date()}~{d1.date()} 价差({a}−{b}): {s0:+.0f} -> {s1:+.0f}"
                                 f"(做空价差收益 {-(s1-s0):+.0f} 元/吨)")
io.open(OUT / f"pair_express_{code.lower()}.txt", "w", encoding="utf-8").write("\n".join(lines))
print("ok")
