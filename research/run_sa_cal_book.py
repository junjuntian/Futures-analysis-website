"""纯碱「跨月对冲簿」验证(PLAN_SA_CAL_BOOK_v1)。跑法:python research/run_sa_cal_book.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / "sa_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "sa_seat.csv.gz"))
v = H.use("SA")
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
roll, _, _ = H.rolling_groups(seat, price, mkt.index)
GRP = list(roll.dropna().iloc[-1])
op, st = H.contract_prices(price)
P = op.combine_first(st)                      # 执行价:开盘,缺则结算
mains = [c for c in dict.fromkeys(mkt["main"]) if isinstance(c, str)]
nxt_of = {c: mains[i + 1] for i, c in enumerate(mains[:-1])}

idx = mkt.index
main_c = mkt["main"]
next_c = main_c.map(nxt_of)

def leg_ret(contract_series):
    """逐日:该腿合约自己的执行价日收益(换月日用新合约自己的前一日价)。"""
    r = pd.Series(np.nan, index=idx)
    for i in range(1, len(idx)):
        c = contract_series.iloc[i]
        if not isinstance(c, str) or c not in P.columns:
            continue
        col = P[c].dropna()
        t, t0 = idx[i], idx[i - 1]
        a, b = col.asof(t0), col.get(t, np.nan)
        if np.isfinite(a) and np.isfinite(b) and a:
            r.iloc[i] = b / a - 1
    return r

ret_sp = (leg_ret(main_c) - leg_ret(next_c)).fillna(0)

def net_on(member_or_grp, contract_series):
    if isinstance(member_or_grp, str):
        sub = seat[seat["member_key"] == member_or_grp]
    else:
        sub = seat[seat["member_key"].isin(member_or_grp)]
    w = sub.pivot_table(index="trade_date", columns="contract", values="net_off", aggfunc="sum").ffill()
    out = pd.Series(np.nan, index=idx)
    for c in contract_series.dropna().unique():
        if c not in w.columns:
            continue
        days = idx[contract_series == c]
        out.loc[days] = w[c].reindex(days.union(w.index)).ffill().reindex(days).values
    return out

def perf(d):
    d = pd.Series(d).dropna()
    if not len(d):
        return np.nan, np.nan, np.nan
    eq = (1 + d).cumprod()
    return ((float(eq.iloc[-1]) - 1) * 100,
            float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan,
            float((eq / eq.cummax() - 1).min()) * 100)

L = [f"纯碱跨月对冲簿(样本 {idx[0].date()} ~ {idx[-1].date()};价差=主力−次主力,两腿各自合约计价,T+1)", ""]
rng = np.random.default_rng(41)
cands = [("国泰君安",)*1, ("东证期货",), ("永安期货",), ("华泰期货",), ("中信期货",), ("海通期货",)]
tests = [(m[0], m[0]) for m in cands] + [("阵营合计(对照)", GRP)]
for name, who in tests:
    nm = net_on(who, main_c)
    nn = net_on(who, next_c)
    f, s = np.sign(nm), np.sign(nn)
    pos = pd.Series(0.0, index=idx)
    m = f.notna() & s.notna() & (f * s < 0)
    pos[m] = f[m]
    held = pos.shift(2)
    base = (held * ret_sp).dropna()
    cum, sh, mdd = perf(base)
    inmkt = float((held != 0).mean() * 100)
    flips = int((pos != pos.shift()).sum())
    arr, n = pos.values, len(pos)
    sh_l = []
    for k in range(500):
        off = int(rng.integers(20, n - 20))
        d2 = (pd.Series(np.roll(arr, off), index=idx).shift(2) * ret_sp).dropna()
        sh_l.append(float(d2.mean() / d2.std() * np.sqrt(242)) if d2.std() > 0 else 0.0)
    p_pl = float((np.array(sh_l) >= sh).mean()) if np.isfinite(sh) else 1.0
    _, sh_t2, _ = perf(pos.shift(3) * ret_sp)
    turn = (pos.shift(2) != pos.shift(3)).astype(float)
    cum_n, sh_n, _ = perf(held * ret_sp - turn * 0.002)
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in base.groupby(base.index.year)}
    ok = p_pl < 0.05 and (np.isfinite(sh_t2) and sh_t2 >= 0.8 * sh) and cum_n > 0
    L.append(f"{name}: 在场 {inmkt:.0f}%  累计 {cum:+.1f}%  夏普 {sh:.2f}  回撤 {mdd:+.1f}%  翻转 {flips}")
    L.append(f"  闸门: 安慰剂 p={p_pl:.3f}(×7={min(p_pl*7,1):.3f})  T+2 {sh_t2:.2f}(需≥{0.8*sh:.2f})"
             f"  扣成本 {cum_n:+.1f}%/{sh_n:.2f}  -> {'全过' if ok else '不过'}")
    L.append("  逐年: " + "  ".join(f"{y}:{vv:+.1f}%" for y, vv in sorted(ys.items())))
    L.append("")

txt = "\n".join(L)
io.open(OUT / "sa_cal_book.txt", "w", encoding="utf-8").write(txt)
