"""筹码带区间回测(运营者洞察:震荡市靠换筹码)。跑法:python research/run_chip_band_bt.py FG"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "FG"
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
idx = mkt.index
piv = lambda col, how: price.pivot_table(index="trade_date", columns="contract", values=col, aggfunc=how).sort_index()
hi, lo, cl = piv("high_price", "max"), piv("low_price", "min"), piv("close_price", "first")

# 逐日:主力合约自己的 5 日带(与线上 zone_band 同口径:低带第3、4低 / 高带最高两天)
lo_hi_band = pd.DataFrame(index=idx, columns=["lo_hi", "hi_lo", "close"], dtype=float)
for i, d in enumerate(idx):
    c = mkt["main"].iloc[i]
    if not isinstance(c, str) or c not in hi.columns:
        continue
    z = H.zone_band(hi[c].loc[:d], lo[c].loc[:d], cl[c].asof(d) if c in cl.columns else np.nan)
    if not z:
        continue
    lo_hi_band.iloc[i] = [z["low_band"][1], z["high_band"][0], z["last"]]

ret = mkt["ret_open"].fillna(0)
COST = 0.0005

def perf(d):
    d = pd.Series(d).dropna()
    if not len(d):
        return np.nan, np.nan, np.nan
    eq = (1 + d).cumprod()
    return ((float(eq.iloc[-1]) - 1) * 100,
            float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan,
            float((eq / eq.cummax() - 1).min()) * 100)

L = [f"{v['name']} 筹码带区间回测(样本 {idx[0].date()} ~ {idx[-1].date()};带=线上 zone_band 口径)", ""]
rng = np.random.default_rng(71)
for name, hold_mid in (("F1 进带反转·带外空仓", False), ("F2 进带反转·中间持有", True)):
    pos = pd.Series(0.0, index=idx)
    cur = 0.0
    for i in range(len(idx)):
        c_, lo_hi, hi_lo = lo_hi_band["close"].iloc[i], lo_hi_band["lo_hi"].iloc[i], lo_hi_band["hi_lo"].iloc[i]
        if not (np.isfinite(c_) and np.isfinite(lo_hi) and np.isfinite(hi_lo)):
            pos.iloc[i] = cur if hold_mid else 0.0
            continue
        if c_ <= lo_hi:
            cur = 1.0          # 收在低带里 -> 多
        elif c_ >= hi_lo:
            cur = -1.0         # 收在高带里 -> 空
        elif not hold_mid:
            cur = 0.0          # 带外空仓
        pos.iloc[i] = cur
    held = pos.shift(2)
    turn = (pos.shift(2) != pos.shift(3)).astype(float)
    base = (held * ret).dropna()
    net = (held * ret - turn * COST * 2).dropna()
    cum, sh, mdd = perf(base)
    cum_n, sh_n, mdd_n = perf(net)
    inmkt = float((held != 0).mean() * 100)
    flips = int((pos != pos.shift()).sum())
    arr, n_ = pos.values, len(pos)
    sh_l = []
    for k in range(500):
        off = int(rng.integers(20, n_ - 20))
        d2 = (pd.Series(np.roll(arr, off), index=idx).shift(2) * ret).dropna()
        sh_l.append(float(d2.mean() / d2.std() * np.sqrt(242)) if d2.std() > 0 else 0.0)
    p_pl = float((np.array(sh_l) >= sh).mean()) if np.isfinite(sh) else 1.0
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in net.groupby(net.index.year)}
    L.append(f"{name}: 在场 {inmkt:.0f}%  毛 {cum:+.1f}%/{sh:.2f}  **扣成本 {cum_n:+.1f}%/{sh_n:.2f}/回撤{mdd_n:+.1f}%**  换手 {flips}")
    L.append(f"  安慰剂 p={p_pl:.3f}  逐年(扣后): " + "  ".join(f"{y}:{vv:+.0f}%" for y, vv in sorted(ys.items())))
    L.append("")
# 基准
cum_b, sh_b, mdd_b = perf(ret)
L.append(f"基准 恒定满仓做多: {cum_b:+.1f}%/{sh_b:.2f}/{mdd_b:+.1f}%   恒定做空: {perf(-ret)[0]:+.1f}%/{perf(-ret)[1]:.2f}")
txt = "\n".join(L)
io.open(OUT / f"chip_band_{code.lower()}.txt", "w", encoding="utf-8").write(txt)
