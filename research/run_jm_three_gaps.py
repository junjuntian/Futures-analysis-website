"""焦煤三项空白全测(PLAN_JM_THREE_GAPS_v1)。跑法:python research/run_jm_three_gaps.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
import campaign as C

code = "JM"
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
roll, _, _ = H.rolling_groups(seat, price, mkt.index)
GRP = list(roll.dropna().iloc[-1])
mains = [c for c in dict.fromkeys(mkt["main"]) if isinstance(c, str)]
L = [f"焦煤三项空白全测(数据至 {mkt.index[-1].date()};阵营={GRP})", ""]


def rankcorr(a, b):
    """手写 Spearman(无 scipy)。"""
    ra = pd.Series(a).rank()
    rb = pd.Series(b).rank()
    return float(np.corrcoef(ra, rb)[0, 1]) if len(ra) > 2 else np.nan


# ============================== A. campaign 引擎重放 ==============================
H.RULES["campaign"] = {"add_min": 1000.0, "confirm": 5000.0, "gap": 3, "tail": 10,
                       "unload": 0.30, "share": 0.25, "max_units": 3}
H.RULES["strategy"] = "campaign"
out = C.run(seat, mkt, op, st, GRP, H.RULES)
trades = out["trades"]
closed = [t for t in trades if t["exit_date"] is not None]
rets = np.array([t["ret_pct"] for t in closed])
L.append("── A. 批次成本进场复裁(生产 campaign 引擎,参数系数 1.0)──")
L.append(f"战役 {len(trades)} 笔(已平 {len(closed)},在场 {len(trades)-len(closed)});"
         f"已平均值 {rets.mean():+.2f}%/笔  中位 {np.median(rets):+.2f}%  胜率 {(rets>0).mean()*100:.0f}%")
t_stat = float(rets.mean() / rets.std(ddof=1) * np.sqrt(len(rets))) if len(rets) > 2 else np.nan
L.append(f"t = {t_stat:.2f}   合计 {rets.sum():+.1f}pp   最差 {rets.min():+.1f}%")

# 闸门1 安慰剂:同合约同方向随机进场日、同持有长度(交易日),2000 次
rng = np.random.default_rng(7)
def hold_ret(c, side, i0, n):
    px = st[c].dropna()
    i1 = min(i0 + n, len(px) - 1)
    if i1 <= i0:
        return np.nan
    sd = 1.0 if side == "long" else -1.0
    return sd * (float(px.iloc[i1]) / float(px.iloc[i0]) - 1) * 100

sims = []
lens = [(t["contract"], t["side"],
         max(1, len(st[t["contract"]].dropna().loc[t["entry_date"]:t["exit_date"]]) - 1))
        for t in closed]
for k in range(2000):
    tot = []
    for c, side, n in lens:
        px = st[c].dropna()
        if len(px) <= n + 2:
            continue
        i0 = int(rng.integers(0, len(px) - n - 1))
        r = hold_ret(c, side, i0, n)
        if np.isfinite(r):
            tot.append(r)
    sims.append(np.mean(tot))
sims = np.array(sims)
p_val = float((sims >= rets.mean()).mean())
L.append(f"闸门1 安慰剂: 随机均值 {sims.mean():+.2f}%/笔  p(≥实际 {rets.mean():+.2f}) = {p_val:.3f}"
         f"  -> {'过' if p_val < 0.05 else '不过'}(前案研究实现 p=0.066)")

# 闸门2 逐年(按进场年)
yr = {}
for t in closed:
    yr.setdefault(t["entry_date"][:4], []).append(t["ret_pct"])
L.append("闸门2 逐年: " + "  ".join(f"{y}:{len(rs)}笔 均 {np.mean(rs):+.2f}%" for y, rs in sorted(yr.items())))

# 闸门3 扣成本复利(等权净值,引擎 daily 矩阵)
daily = out.get("daily")
if daily is not None and len(daily):
    ser = (daily.mean(axis=1, skipna=True) if isinstance(daily, pd.DataFrame) else daily).dropna()
    eq = (1 + ser).cumprod()
    sharpe = float(ser.mean() / ser.std() * np.sqrt(242)) if ser.std() > 0 else np.nan
    mdd = float((eq / eq.cummax() - 1).min()) * 100
    L.append(f"闸门3 等权净值(扣成本): 复利 {(float(eq.iloc[-1])-1)*100:+.1f}%  夏普 {sharpe:.2f}  回撤 {mdd:+.1f}%")
for t in trades:
    if t["exit_date"] is None:
        L.append(f"  在场活体: {t['contract']} {t['side']} {t['units']}批 均价 {t['entry_px']} 浮 {t['ret_pct']:+.1f}%")
L.append("")

# ============================== B. 焦煤版压力表 ==============================
L.append("── B. 焦煤版移仓压力表(散户带符号剩仓 vs 交割前近−次价差)──")
RETAIL = [m for m in H.RULES["retail_seed"] if m in set(seat["member_key"])]
L.append(f"散户名单在焦煤: {RETAIL}")

def retail_net_on(contract, upto):
    sub = seat[(seat["member_key"].isin(RETAIL)) & (seat["contract"] == contract)
               & (seat["trade_date"] <= upto)]
    if sub.empty:
        return None
    w = sub.pivot_table(index="trade_date", columns="member_key",
                        values="net_off", aggfunc="first").ffill()
    x = float(w.iloc[-1].sum())
    return x if np.isfinite(x) else None

nxt_of = {c: mains[i + 1] for i, c in enumerate(mains[:-1])}
rows = {30: [], 20: [], 10: []}
for c in mains[:-1]:                      # 当前届 JM2701 未完,不入历届
    n = nxt_of.get(c)
    if c not in st.columns or n not in st.columns:
        continue
    px_c = st[c].dropna()
    spread = (st[c] - st[n]).dropna()
    dl = pd.Series([H.days_to_window_end(c, t) for t in px_c.index], index=px_c.index)
    for th in (30, 20, 10):
        hit = dl[(dl <= th) & (dl > 5)]
        if not len(hit):
            continue
        t0 = hit.index[0]
        rn = retail_net_on(c, t0)
        if rn is None:
            continue
        endrows = dl[dl <= 5]
        t1 = endrows.index[0] if len(endrows) else px_c.index[-1]
        s0, s1 = spread.asof(t0), spread.asof(t1)
        base = px_c.asof(t0)
        if not (np.isfinite(s0) and np.isfinite(s1) and np.isfinite(base) and base):
            continue
        rows[th].append({"main": c, "t0": t0, "net": rn,
                         "move": float(s1 - s0) / float(base) * 100})

for th in (30, 20, 10):
    rs = rows[th]
    if len(rs) < 4:
        L.append(f"锚点 ≤{th}: 样本 {len(rs)} 届,不足")
        continue
    nets = [r["net"] for r in rs]
    moves = [r["move"] for r in rs]
    rc = rankcorr(nets, moves)
    med = np.median(nets)
    hi = [r["move"] for r in rs if r["net"] >= med]
    lo = [r["move"] for r in rs if r["net"] < med]
    L.append(f"锚点 ≤{th}: {len(rs)} 届  秩相关(净剩仓,价差变动) {rc:+.2f}"
             f"  高剩仓组 {np.mean(hi):+.2f}%({(np.array(hi)<0).mean()*100:.0f}%届在跌)"
             f"  低组 {np.mean(lo):+.2f}%")
L.append("逐届(锚点≤20): " + "; ".join(
    f"{r['main'][2:]}:净{r['net']:+,.0f}→{r['move']:+.2f}%" for r in rows[20]))

# 判据 PIT(expanding 分位,双向分支)
trig = []
hist_nets = []
for r in rows[20]:
    n_, mv = r["net"], r["move"]
    fired = None
    if len(hist_nets) >= 4:
        q3 = float(np.percentile(hist_nets, 75))
        q1 = float(np.percentile(hist_nets, 25))
        if n_ >= q3 and n_ > 0:
            fired = ("short_spread", -mv)         # 空价差赚的是 −move
        elif n_ <= q1 and n_ < 0:
            fired = ("long_spread", +mv)
    hist_nets.append(n_)
    trig.append((r["main"], fired, mv))
fired_rs = [(m, f[0], f[1]) for m, f, _ in trig if f]
idle_rs = [mv for m, f, mv in trig if f is None and len(hist_nets) >= 5]
L.append(f"判据 PIT(≤20,expanding,双向): 可判 {sum(1 for m,f,_ in trig if len(hist_nets)>=5)} 届"
         f"  触发 {len(fired_rs)} 届")
if fired_rs:
    pnl = [x for _, _, x in fired_rs]
    L.append("  触发明细: " + "; ".join(f"{m[2:]}:{s}→{x:+.2f}%" for m, s, x in fired_rs))
    L.append(f"  触发届收益 {np.mean(pnl):+.2f}%/届  胜 {(np.array(pnl)>0).mean()*100:.0f}%"
             f"  未触发届原始价差变动均 {np.mean(idle_rs):+.2f}%")
L.append("")

# ============================== C. 跨期复核 ==============================
L.append("── C. 跨期表达复核(合格反向战役共存,最新数据)──")
def month_key(c):
    return int(c[2:6])
def leg_days(t):
    c = t["contract"]
    px = st[c].dropna()
    e = pd.Timestamp(t["entry_date"])
    x = pd.Timestamp(t["exit_date"]) if t["exit_date"] else px.index[-1]
    return [d for d in px.index if e < d <= x]
book = {}
for t in trades:
    for d in leg_days(t):
        book.setdefault(d, []).append(t)
coex = []
for d, ts in sorted(book.items()):
    prs = [(s["contract"], g["contract"]) for s in ts if s["side"] == "short"
           for g in ts if g["side"] == "long" and month_key(s["contract"]) < month_key(g["contract"])]
    if prs:
        coex.append((d, sorted(set(prs))))
if not coex:
    L.append("反向合格战役共存: 0 天 —— 维持前案判定(纯展示)。")
else:
    segs = []
    cur = [coex[0]]
    for prev, now in zip(coex, coex[1:]):
        (segs.append(cur), cur := [now]) if (now[0] - prev[0]).days > 5 else cur.append(now)
    segs.append(cur)
    L.append(f"共存 {len(coex)} 个交易日,{len(segs)} 段(前案 7 日/3 段):")
    for sg in segs:
        prs = sorted({p for _, ps in sg for p in ps})
        L.append(f"  {sg[0][0].date()} ~ {sg[-1][0].date()}({len(sg)}日) " +
                 "; ".join(f"空{a}多{b}" for a, b in prs))

txt = "\n".join(L)
io.open(OUT / "jm_three_gaps.txt", "w", encoding="utf-8").write(txt)
print(txt)
