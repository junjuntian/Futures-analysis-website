"""方向尺:各席位组合计净持仓的方向质量对比(PLAN_JM_SEAT_DIR_v1)。
跑法:仓库根目录 python research/run_seat_dir.py JM
口径:pos=sign(可见合计净持仓,官方行,DEC-108),收盘定、次日开盘成交
(pos.shift(2) x ret_open,DEC-090);round=同号连续段。"""
import sys, pathlib
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "JM"
D = pathlib.Path(__file__).resolve().parent / "data"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]


def pnl_size_table(hi: pd.Timestamp) -> pd.DataFrame:
    """截至 hi(不含)每家的总盈亏与平均|净持仓|(run_seat_big8 同口径)。"""
    d = seat[seat["trade_date"] < hi].merge(
        price[["contract", "trade_date", "settle"]], on=["contract", "trade_date"], how="inner")
    d = d.sort_values(["member_key", "contract", "trade_date"])
    g = d.groupby(["member_key", "contract"])
    d["prev_net"] = g["net"].shift()
    d["prev_settle"] = g["settle"].shift()
    gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
    d = d[d["prev_net"].notna() & (gap <= 5)].copy()
    d["dpx"] = (d["settle"] - d["prev_settle"]) * H.RULES["multiplier"]
    grp = d.groupby("member_key")
    t = pd.DataFrame({
        "pnl": grp.apply(lambda s: (s["dpx"] * s["prev_net"]).sum(), include_groups=False),
        "avg_abs": grp["prev_net"].apply(lambda s: s.abs().mean()),
        "days": grp["trade_date"].nunique()})
    return t[t["days"] >= H.RULES["member_min_days"]]


def big_pnl_top(hi: pd.Timestamp, k: int) -> tuple | None:
    t = pnl_size_table(hi)
    if len(t) < k:
        return None
    big = t[t["avg_abs"] >= t["avg_abs"].median()]
    if len(big) < k:
        return None
    return tuple(big.sort_values("pnl", ascending=False).head(k).index)


# —— 参赛组 ——
roll_a, log_a, cuts = H.rolling_groups(seat, price, mkt.index)
cuts_ts = [pd.Timestamp(c) for c in cuts if pd.Timestamp(c) <= mkt.index[-1]]

# B:滚动「大且赚钱」,与 A 同切点扩窗重选
picks = {}
for cut in cuts_ts:
    picks[cut] = big_pnl_top(cut, H.RULES["group_k"])
roll_b = pd.Series(index=mkt.index, dtype=object)
for d in mkt.index:
    valid = [c for c in cuts_ts if c <= d]
    roll_b[d] = picks[valid[-1]] if valid else None

# C:固定@截点(只用截点前数据选,只评截点之后)
fixed_cuts = [pd.Timestamp("2024-08-01"), pd.Timestamp("2025-08-01")]
fixed = {}
for fc in fixed_cuts:
    m = big_pnl_top(fc, H.RULES["group_k"])
    if m:
        fixed[fc] = m
# D:全样本选(样本内参照)
full_pick = big_pnl_top(mkt.index[-1] + pd.Timedelta(days=1), H.RULES["group_k"])


def const_groups(members: tuple, since: pd.Timestamp | None = None) -> pd.Series:
    s = pd.Series([members] * len(mkt.index), index=mkt.index, dtype=object)
    if since is not None:
        s[s.index < since] = None
    return s


def direction_daily(groups: pd.Series) -> tuple[pd.Series, pd.Series]:
    """返回 (日收益 s, 实际持有 pos held)。"""
    sig = H.signal_series(seat, groups)
    net = sig["net"]
    pos = np.sign(net)
    pos = pos.replace(0, np.nan).ffill().fillna(0)
    pos[groups.isna()] = np.nan  # 组未定义的日子不参赛
    pos = pos.ffill().fillna(0) if False else pos  # 保留 NaN 供窗口过滤
    held = pos.shift(2)
    s = held * mkt["ret_open"]
    return s, held


def rounds_stats(s: pd.Series, held: pd.Series) -> dict:
    h = held.fillna(0)
    rid = (h != h.shift()).cumsum()
    rets = []
    for _, seg in s.groupby(rid):
        hh = h.loc[seg.index[0]]
        if hh == 0:
            continue
        seg = seg.dropna()
        if seg.empty:
            continue
        rets.append(float(np.prod(1 + seg) - 1))
    rets = np.array(rets)
    if len(rets) == 0:
        return {"n": 0}
    w, l = rets[rets > 0], rets[rets <= 0]
    return {"n": len(rets), "win": len(w) / len(rets) * 100,
            "plr": (w.mean() / abs(l.mean())) if len(w) and len(l) and l.mean() != 0 else np.inf,
            "avg": rets.mean() * 100}


def eval_window(s: pd.Series, held: pd.Series, lo=None) -> dict:
    ss = s if lo is None else s[s.index >= lo]
    hh = held if lo is None else held[held.index >= lo]
    valid = hh.notna()
    ss = ss[valid]; hh = hh[valid]
    p = H._perf(ss)
    r = rounds_stats(ss, hh)
    d = ss.dropna()
    t = float(d.mean() / d.std() * np.sqrt(len(d))) if len(d) > 2 and d.std() > 0 else np.nan
    yearly = {}
    for y, seg in ss.groupby(ss.index.year):
        yearly[int(y)] = round((float(np.prod(1 + seg.fillna(0))) - 1) * 100, 1)
    exposure = float((hh != 0).mean() * 100)
    return {**p, **r, "t": t, "yearly": yearly, "expo": exposure}


def fwd_hits(groups: pd.Series) -> dict:
    """方向对未来 H 日(次日开盘→H 日后开盘)的命中率。"""
    sig = H.signal_series(seat, groups)
    pos = np.sign(sig["net"]).replace(0, np.nan).ffill()
    adjo = (1 + mkt["ret_open"].fillna(0)).cumprod()
    out = {}
    for hz in (5, 10, 20):
        fwd = adjo.shift(-(hz + 1)) / adjo.shift(-1) - 1
        m = pos.notna() & fwd.notna() & groups.notna()
        hit = (np.sign(fwd[m]) == pos[m]).mean() * 100
        out[hz] = round(float(hit), 1)
    return out


def show(name: str, groups: pd.Series, lo=None, hits=True):
    s, held = direction_daily(groups)
    windows = [("全评估段", lo), ("近一年", mkt.index[-1] - pd.Timedelta(days=365)),
               ("2026", pd.Timestamp("2026-01-01"))]
    print(f"\n### {name}")
    cur = [g for g in groups.dropna()]
    if cur:
        print("  当前名单:", "、".join(cur[-1]))
    for wname, wlo in windows:
        e = eval_window(s, held, wlo)
        if e.get("n", 0) == 0 and wname != "全评估段":
            continue
        plr = e.get("plr")
        plr_s = f"{plr:5.2f}" if plr not in (None, np.inf) and not np.isnan(plr) else "  inf"
        print(f"  {wname:6s} 累计{e['cum_pct']:>+8.1f}%  夏普{e['sharpe'] if e['sharpe'] is not None else float('nan'):>6.2f}  "
              f"回撤{e['max_dd_pct']:>6.1f}%  轮{e.get('n', 0):>3d}  胜{e.get('win', float('nan')):>5.1f}%  "
              f"盈亏比{plr_s}  t{e['t']:>+6.2f}  在场{e['expo']:>5.1f}%")
        if wname == "全评估段":
            print("        逐年:", "  ".join(f"{y}:{r:+.1f}%" for y, r in e["yearly"].items()))
    if hits:
        hh = fwd_hits(groups)
        print("  方向命中率(次日开盘起):", "  ".join(f"{k}日 {v}%" for k, v in hh.items()))


first_cut = cuts_ts[0]
print(f"{v['name']}  数据至 {seat['trade_date'].max().date()}  评估起点(首个切点)= {first_cut.date()}")
print(f"滚动切点:{[c.date().isoformat() for c in cuts_ts]}")
print("\nB 组各切点名单:")
for cut in cuts_ts:
    print(f"  {cut.date()}:", "、".join(picks[cut]) if picks[cut] else "(候选不足)")
for fc, m in fixed.items():
    print(f"C 固定@{fc.date()}:", "、".join(m))
print("D 固定@全样本(样本内参照):", "、".join(full_pick) if full_pick else "-")

show("A 现行滚动 alpha 前5", roll_a, lo=first_cut)
show("B 滚动 大且赚钱 前5", roll_b, lo=first_cut)
for fc, m in fixed.items():
    show(f"C 固定 大且赚钱 @{fc.date()}(只评截点后)", const_groups(m, since=fc), lo=fc, hits=False)
if full_pick:
    show("D 固定@全样本(样本内,参照)", const_groups(full_pick, since=first_cut), lo=first_cut, hits=False)

# —— 基准 ——
print("\n### 基准(同评估段)")
for name, sgn in (("恒定满仓做空", -1), ("买入持有", 1)):
    s = sgn * mkt["ret_open"]
    held = pd.Series(float(sgn), index=mkt.index)
    e = eval_window(s[s.index >= first_cut], held[held.index >= first_cut])
    print(f"  {name}: 累计{e['cum_pct']:+.1f}%  夏普{e['sharpe']:.2f}  回撤{e['max_dd_pct']:.1f}%")
    print("        逐年:", "  ".join(f"{y}:{r:+.1f}%" for y, r in e["yearly"].items()))
