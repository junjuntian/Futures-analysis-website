"""鸡蛋战役制第二引擎验收(PLAN_JD_SECOND_v1)。跑法:python research/run_jd_second.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
import campaign as C

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / "jd_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "jd_seat.csv.gz"))
v = H.use("JD")
_rs = pd.Timestamp(H.RULES["replay_start"])
price = price[price["trade_date"] >= _rs]; seat = seat[seat["trade_date"] >= _rs]
_ok = price.dropna(subset=["open_interest"])["trade_date"].unique()
price = price[price["trade_date"].isin(_ok)]; seat = seat[seat["trade_date"].isin(_ok)]
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= _rs]
groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
sig = H.signal_series(seat, groups)
sig = H.attach_cost_signal(sig, seat, mkt, groups)
rdf, rhave = H.retail_series(seat, mkt.index)
_, _, daily_cur = H.replay(sig, mkt, rdf, op, st)
K = 0.37
H.RULES["campaign"] = {"add_min": 1000.0*K, "confirm": 5000.0*K, "gap": 3, "tail": 10,
                       "unload": 0.30, "share": 0.25, "max_units": 3}
H.RULES["strategy"] = "campaign"
out = C.run(seat, mkt, op, st, list(groups.dropna().iloc[-1]), H.RULES)

b = pd.concat([pd.Series(daily_cur), pd.Series(out["daily"])], axis=1).fillna(0.0)
b.columns = ["cur", "cmp"]

def sharpe(d):
    d = pd.Series(d).dropna()
    return float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan

def perf(d):
    d = pd.Series(d).dropna()
    eq = (1 + d).cumprod()
    return (float(eq.iloc[-1]) - 1) * 100, sharpe(d), float((eq / eq.cummax() - 1).min()) * 100

L = [f"鸡蛋战役制第二引擎验收(样本 {b.index[0].date()} ~ {b.index[-1].date()},n={len(b)})", ""]
c_cur, s_cur, m_cur = perf(b.cur)
L.append(f"单跑现行: {c_cur:+.1f}% / {s_cur:.2f} / {m_cur:+.1f}%   单跑战役制: {perf(b.cmp)[0]:+.1f}% / {perf(b.cmp)[1]:.2f} / {perf(b.cmp)[2]:+.1f}%")

# 闸门1 权重面
L.append("闸门1 权重面(战役制权重 25/50/75%):")
ws_ok = []
for w in (0.25, 0.50, 0.75):
    combo = (1 - w) * b.cur + w * b.cmp
    c_, s_, m_ = perf(combo)
    ok = s_ >= s_cur
    ws_ok.append(ok)
    L.append(f"  w={w:.0%}: {c_:+.1f}% / {s_:.2f} / {m_:+.1f}%  -> {'不劣' if ok else '劣于单跑'}")
cont = ws_ok in ([True,True,True],[True,True,False],[False,True,True],[True,False,False],[False,False,True])
g1 = (sum(ws_ok) >= 2 and (ws_ok[0] and ws_ok[1] or ws_ok[1] and ws_ok[2])) or all(ws_ok)
L.append(f"  连续不劣区间: {'有' if g1 else '无'} -> {'过' if g1 else '不过'}")

# 闸门2 block bootstrap(块20日,成对同块重抽)
rng = np.random.default_rng(53)
n, blk = len(b), 20
starts_all = np.arange(0, n - blk)
wins = 0
combo50 = 0.5 * b.cur + 0.5 * b.cmp
for k in range(2000):
    ix = np.concatenate([np.arange(s0, s0 + blk) for s0 in rng.choice(starts_all, size=n // blk + 1)])[:n]
    if sharpe(combo50.values[ix]) > sharpe(b.cur.values[ix]):
        wins += 1
p_boot = wins / 2000
grade = "铁闸过" if p_boot >= 0.95 else ("知情上一档" if p_boot >= 0.80 else "不立")
L.append(f"闸门2 bootstrap: P(组合夏普>现行) = {p_boot:.1%} -> {grade}")

# 闸门3 逐年
L.append("闸门3 逐年(50/50 vs 单跑现行):")
wins_y = 0; tot_y = 0
for y, g in combo50.groupby(combo50.index.year):
    cy = (np.prod(1 + g) - 1) * 100
    ref = (np.prod(1 + b.cur[b.cur.index.year == y]) - 1) * 100
    tot_y += 1; wins_y += cy >= ref - 0.5
    L.append(f"  {y}: 组合 {cy:+.1f}% vs 现行 {ref:+.1f}%  {'≥' if cy >= ref - 0.5 else '<'}")
g3 = wins_y >= 3
L.append(f"  不劣年 {wins_y}/{tot_y} -> {'过' if g3 else '不过'}")

# 闸门4 回撤
c50, s50, m50 = perf(combo50)
g4 = m50 >= m_cur
L.append(f"闸门4 回撤: 组合 {m50:+.1f}% vs 现行 {m_cur:+.1f}% -> {'过' if g4 else '不过'}")

# 闸门5 滚动相关
active = b[(b.cur != 0) | (b.cmp != 0)]
rc = active["cur"].rolling(60).corr(active["cmp"]).dropna()
L.append(f"闸门5 滚动60日相关: 全样本 {float(active.corr().iloc[0,1]):+.2f}  中位 {float(rc.median()):+.2f}  最大 {float(rc.max()):+.2f}"
         + ("(某段>0.6,分散叙事打折)" if float(rc.max()) > 0.6 else ""))

# 附:贡献时序
cmp_gain = b.cmp[b.cmp != 0]
cur_state = b.cur.reindex(cmp_gain.index)
idle = cmp_gain[cur_state == 0]
L.append("")
L.append(f"附 贡献时序: 战役制有仓 {len(cmp_gain)} 日,其中现行空仓的 {len(idle)} 日({len(idle)/max(len(cmp_gain),1)*100:.0f}%),"
         f"这些日子战役制合计 {(np.prod(1+idle)-1)*100:+.1f}pp(它补的是现行的空窗吗)")

txt = "\n".join(L)
io.open(OUT / "jd_second.txt", "w", encoding="utf-8").write(txt)
