"""纯碱成本进场的锚:把「跟 0 比」换成「跟自己的滚动分布比」。

预注册见 `PLAN_SA_ANCHOR_v1.md`(提交 f267346,**先于本脚本**)。
跑法:`python research/run_sa_anchor.py`(产出 `research/out/sa_anchor.txt`)

现行那道门(`cost_entry_frame`)是:做多要 `价 ≤ 成本`、做空要 `价 ≥ 成本` ——
也就是 `gap = (价−成本)/价` 跟**固定的 0** 比。两个水平量不协整时那个 0 没有锚点
意义:纯碱的 gap 自 2023 年起停在 −2~−3%,门就常年关着(笔数 22/年 → 4/6)。

候选:把 0 换成 gap 自己的**滚动分位**。4 格,就这 4 格(预注册)。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
import hog_money as H  # noqa: E402

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)
YR = 242
MIN_PERIODS = 250          # 预注册
GRID = [(250, 0.50), (250, 0.35), (500, 0.50), (500, 0.35)]
REP = (250, 0.50)          # 预注册的代表格
RNG = np.random.default_rng(20260903)
L: list[str] = []


def setup(code="SA"):
    H.use(code)
    price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
    rs = pd.Timestamp(H.RULES["replay_start"])
    price, seat = price[price["trade_date"] >= rs], seat[seat["trade_date"] >= rs]
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= rs]
    groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
    return price, seat, mkt, groups


def rank_series(gap: pd.Series, n: int) -> pd.Series:
    """gap 在过去 n 个交易日(含当日)中的分位。只用过去与当日,不含未来。"""
    return gap.rolling(n, min_periods=MIN_PERIODS).apply(
        lambda w: float((w <= w[-1]).mean()), raw=True)


def cost_z_pct(sig, mkt, groups, n, q):
    """候选门:把 cost_entry_frame 的零门槛换成分位门,其余条件一字不动。"""
    unload = H.unload_series(sig, seat, groups)["pct"].reindex(mkt.index)
    cc = H.inst_cost_series(sig, mkt, groups)
    settle = mkt["settle"]
    gap = ((settle - cc["cost"].reindex(mkt.index)) / settle)
    rk = rank_series(gap, n)
    amp, umax = H.RULES["enter"] + 0.5, H.RULES["cost_unload_max"]
    net = sig["net"].reindex(mkt.index)
    z = pd.Series(0.0, index=mkt.index)
    for d in mkt.index:
        if not np.isfinite(net.get(d, np.nan)) or not np.isfinite(settle.get(d, np.nan)):
            continue
        side, cost = cc["side"].get(d, np.nan), cc["cost"].get(d, np.nan)
        if not np.isfinite(side) or side == 0 or not np.isfinite(cost):
            continue
        u = unload.get(d, np.nan)
        if not np.isfinite(u) or u > umax:
            continue
        r = rk.get(d, np.nan)
        if not np.isfinite(r):
            continue                      # 窗口没攒够 → 不判(评估期靠它对齐)
        if side > 0 and r <= q:
            z[d] = amp
        elif side < 0 and r >= 1 - q:
            z[d] = -amp
    return z, rk


def perf(closed, daily, lo=None):
    dl = pd.Series(daily).dropna()
    if lo is not None:
        dl = dl[dl.index >= lo]
        closed = [t for t in closed if pd.Timestamp(t["entry_date"]) >= lo]
    cum = (np.prod([1 + t["ret_pct"] / 100 for t in closed]) - 1) * 100 if closed else 0.0
    sh = float(dl.mean() / dl.std() * np.sqrt(YR)) if len(dl) > 2 and dl.std() > 0 else np.nan
    eq = (1 + dl).cumprod()
    dd = float((eq / eq.cummax() - 1).min() * 100) if len(eq) else np.nan
    return {"n": len(closed), "cum": cum, "sharpe": sh, "dd": dd,
            "win": (100 * np.mean([t["ret_pct"] > 0 for t in closed]) if closed else np.nan),
            "closed": closed, "daily": dl}


def replay_with(zcol, sig, mkt, price, lag_extra=0):
    op, st = H.contract_prices(price)
    rdf, _ = H.retail_series(seat, mkt.index)
    s2 = sig.assign(cost_z=zcol.reindex(sig.index),
                    cost_reason=pd.Series(None, index=sig.index, dtype=object))
    if lag_extra:
        s2 = s2.assign(cost_z=s2["cost_z"].shift(lag_extra).fillna(0.0))
    closed, _o, daily = H.replay(s2, mkt, rdf, op, st)[:3]
    return closed, daily


price, seat, mkt, groups = setup("SA")
sig = H.signal_series(seat, groups)
base_z = H.attach_cost_signal(sig, seat, mkt, groups)["cost_z"]

# 评估期:两条臂都可判的第一天(rank 攒够 MIN_PERIODS)
_z0, rk0 = cost_z_pct(sig, mkt, groups, 500, 0.5)
LO = rk0.dropna().index.min()

L += ["# 纯碱成本进场的锚:分位门 vs 零门槛(预注册 PLAN_SA_ANCHOR_v1)", "",
      f"数据至 {mkt.index[-1]:%Y-%m-%d};**评估期 {LO:%Y-%m-%d} 起**"
      f"(两条臂都可判的日子,基线在同一段上重算)", ""]

b_closed, b_daily = replay_with(base_z, sig, mkt, price)
B = perf(b_closed, b_daily, LO)
yrs = sorted({d.year for d in B["daily"].index})


def yearly(p):
    out = {}
    for y in yrs:
        dl = p["daily"][p["daily"].index.year == y]
        out[y] = float((1 + dl).prod() - 1) * 100 if len(dl) else np.nan
    return out


B_y = yearly(B)
span_y = (B["daily"].index[-1] - B["daily"].index[0]).days / 365.25

L += ["## 一、四格对比(基线 = 现行零门槛,同评估期重算)", "",
      f"{'规格':<18}{'笔数':>6}{'笔/年':>7}{'累计%':>10}{'夏普':>8}{'回撤%':>9}{'胜率%':>8}"]
L.append(f"{'S0 现行(零门槛)':<14}{B['n']:>6}{B['n']/span_y:>7.1f}{B['cum']:>10.1f}"
         f"{B['sharpe']:>8.2f}{B['dd']:>9.1f}{B['win']:>8.1f}")
res = {}
for n, q in GRID:
    z, _ = cost_z_pct(sig, mkt, groups, n, q)
    c, dd = replay_with(z, sig, mkt, price)
    P = perf(c, dd, LO)
    res[(n, q)] = P
    tag = f"S1 N={n} q={q:.2f}"
    L.append(f"{tag:<18}{P['n']:>6}{P['n']/span_y:>7.1f}{P['cum']:>10.1f}"
             f"{P['sharpe']:>8.2f}{P['dd']:>9.1f}{P['win']:>8.1f}")

# ---- 闸门 ----
L += ["", "## 二、闸门逐条", ""]
g1 = sum(1 for k, P in res.items()
         if P["sharpe"] >= B["sharpe"] and P["cum"] >= B["cum"])
L.append(f"G1 相邻档同向:4 格里双指标都不劣于基线的有 **{g1}** 格(要求 ≥3)"
         f" → {'过' if g1 >= 3 else '**不过**'}")

R = res[REP]
R_y = yearly(R)
ok_y = sum(1 for y in yrs if np.isfinite(R_y[y]) and np.isfinite(B_y[y]) and R_y[y] >= B_y[y])
L.append(f"G2 逐年(代表格 N={REP[0]} q={REP[1]}):候选 ≥ 基线的年份 **{ok_y}/{len(yrs)}**"
         f"(要求 ≥4/6) → {'过' if ok_y >= 4 else '**不过**'}")
L.append("      逐年 基线 vs 候选:" + "; ".join(
    f"{y} {B_y[y]:+.1f}% vs {R_y[y]:+.1f}%" for y in yrs))

rate = R["n"] / span_y
L.append(f"G3 笔数:代表格年均 **{rate:.1f}** 笔(要求 ≥8) → {'过' if rate >= 8 else '**不过**'}")

# G4 安慰剂:保持每年触发次数不变、随机重掷进场日
def placebo(P, reps=500):
    z = pd.Series(0.0, index=mkt.index)
    real = [pd.Timestamp(t["entry_date"]) for t in P["closed"]]
    sides = [1 if t["side"] == "long" else -1 for t in P["closed"]]
    if not real:
        return np.nan
    pool = {y: [d for d in mkt.index if d.year == y and d >= LO] for y in yrs}
    cnt = {y: sum(1 for d in real if d.year == y) for y in yrs}
    amp = H.RULES["enter"] + 0.5
    wins = 0
    for _ in range(reps):
        zz = z.copy()
        k = 0
        for y in yrs:
            if not cnt.get(y) or len(pool[y]) < cnt[y]:
                continue
            for d in RNG.choice(pool[y], size=cnt[y], replace=False):
                zz[d] = amp * sides[k % len(sides)]
                k += 1
        c2, d2 = replay_with(zz, sig, mkt, price)
        if perf(c2, d2, LO)["cum"] >= P["cum"]:
            wins += 1
    return (wins + 1) / (reps + 1)


p4 = placebo(R, reps=200)
L.append(f"G4 安慰剂(保持每年触发次数,重掷进场日,200 次):p = **{p4:.3f}**"
         f"(要求 <0.05) → {'过' if p4 < 0.05 else '**不过**'}")
L.append("      (引擎 replay 已扣单边 0.05% 换手成本,G5 与本行同源,不另算)")

z_rep, _ = cost_z_pct(sig, mkt, groups, *REP)
c_t2, d_t2 = replay_with(z_rep, sig, mkt, price, lag_extra=1)
T2 = perf(c_t2, d_t2, LO)
ok6 = np.isfinite(T2["sharpe"]) and np.isfinite(B["sharpe"]) and T2["sharpe"] >= 0.8 * B["sharpe"]
L.append(f"G6 延迟一天(T+2):夏普 {T2['sharpe']:.2f} vs 基线 {B['sharpe']:.2f} 的 80% "
         f"({0.8*B['sharpe']:.2f}) → {'过' if ok6 else '**不过**'}")

# ---- H2:选人周期 24 个月 ----
L += ["", "## 三、H2:重选周期 12 → 24 个月(判据不变,独立判)", ""]
start = mkt.index.min() + pd.Timedelta(days=H.RULES["warmup_days"])
cuts24 = pd.date_range(start, mkt.index.max(), freq="24MS")
picks, cur = {}, None
for c in cuts24:
    a = H.alpha_upto(seat, price, c)
    if len(a) >= H.RULES["group_k"]:
        cur = tuple(a.head(H.RULES["group_k"]).index)
    picks[c] = cur
g24 = pd.Series(index=mkt.index, dtype=object)
for d0 in mkt.index:
    v = [c for c in cuts24 if c <= d0]
    g24[d0] = picks[v[-1]] if v else None
sig24 = H.signal_series(seat, g24)
z24 = H.attach_cost_signal(sig24, seat, mkt, g24)["cost_z"]
op, st = H.contract_prices(price)
rdf, _ = H.retail_series(seat, mkt.index)
c24, _o, d24 = H.replay(sig24.assign(cost_z=z24), mkt, rdf, op, st)[:3]
P24 = perf(c24, d24, LO)
L.append(f"{'规格':<18}{'笔数':>6}{'累计%':>10}{'夏普':>8}{'回撤%':>9}")
L.append(f"{'S0 现行 12 个月':<14}{B['n']:>6}{B['cum']:>10.1f}{B['sharpe']:>8.2f}{B['dd']:>9.1f}")
L.append(f"{'S2 24 个月':<16}{P24['n']:>6}{P24['cum']:>10.1f}{P24['sharpe']:>8.2f}{P24['dd']:>9.1f}")

# H2 也要过闸(预注册写的是「闸门全部要过,缺一不上」),逐条跑:
P24_y = yearly(P24)
ok_y2 = sum(1 for y in yrs if np.isfinite(P24_y[y]) and np.isfinite(B_y[y]) and P24_y[y] >= B_y[y])
L += ["", f"G2 逐年:S2 ≥ S0 的年份 **{ok_y2}/{len(yrs)}**(要求 ≥4/6)"
          f" → {'过' if ok_y2 >= 4 else '**不过**'}",
      "      " + "; ".join(f"{y} {B_y[y]:+.1f}% vs {P24_y[y]:+.1f}%" for y in yrs)]
L.append(f"G3 笔数:年均 **{P24['n']/span_y:.1f}** 笔 → "
         f"{'过' if P24['n']/span_y >= 8 else '**不过**'}")

# 单笔均值差的 t —— 样本小的时候先算 t 再谈谁更好(PITFALLS 六)
a_ = np.array([t["ret_pct"] for t in B["closed"]])
b_ = np.array([t["ret_pct"] for t in P24["closed"]])
se = np.sqrt(a_.var(ddof=1) / len(a_) + b_.var(ddof=1) / len(b_))
tt = (b_.mean() - a_.mean()) / se if se > 0 else np.nan
L.append(f"      单笔均值 {a_.mean():+.2f}% → {b_.mean():+.2f}%,差异 **t = {tt:+.2f}**"
         f"({'分得开' if abs(tt) >= 2 else '**分不开,在噪音范围内**'})")

# G4 安慰剂:24 个月切点上随机选 5 家(从达到 min_days 的池子里),而不是按 alpha 前 5
def placebo_seats(reps=200):
    wins = 0
    pools = {}
    for c in cuts24:
        a = H.alpha_upto(seat, price, c)
        pools[c] = list(a.index)
    for _ in range(reps):
        pk, cu = {}, None
        for c in cuts24:
            if len(pools[c]) >= H.RULES["group_k"]:
                cu = tuple(RNG.choice(pools[c], size=H.RULES["group_k"], replace=False))
            pk[c] = cu
        gg = pd.Series(index=mkt.index, dtype=object)
        for d0 in mkt.index:
            v = [c for c in cuts24 if c <= d0]
            gg[d0] = pk[v[-1]] if v else None
        if gg.dropna().empty:
            continue
        s_ = H.signal_series(seat, gg)
        z_ = H.attach_cost_signal(s_, seat, mkt, gg)["cost_z"]
        c_, _oo, d_ = H.replay(s_.assign(cost_z=z_), mkt, rdf, op, st)[:3]
        if perf(c_, d_, LO)["cum"] >= P24["cum"]:
            wins += 1
    return (wins + 1) / (reps + 1)


p24 = placebo_seats(reps=200)
L.append(f"G4 安慰剂(24 个月切点上随机选 5 家,200 次):p = **{p24:.3f}**"
         f"(要求 <0.05) → {'过' if p24 < 0.05 else '**不过**'}")

c24b, _o2, d24b = H.replay(sig24.assign(cost_z=z24.shift(1).fillna(0.0)),
                           mkt, rdf, op, st)[:3]
T24 = perf(c24b, d24b, LO)
ok62 = np.isfinite(T24["sharpe"]) and T24["sharpe"] >= 0.8 * B["sharpe"]
L.append(f"G6 延迟一天(T+2):夏普 {T24['sharpe']:.2f} vs 基线 80%({0.8*B['sharpe']:.2f})"
         f" → {'过' if ok62 else '**不过**'}")
# G1 对 H2 同样要过 —— 24 个月是 DEC-193 从 25 格里挑出来的那一格,不补相邻档
# 就等于拿挑出来的那个点自己给自己背书。补 18 / 36 个月:这两档只可能证伪,不可能
# 帮 24 个月加分,所以不算「跑完加格」。
L.append("")
L.append("G1 相邻档同向(补 18 / 36 个月;这两档只可能证伪 24 个月):")
adj = {}
for mo in (18, 24, 36):
    cts = pd.date_range(start, mkt.index.max(), freq=f"{mo}MS")
    pk, cu = {}, None
    for c in cts:
        a = H.alpha_upto(seat, price, c)
        if len(a) >= H.RULES["group_k"]:
            cu = tuple(a.head(H.RULES["group_k"]).index)
        pk[c] = cu
    gg = pd.Series(index=mkt.index, dtype=object)
    for d0 in mkt.index:
        v = [c for c in cts if c <= d0]
        gg[d0] = pk[v[-1]] if v else None
    s_ = H.signal_series(seat, gg)
    z_ = H.attach_cost_signal(s_, seat, mkt, gg)["cost_z"]
    c_, _oo, d_ = H.replay(s_.assign(cost_z=z_), mkt, rdf, op, st)[:3]
    adj[mo] = perf(c_, d_, LO)
    L.append(f"      {mo:>2} 个月:{adj[mo]['n']:>3} 笔  累计 {adj[mo]['cum']:>+7.1f}%"
             f"  夏普 {adj[mo]['sharpe']:>5.2f}  回撤 {adj[mo]['dd']:>6.1f}%")
n_ok = sum(1 for mo in (18, 24, 36)
           if adj[mo]["sharpe"] >= B["sharpe"] and adj[mo]["cum"] >= B["cum"])
L.append(f"      三档里双指标都不劣于基线的有 **{n_ok}/3**(要求 ≥2,否则 24 个月是个尖峰)"
         f" → {'过' if n_ok >= 2 else '**不过**'}")

(OUT / "sa_anchor.txt").write_text("\n".join(L), encoding="utf-8")
print("\n".join(L))
