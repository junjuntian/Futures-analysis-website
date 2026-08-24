"""焦煤「跟华泰」闸门验收(PLAN_JM_HUATAI_v1)。跑法:python research/run_jm_huatai.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code, MEMBER = "JM", "华泰期货"
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
sub = seat[seat["member_key"] == MEMBER]

def perf(daily):
    dd = daily.dropna()
    eq = (1 + dd).cumprod()
    mdd = float((eq / eq.cummax() - 1).min()) * 100
    sharpe = float(dd.mean() / dd.std() * np.sqrt(242)) if dd.std() > 0 else np.nan
    return (float(eq.iloc[-1]) - 1) * 100, sharpe, mdd

def pos_main(fresh_days=None):
    """主力单合约口径的方向序列;fresh_days 给定时,近 N 日无官方行则 NaN(空仓)。"""
    sig = pd.Series(np.nan, index=mkt.index)
    for c in dict.fromkeys(mkt["main"]):
        if not isinstance(c, str):
            continue
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = mkt.index[mkt["main"] == c]
        wf = w.reindex(days.union(w.index)).ffill().reindex(days)
        if fresh_days is not None:
            last_row = pd.Series(w.index, index=w.index).reindex(days.union(w.index)).ffill().reindex(days)
            stale = (days.to_series().values - last_row.values).astype("timedelta64[D]").astype(float) > fresh_days
            wf[stale] = np.nan
        sig.loc[days] = wf.values
    p = np.sign(sig)
    p[p == 0] = np.nan
    return p if fresh_days is not None else p.ffill()

def pos_variety():
    """品种合计口径:华泰在全部合约上的可见净持仓合计方向。"""
    w = sub.pivot_table(index="trade_date", columns="contract", values="net_off", aggfunc="sum").ffill()
    tot = w.sum(axis=1).reindex(mkt.index).ffill()
    p = np.sign(tot)
    p[p == 0] = np.nan
    return p.ffill()

lines = [f"焦煤·跟华泰 闸门验收(数据至 {mkt.index[-1].date()})", ""]
pos = pos_main()
base = pos.shift(2) * mkt["ret_open"]
c0, s0, m0 = perf(base)
flips = int((pos != pos.shift()).sum())
lines.append(f"主口径(T+1): 累计 {c0:+.1f}%  夏普 {s0:.2f}  回撤 {m0:+.1f}%  翻转 {flips} 次")

# 闸门1:安慰剂(循环移位 500 次)
rng = np.random.default_rng(11)
n = len(pos)
sh = []
arr = pos.values
for k in range(500):
    off = int(rng.integers(20, n - 20))
    p2 = pd.Series(np.roll(arr, off), index=pos.index)
    d2 = (p2.shift(2) * mkt["ret_open"]).dropna()
    sh.append(float(d2.mean() / d2.std() * np.sqrt(242)) if d2.std() > 0 else 0.0)
sh = np.array(sh)
p_val = float((sh >= s0).mean())
lines.append(f"闸门1 安慰剂: 置换夏普均值 {sh.mean():.2f}  p(>=实际 {s0:.2f}) = {p_val:.3f}  -> {'过' if p_val < 0.05 else '不过'}")

# 闸门2:滞后
for name, k in (("T+0(不可得,量化滞后成本)", 1), ("T+1(主口径)", 2), ("T+2(再晚一天)", 3)):
    c_, s_, m_ = perf(pos.shift(k) * mkt["ret_open"])
    lines.append(f"闸门2 {name}: 累计 {c_:+.1f}%  夏普 {s_:.2f}  回撤 {m_:+.1f}%")
bench = -mkt["ret_open"].fillna(0)
cb, sb, mb = perf(bench)
t2 = perf(pos.shift(3) * mkt["ret_open"])
lines.append(f"  基准恒定做空: 累计 {cb:+.1f}%  夏普 {sb:.2f}  | T+2 夏普 {t2[1]:.2f} > 基准 {sb:.2f} -> {'过' if t2[1] > sb and t2[0] > 0 else '不过'}")

# 闸门3:口径稳健
pv = pos_variety()
cv, sv, mv = perf(pv.shift(2) * mkt["ret_open"])
lines.append(f"闸门3a 品种合计口径: 累计 {cv:+.1f}%  夏普 {sv:.2f}  回撤 {mv:+.1f}%  -> {'同号' if cv > 0 else '翻号!'}")
pf = pos_main(fresh_days=3)
df_ = pf.shift(2) * mkt["ret_open"]
cf, sf, mf = perf(df_)
inmkt = float(pf.shift(2).notna().mean() * 100)
lines.append(f"闸门3b 新鲜度门(3日): 累计 {cf:+.1f}%  夏普 {sf:.2f}  回撤 {mf:+.1f}%  在场 {inmkt:.0f}%  -> {'同号' if cf > 0 else '翻号!'}")

# 闸门4:成本
turn = (pos.shift(2) != pos.shift(3)).astype(float)
net = base - turn * 0.0005 * 2
cn, sn, mn = perf(net)
lines.append(f"闸门4 扣双边成本(0.05%x2/翻转): 累计 {cn:+.1f}%  夏普 {sn:.2f}  -> {'过' if cn > 0 and sn > 0.7 else '塌'}")

# 闸门5:收益来源
runs = []
cur_sign, cur_ret, cur_len = None, 1.0, 0
held = pos.shift(2)
for d in mkt.index:
    s_ = held.get(d, np.nan)
    r_ = mkt["ret_open"].get(d, np.nan)
    if not np.isfinite(s_):
        continue
    if cur_sign is None or s_ != cur_sign:
        if cur_sign is not None:
            runs.append((cur_ret - 1) * 100)
        cur_sign, cur_ret, cur_len = s_, 1.0, 0
    if np.isfinite(r_):
        cur_ret *= (1 + s_ * r_)
        cur_len += 1
runs.append((cur_ret - 1) * 100)
rr = pd.Series(runs)
top5 = rr.nlargest(5).sum()
lines.append(f"闸门5 逐段: {len(rr)} 段  胜率 {(rr>0).mean()*100:.0f}%  中位 {rr.median():+.2f}%  前5段合计 {top5:+.1f}pp(全部段合计 {rr.sum():+.1f}pp,占 {top5/rr.sum()*100 if rr.sum() else 0:.0f}%)")
o2c = pos.shift(1) * mkt["o2c"]
ovn = base - o2c.reindex(base.index).fillna(0)
lines.append(f"  拆分: 日内(开→结算)累计 {perf(o2c)[0]:+.1f}%  隔夜与其余 {perf(base)[0]-perf(o2c)[0]:+.1f}pp(粗拆,仅示意)")

# 闸门6:逐年 + 2026 逐月
d_ = base.dropna()
lines.append("闸门6 逐年: " + "  ".join(f"{y}:{(np.prod(1+g)-1)*100:+.1f}%" for y, g in d_.groupby(d_.index.year)))
g26 = d_[d_.index.year == 2026]
lines.append("  2026 逐月: " + "  ".join(f"{m}:{(np.prod(1+g)-1)*100:+.1f}%" for m, g in g26.groupby(g26.index.month)))

# 闸门7:对照其余四家(选择偏差量级)
others = []
for m in ("东证期货", "国泰君安", "永安期货", "东吴期货"):
    sub2 = seat[seat["member_key"] == m]
    sig2 = pd.Series(np.nan, index=mkt.index)
    for c in dict.fromkeys(mkt["main"]):
        if not isinstance(c, str):
            continue
        rows = sub2[sub2["contract"] == c]
        if rows.empty:
            continue
        w2 = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0].ffill()
        days = mkt.index[mkt["main"] == c]
        sig2.loc[days] = w2.reindex(days).values
    p2 = np.sign(sig2).replace(0, np.nan).ffill()
    others.append((m, perf(p2.shift(2) * mkt["ret_open"])[1]))
lines.append("闸门7 其余四家夏普(选择偏差参照): " + "  ".join(f"{m}:{s_:.2f}" for m, s_ in others))
io.open(OUT / "jm_huatai_gates.txt", "w", encoding="utf-8").write("\n".join(lines))
print("\n".join(lines))
