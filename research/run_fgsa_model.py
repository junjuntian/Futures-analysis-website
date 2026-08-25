"""玻璃/纯碱完整模型套装(PLAN_FGSA_MODEL_v1;JD 档按 PLAN_JD_MODEL_v1 加)。
跑法:python research/run_fgsa_model.py [FG|SA|JD]"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
import campaign as C

code = sys.argv[1] if len(sys.argv) > 1 else "FG"
K = {"FG": 0.87, "SA": 1.18, "JD": 0.37}[code]   # 规模系数,预注册写死(JD 见 PLAN_JD_MODEL_v1)
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
# campaign 引擎假设合约行情不早于 mkt 索引(JM/LH 数据天然如此);
# FG 行情早于 replay_start,裁齐再进引擎,口径与焦煤/生猪一致。
_rs = pd.Timestamp(H.RULES["replay_start"])
price = price[price["trade_date"] >= _rs]
seat = seat[seat["trade_date"] >= _rs]
# 采集坏日整体剔除(JD 2026-07-31/08-03/08-05:akshare 只有结算价,量/仓/开盘全缺
# 且每合约重复 3 行)——留着会让 campaign 逐日矩阵与逐笔对不上(两边索引不一致)。
_ok_days = price.dropna(subset=["open_interest"])["trade_date"].unique()
price = price[price["trade_date"].isin(_ok_days)]
seat = seat[seat["trade_date"].isin(_ok_days)]
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
roll, _, _ = H.rolling_groups(seat, price, mkt.index)
GRP = list(roll.dropna().iloc[-1])
mains = [c for c in dict.fromkeys(mkt["main"]) if isinstance(c, str)]
L = [f"{v['name']} 完整模型套装(数据至 {mkt.index[-1].date()};k={K};阵营={GRP})", ""]


def rankcorr(a, b):
    ra, rb = pd.Series(a).rank(), pd.Series(b).rank()
    return float(np.corrcoef(ra, rb)[0, 1]) if len(ra) > 2 else np.nan


def perf(daily):
    dd = pd.Series(daily).dropna()
    if not len(dd):
        return np.nan, np.nan, np.nan
    eq = (1 + dd).cumprod()
    mdd = float((eq / eq.cummax() - 1).min()) * 100
    sh = float(dd.mean() / dd.std() * np.sqrt(242)) if dd.std() > 0 else np.nan
    return (float(eq.iloc[-1]) - 1) * 100, sh, mdd


# ============================== A. 战役制 ==============================
H.RULES["campaign"] = {"add_min": 1000.0 * K, "confirm": 5000.0 * K, "gap": 3, "tail": 10,
                       "unload": 0.30, "share": 0.25, "max_units": 3}
H.RULES["strategy"] = "campaign"
out = C.run(seat, mkt, op, st, GRP, H.RULES)
trades = out["trades"]
closed = [t for t in trades if t["exit_date"] is not None]
rets = np.array([t["ret_pct"] for t in closed])
L.append(f"── A. 战役制(add_min {1000*K:.0f}/confirm {5000*K:.0f})──")
if len(closed) < 8:
    L.append(f"战役仅 {len(closed)} 笔已平,样本不足,判定:不支持。")
    A_pass = False
else:
    t_stat = float(rets.mean() / rets.std(ddof=1) * np.sqrt(len(rets)))
    L.append(f"战役 {len(trades)} 笔(已平 {len(closed)}) 均 {rets.mean():+.2f}%/笔 中位 {np.median(rets):+.2f}%"
             f" 胜率 {(rets>0).mean()*100:.0f}%  t={t_stat:.2f}  合计 {rets.sum():+.1f}pp  最差 {rets.min():+.1f}%")
    rng = np.random.default_rng(7)
    lens = [(t["contract"], t["side"],
             max(1, len(st[t["contract"]].dropna().loc[t["entry_date"]:t["exit_date"]]) - 1))
            for t in closed]
    sims = []
    for k in range(2000):
        tot = []
        for c, side, n in lens:
            px = st[c].dropna()
            if len(px) <= n + 2:
                continue
            i0 = int(rng.integers(0, len(px) - n - 1))
            sd = 1.0 if side == "long" else -1.0
            r = sd * (float(px.iloc[i0 + n]) / float(px.iloc[i0]) - 1) * 100
            if np.isfinite(r):
                tot.append(r)
        sims.append(np.mean(tot))
    sims = np.array(sims)
    p_val = float((sims >= rets.mean()).mean())
    L.append(f"闸门1 安慰剂: 随机均值 {sims.mean():+.2f}%/笔  p = {p_val:.3f} -> {'过' if p_val < 0.05 else '不过'}")
    yr = {}
    for t in closed:
        yr.setdefault(t["entry_date"][:4], []).append(t["ret_pct"])
    L.append("闸门2 逐年: " + "  ".join(f"{y}:{len(rs)}笔{np.mean(rs):+.1f}%" for y, rs in sorted(yr.items())))
    daily = out.get("daily")
    ser = (daily.mean(axis=1, skipna=True) if isinstance(daily, pd.DataFrame) else daily).dropna()
    cum, sh, mdd = perf(ser)
    L.append(f"闸门3 等权净值(扣成本): 复利 {cum:+.1f}%  夏普 {sh:.2f}  回撤 {mdd:+.1f}%")
    recent = [t["ret_pct"] for t in closed if t["entry_date"] >= "2024-01-01"]
    rec_ok = (np.mean(recent) > -1.0) if recent else True
    A_pass = bool(p_val < 0.05 and rec_ok)
    L.append(f"近三年(2024 起) {len(recent)} 笔 均 {np.mean(recent):+.2f}%/笔" if recent else "近三年无笔")
    L.append(f"A 判定: {'支持采纳候选' if A_pass else '不支持'}")
for t in trades:
    if t["exit_date"] is None:
        L.append(f"  在场活体: {t['contract']} {t['side']} {t['units']}批 均价 {t['entry_px']} 浮 {t['ret_pct']:+.1f}%")
L.append("")

# ============================== B. 单席位跟随 ==============================
L.append("── B. 单席位跟随(五家逐家,T+1 主力方向)──")
adjo = (1 + mkt["ret_open"].fillna(0)).cumprod()

def member_pos(member):
    sub = seat[seat["member_key"] == member]
    sig = pd.Series(np.nan, index=mkt.index)
    for c in mains:
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = mkt.index[mkt["main"] == c]
        sig.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
    p = np.sign(sig)
    p[p == 0] = np.nan
    return p.ffill(), sig

rows_b = []
for m in GRP:
    pos, rawsig = member_pos(m)
    base = pos.shift(2) * mkt["ret_open"]
    cum, sh, mdd = perf(base)
    flips = int((pos != pos.shift()).sum())
    d_ = base.dropna()
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in d_.groupby(d_.index.year)}
    pos_years = sum(1 for x in ys.values() if x > 0)
    inmkt = float(pos.shift(2).notna().mean() * 100)
    # 独立旁证:非重叠 5 日窗流向 IC
    dn = rawsig.diff(5)
    fwd = mkt["ret_open"].rolling(5).sum().shift(-5)
    sel = pd.concat([dn, fwd], axis=1).dropna().iloc[::5]
    ic = rankcorr(sel.iloc[:, 0], sel.iloc[:, 1])
    t_ic = ic * np.sqrt(max(len(sel) - 2, 1)) / np.sqrt(max(1 - ic**2, 1e-9)) if np.isfinite(ic) else np.nan
    rows_b.append({"m": m, "pos": pos, "sh": sh, "cum": cum, "mdd": mdd, "flips": flips,
                   "ys": ys, "pos_years": pos_years, "nyears": len(ys), "inmkt": inmkt,
                   "ic": ic, "t_ic": t_ic})
    L.append(f"{m}: 累计 {cum:+.1f}%  夏普 {sh:.2f}  回撤 {mdd:+.1f}%  翻转 {flips}"
             f"  正年 {pos_years}/{len(ys)}  在场 {inmkt:.0f}%  IC {ic:+.3f}(t {t_ic:+.2f})")

cands = [r for r in rows_b if r["sh"] > 0.7 and r["pos_years"] >= 0.75 * r["nyears"]]
if not cands:
    L.append("B 判定: 无人过「夏普>0.7 且 正年≥75%」的初筛,不支持单席位跟随。")
else:
    for r in sorted(cands, key=lambda x: -x["sh"]):
        m, pos = r["m"], r["pos"]
        base = (pos.shift(2) * mkt["ret_open"]).dropna()
        s0 = r["sh"]
        rng = np.random.default_rng(11)
        arr, n = pos.values, len(pos)
        sh_l = []
        for k in range(500):
            off = int(rng.integers(20, n - 20))
            p2 = pd.Series(np.roll(arr, off), index=pos.index)
            d2 = (p2.shift(2) * mkt["ret_open"]).dropna()
            sh_l.append(float(d2.mean() / d2.std() * np.sqrt(242)) if d2.std() > 0 else 0.0)
    # sourcery skip
        p_pl = float((np.array(sh_l) >= s0).mean())
        _, sh_t2, _ = perf(pos.shift(3) * mkt["ret_open"])
        turn = (pos.shift(2) != pos.shift(3)).astype(float)
        cum_n, sh_n, _ = perf(pos.shift(2) * mkt["ret_open"] - turn * 0.001)
        ok = p_pl < 0.05 and sh_t2 >= 0.8 * s0 and cum_n > 0
        L.append(f"  候选 {m}: 安慰剂 p={p_pl:.3f}  T+2 夏普 {sh_t2:.2f}(/T+1 {s0:.2f})"
                 f"  扣成本 {cum_n:+.1f}%/{sh_n:.2f}  -> {'全过' if ok else '不过'}"
                 f"  旁证IC t {r['t_ic']:+.2f}{'(显著)' if abs(r['t_ic'])>2 else '(不显著,仅回测支持)'}")
L.append("")

# ============================== C. 压力表 ==============================
L.append("── C. 移仓压力表(散户带符号剩仓 vs 交割前近−次价差,step=+4)──")
RETAIL = [m for m in H.RULES["retail_seed"] if m in set(seat["member_key"])]

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
rows_c = {30: [], 20: [], 10: []}
for c in mains[:-1]:
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
        rows_c[th].append({"main": c, "net": rn, "move": float(s1 - s0) / float(base) * 100})

rc20 = np.nan
for th in (30, 20, 10):
    rs = rows_c[th]
    if len(rs) < 6:
        L.append(f"锚点 ≤{th}: 样本 {len(rs)} 届,不足")
        continue
    nets = [r["net"] for r in rs]
    moves = [r["move"] for r in rs]
    rc = rankcorr(nets, moves)
    if th == 20:
        rc20 = rc
    med = np.median(nets)
    hi = [r["move"] for r in rs if r["net"] >= med]
    lo = [r["move"] for r in rs if r["net"] < med]
    L.append(f"锚点 ≤{th}: {len(rs)} 届  秩相关 {rc:+.2f}"
             f"  高剩仓组 {np.mean(hi):+.2f}%({(np.array(hi)<0).mean()*100:.0f}%届在跌)  低组 {np.mean(lo):+.2f}%")
neg_share = np.mean([1 if r["net"] < 0 else 0 for r in rows_c[20]]) * 100 if rows_c[20] else np.nan
L.append(f"净空届占比(≤20): {neg_share:.0f}%")
# 判据 PIT 双向
hist_nets, fired, idle = [], [], []
for r in rows_c[20]:
    n_, mv = r["net"], r["move"]
    f = None
    if len(hist_nets) >= 6:
        q3 = float(np.percentile(hist_nets, 75))
        q1 = float(np.percentile(hist_nets, 25))
        if n_ >= q3 and n_ > 0:
            f = ("short", -mv)
        elif n_ <= q1 and n_ < 0:
            f = ("long", +mv)
        (fired if f else idle).append((r["main"], f, mv))
    hist_nets.append(n_)
if fired:
    pnl = [x[1][1] for x in fired]
    sides = {s: sum(1 for x in fired if x[1][0] == s) for s in ("short", "long")}
    L.append(f"判据 PIT(≤20,双向): 触发 {len(fired)} 届(空价差 {sides['short']}/多价差 {sides['long']})"
             f"  收益 {np.mean(pnl):+.2f}%/届 胜 {(np.array(pnl)>0).mean()*100:.0f}%"
             f"  未触发 {len(idle)} 届原始价差均 {np.mean([x[2] for x in idle]):+.2f}%")
    L.append("  明细: " + "; ".join(f"{m[2:]}:{f[0]}→{f[1]:+.2f}%" for m, f, _ in fired))
else:
    L.append("判据 PIT: 零触发")
L.append("")

# ============================== D. 跨期(仅 A 过闸时)==============================
if A_pass:
    def month_key(c):
        return int("".join(ch for ch in c if ch.isdigit())[:4])
    book = {}
    for t in trades:
        c = t["contract"]
        px = st[c].dropna()
        e = pd.Timestamp(t["entry_date"])
        x = pd.Timestamp(t["exit_date"]) if t["exit_date"] else px.index[-1]
        for d in px.index:
            if e < d <= x:
                book.setdefault(d, []).append(t)
    coex = sum(1 for d, ts in book.items()
               if any(s["side"] == "short" and g["side"] == "long"
                      and month_key(s["contract"]) < month_key(g["contract"])
                      for s in ts for g in ts))
    L.append(f"── D. 跨期: 反向合格战役共存 {coex} 个交易日 ──")
else:
    L.append("── D. 跨期: A 未过闸,跳过 ──")

txt = "\n".join(L)
io.open(OUT / f"fgsa_model_{code.lower()}.txt", "w", encoding="utf-8").write(txt)
