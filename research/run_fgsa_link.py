"""玻纯联动(PLAN_FGSA_LINK_v1):三候选交易 FG−SA 价差。跑法:python research/run_fgsa_link.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"

data = {}
for code in ("FG", "SA"):
    price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
    v = H.use(code)
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        groups, log = H.apply_group_overrides(groups, log, cuts, H.RULES["group_overrides"], seat, price)
    sig = H.signal_series(seat, groups)
    zwin = H.RULES["z_win"]
    # 永安在主力上的净持仓
    sub = seat[seat["member_key"] == "永安期货"]
    ya = pd.Series(np.nan, index=mkt.index)
    for c in dict.fromkeys(mkt["main"]):
        if not isinstance(c, str):
            continue
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = mkt.index[mkt["main"] == c]
        ya.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
    data[code] = {"mkt": mkt, "sig": sig, "ya": ya, "zwin": zwin}

idx = data["FG"]["mkt"].index.intersection(data["SA"]["mkt"].index)
idx = idx[idx >= pd.Timestamp("2020-06-01")]
ret_sp = (data["FG"]["mkt"]["ret_open"].reindex(idx).fillna(0)
          - data["SA"]["mkt"]["ret_open"].reindex(idx).fillna(0))

def expnorm(s):
    return s / s.expanding(60).std()

# C1 仓位水平差
lvl = {c: expnorm(data[c]["sig"]["net"]).reindex(idx) for c in ("FG", "SA")}
p1 = np.sign(lvl["FG"] - lvl["SA"])
# C2 流向差(生产 pair z 口径)
zz = {}
for c in ("FG", "SA"):
    chg = data[c]["sig"]["chg"]
    zz[c] = ((chg - chg.rolling(data[c]["zwin"], min_periods=60).mean())
             / chg.rolling(data[c]["zwin"], min_periods=60).std()).reindex(idx)
p2 = np.sign(zz["FG"] - zz["SA"])
# C3 永安跨品种
yan = {c: expnorm(data[c]["ya"]).reindex(idx) for c in ("FG", "SA")}
p3 = np.sign(yan["FG"] - yan["SA"])

def perf(d):
    d = pd.Series(d).dropna()
    if not len(d):
        return np.nan, np.nan, np.nan
    eq = (1 + d).cumprod()
    return ((float(eq.iloc[-1]) - 1) * 100,
            float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan,
            float((eq / eq.cummax() - 1).min()) * 100)

L = [f"玻纯联动三候选(共同样本 {idx[0].date()} ~ {idx[-1].date()},n={len(idx)};"
     f"价差=ret_open(FG)−ret_open(SA),T+1)", ""]

# 机制描述:阵营方向反向占比
dir_fg = np.sign(data["FG"]["sig"]["net"].reindex(idx))
dir_sa = np.sign(data["SA"]["sig"]["net"].reindex(idx))
both = pd.concat([dir_fg, dir_sa], axis=1).dropna()
opp = float((both.iloc[:, 0] != both.iloc[:, 1]).mean() * 100)
L.append(f"机制画像: 两品种阵营净持仓方向**反向**的天数占比 {opp:.0f}%(运营者「两边吃」画像)")
L.append("")

rng_master = np.random.default_rng(23)
results = {}
for name, pos in (("C1 仓位水平差", p1), ("C2 流向差(生产pair z)", p2), ("C3 永安跨品种", p3)):
    pos = pos.replace(0, np.nan).ffill()
    base = (pos.shift(2) * ret_sp).dropna()
    cum, sh, mdd = perf(base)
    flips = int((pos != pos.shift()).sum())
    # 闸门1 安慰剂
    arr, n = pos.values, len(pos)
    sh_l = []
    for k in range(500):
        off = int(rng_master.integers(20, n - 20))
        d2 = (pd.Series(np.roll(arr, off), index=pos.index).shift(2) * ret_sp).dropna()
        sh_l.append(float(d2.mean() / d2.std() * np.sqrt(242)) if d2.std() > 0 else 0.0)
    p_pl = float((np.array(sh_l) >= sh).mean())
    # 闸门2 T+2
    _, sh_t2, _ = perf(pos.shift(3) * ret_sp)
    # 闸门3 成本
    turn = (pos.shift(2) != pos.shift(3)).astype(float)
    cum_n, sh_n, mdd_n = perf(pos.shift(2) * ret_sp - turn * 0.002)
    d_ = base
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in d_.groupby(d_.index.year)}
    ok = p_pl < 0.05 and (np.isfinite(sh_t2) and sh_t2 >= 0.8 * sh) and cum_n > 0
    results[name] = (pos, ok, p_pl)
    L.append(f"{name}: 累计 {cum:+.1f}%  夏普 {sh:.2f}  回撤 {mdd:+.1f}%  翻转 {flips}({flips/6.2:.0f}/年)")
    L.append(f"  闸门: 安慰剂 p={p_pl:.3f}  T+2 夏普 {sh_t2:.2f}  扣成本 {cum_n:+.1f}%/{sh_n:.2f}"
             f"  -> {'全过' if ok else '不过'}(Bonferroni×3 p={min(p_pl*3,1):.3f})")
    L.append("  逐年: " + "  ".join(f"{y}:{vv:+.1f}%" for y, vv in sorted(ys.items())))
    L.append("")

# 过闸者与现行引擎相关(定位)
passed = [(n, pos) for n, (pos, ok, _) in results.items() if ok]
if passed:
    # 现行 FG 引擎与永安第二引擎日收益
    price = H.clean_price(pd.read_csv(D / "fg_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / "fg_seat.csv.gz"))
    H.use("FG")
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        groups, log = H.apply_group_overrides(groups, log, cuts, H.RULES["group_overrides"], seat, price)
    sig = H.signal_series(seat, groups)
    sig = H.attach_cost_signal(sig, seat, mkt, groups)
    rdf, rhave = H.retail_series(seat, mkt.index)
    _, _, daily_fg = H.replay(sig, mkt, rdf, op, st)
    ya_fg = data["FG"]["ya"]
    posy = np.sign(ya_fg).replace(0, np.nan).ffill()
    daily_ya = posy.shift(2) * data["FG"]["mkt"]["ret_open"]
    for n, pos in passed:
        d_c = (pos.shift(2) * ret_sp)
        for ref_name, ref in (("现行FG引擎", daily_fg), ("FG永安第二引擎", daily_ya)):
            b2 = pd.concat([d_c, ref], axis=1).dropna()
            L.append(f"{n} vs {ref_name}: 日收益相关 {float(b2.corr().iloc[0,1]):+.2f}")
        cur = pos.dropna()
        L.append(f"{n} 当前方向: {'做扩(多FG空SA)' if cur.iloc[-1]>0 else '做缩(空FG多SA)'}(截至 {cur.index[-1].date()})")
else:
    L.append("三候选全不过闸 —— 联动维持背景显示(pair_fgsa 原样)。")

txt = "\n".join(L)
io.open(OUT / "fgsa_link.txt", "w", encoding="utf-8").write(txt)
