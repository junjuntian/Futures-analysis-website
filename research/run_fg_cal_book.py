"""玻璃跨月对冲簿(PLAN_FG_CAL_BOOK_v1)。跑法:python research/run_fg_cal_book.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / "fg_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "fg_seat.csv.gz"))
v = H.use("FG")
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
op, st = H.contract_prices(price)
P = op.combine_first(st)                       # 执行价:开盘缺则结算
idx = mkt.index
groups, log, cuts = H.rolling_groups(seat, price, idx)
if H.RULES.get("group_overrides"):
    groups, log = H.apply_group_overrides(groups, log, cuts, H.RULES["group_overrides"], seat, price)
GRP = list(groups.dropna().iloc[-1])

# 逐日逐合约的席位净持仓(可见口径)
def net_wide(members):
    sub = seat[seat["member_key"].isin(members)]
    return (sub.pivot_table(index="trade_date", columns="contract", values="net_off", aggfunc="sum")
              .reindex(idx).ffill())

def ym(c):
    return int("".join(ch for ch in str(c) if ch.isdigit())[:4])

def leg_ret(series_of_contract):
    """逐日:该腿当日所属合约自己的执行价日收益(换腿日用新合约自己的前一日价)。"""
    r = pd.Series(np.nan, index=idx)
    for i in range(1, len(idx)):
        c = series_of_contract.iloc[i]
        if not isinstance(c, str) or c not in P.columns:
            continue
        col = P[c].dropna()
        a, b = col.asof(idx[i - 1]), col.get(idx[i], np.nan)
        if np.isfinite(a) and np.isfinite(b) and a:
            r.iloc[i] = b / a - 1
    return r

def perf(d):
    d = pd.Series(d).dropna()
    if not len(d):
        return np.nan, np.nan, np.nan
    eq = (1 + d).cumprod()
    return ((float(eq.iloc[-1]) - 1) * 100,
            float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan,
            float((eq / eq.cummax() - 1).min()) * 100)

mains = mkt["main"]
# 近月候选:当日有行情、月份小于主力、未过窗口止点的最近一个
active = {}
for i, d in enumerate(idx):
    m = mains.iloc[i]
    if not isinstance(m, str):
        continue
    cands = [c for c in P.columns if isinstance(c, str) and ym(c) < ym(m)
             and np.isfinite(P[c].asof(d)) and H.days_to_window_end(c, d) > 0]
    active[d] = max(cands, key=ym) if cands else None

L = [f"玻璃跨月对冲簿(样本 {idx[0].date()} ~ {idx[-1].date()},n={len(idx)})", ""]
rng = np.random.default_rng(61)
results = {}
for src_name, members in (("永安", ["永安期货"]), ("阵营", GRP)):
    W = net_wide(members)
    for cand in ("E1 固定腿(主力vs近月)", "E2 忠实结构(最大多腿vs最大空腿)"):
        far_c = pd.Series(index=idx, dtype=object)
        near_c = pd.Series(index=idx, dtype=object)
        pos = pd.Series(0.0, index=idx)
        for i, d in enumerate(idx):
            m = mains.iloc[i]
            if not isinstance(m, str):
                continue
            if cand.startswith("E1"):
                n = active.get(d)
                if not n or m not in W.columns or n not in W.columns:
                    continue
                a, b = W[m].iloc[i], W[n].iloc[i]
                if not (np.isfinite(a) and np.isfinite(b)) or a * b >= 0:
                    continue      # 同向或缺数据 -> 不在场
                far_c.iloc[i], near_c.iloc[i] = m, n
                pos.iloc[i] = np.sign(a)
            else:
                row = W.iloc[i].dropna()
                row = row[[c for c in row.index if isinstance(c, str)
                           and np.isfinite(P[c].asof(d)) and H.days_to_window_end(c, d) > 0]]
                if row.empty:
                    continue
                longs, shorts = row[row > 0], row[row < 0]
                if longs.empty or shorts.empty:
                    continue
                f, n = longs.idxmax(), shorts.idxmin()
                if ym(f) <= ym(n):
                    continue      # 要求多腿在远月,否则不在场
                far_c.iloc[i], near_c.iloc[i] = f, n
                pos.iloc[i] = 1.0
        ret_sp = (leg_ret(far_c) - leg_ret(near_c)).fillna(0)
        held = pos.shift(2)
        base = (held * ret_sp).dropna()
        cum, sh, mdd = perf(base)
        inmkt = float((held != 0).mean() * 100)
        flips = int((pos != pos.shift()).sum())
        arr, n_ = pos.values, len(pos)
        sh_l = []
        for k in range(500):
            off = int(rng.integers(20, n_ - 20))
            d2 = (pd.Series(np.roll(arr, off), index=idx).shift(2) * ret_sp).dropna()
            sh_l.append(float(d2.mean() / d2.std() * np.sqrt(242)) if d2.std() > 0 else 0.0)
        p_pl = float((np.array(sh_l) >= sh).mean()) if np.isfinite(sh) else 1.0
        _, sh_t2, _ = perf(pos.shift(3) * ret_sp)
        turn = (pos.shift(2) != pos.shift(3)).astype(float)
        cum_n, sh_n, _ = perf(held * ret_sp - turn * 0.002)
        ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in base.groupby(base.index.year)}
        ok = p_pl < 0.05 and (np.isfinite(sh_t2) and sh_t2 >= 0.8 * sh) and cum_n > 0
        results[f"{src_name}|{cand}"] = base
        L.append(f"{src_name} {cand}: 在场 {inmkt:.0f}%  累计 {cum:+.1f}%  夏普 {sh:.2f}  回撤 {mdd:+.1f}%  切换 {flips}")
        L.append(f"  闸门: 安慰剂 p={p_pl:.3f}(×4={min(p_pl*4,1):.3f})  T+2 {sh_t2:.2f}(需≥{0.8*sh:.2f})"
                 f"  扣成本 {cum_n:+.1f}%/{sh_n:.2f}  -> {'全过' if ok else '不过'}")
        L.append("  逐年: " + "  ".join(f"{y}:{vv:+.0f}%" for y, vv in sorted(ys.items())))
        L.append("")

txt = "\n".join(L)
io.open(OUT / "fg_cal_book.txt", "w", encoding="utf-8").write(txt)
