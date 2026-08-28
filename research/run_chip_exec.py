"""筹码带当「执行工具」:方向仍跟第二引擎(永安),但只在有利带建仓、对侧带止盈。
对照基准 = DEC-141 纯跟随(天天满仓跟方向)。跑法:python research/run_chip_exec.py FG"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "FG"
MEMBER = {"FG": "永安期货", "SA": "永安期货"}[code]
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
idx = mkt.index
piv = lambda col, how: price.pivot_table(index="trade_date", columns="contract", values=col, aggfunc=how).sort_index()
hi, lo, cl = piv("high_price", "max"), piv("low_price", "min"), piv("close_price", "first")

# 方向信号:该席位在当日主力的可见净持仓方向(DEC-141 口径)
sub = seat[seat["member_key"] == MEMBER]
sig = pd.Series(np.nan, index=idx)
for c in dict.fromkeys(mkt["main"]):
    if not isinstance(c, str):
        continue
    rows = sub[sub["contract"] == c]
    if rows.empty:
        continue
    w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
    days = idx[mkt["main"] == c]
    sig.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
dir_ = np.sign(sig).replace(0, np.nan).ffill()

bands = pd.DataFrame(index=idx, columns=["lo_hi", "hi_lo", "close"], dtype=float)
for i, d in enumerate(idx):
    c = mkt["main"].iloc[i]
    if not isinstance(c, str) or c not in hi.columns:
        continue
    z = H.zone_band(hi[c].loc[:d], lo[c].loc[:d], cl[c].asof(d) if c in cl.columns else np.nan)
    if z:
        bands.iloc[i] = [z["low_band"][1], z["high_band"][0], z["last"]]

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

def run(pos):
    held = pos.shift(2)
    turn = (pos.shift(2) != pos.shift(3)).astype(float)
    net = (held * ret - turn * COST * 2).dropna()
    cum, sh, mdd = perf(net)
    return cum, sh, mdd, float((held != 0).mean() * 100), int((pos != pos.shift()).sum()), net

# 基准:纯跟随
base_pos = dir_.fillna(0)
# 筹码执行版:方向跟信号,但只在有利带建仓;到对侧带止盈平仓;信号翻向立即换边
exec_pos = pd.Series(0.0, index=idx)
cur = 0.0
for i in range(len(idx)):
    d_, c_ = dir_.iloc[i], bands["close"].iloc[i]
    lo_hi, hi_lo = bands["lo_hi"].iloc[i], bands["hi_lo"].iloc[i]
    if not np.isfinite(d_):
        exec_pos.iloc[i] = cur
        continue
    if cur != 0 and np.sign(cur) != d_:
        cur = 0.0                                   # 信号翻向:先清,等有利带再进
    if np.isfinite(c_) and np.isfinite(lo_hi) and np.isfinite(hi_lo):
        if cur == 0:
            if d_ > 0 and c_ <= lo_hi:
                cur = 1.0                           # 看多且价在低带 -> 进
            elif d_ < 0 and c_ >= hi_lo:
                cur = -1.0                          # 看空且价在高带 -> 进
        else:
            if cur > 0 and c_ >= hi_lo:
                cur = 0.0                           # 多单到高带止盈
            elif cur < 0 and c_ <= lo_hi:
                cur = 0.0                           # 空单到低带止盈
    exec_pos.iloc[i] = cur

L = [f"{v['name']} 筹码带当执行工具(方向=跟{MEMBER},样本 {idx[0].date()}~{idx[-1].date()})", ""]
for name, pos in (("基准 DEC-141 纯跟随", base_pos), ("筹码执行版(有利带建仓/对侧带止盈)", exec_pos)):
    cum, sh, mdd, inmkt, flips, net = run(pos)
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in net.groupby(net.index.year)}
    L.append(f"{name}: 扣成本 {cum:+.1f}%  夏普 {sh:.2f}  回撤 {mdd:+.1f}%  在场 {inmkt:.0f}%  换手 {flips}")
    L.append("  逐年: " + "  ".join(f"{y}:{vv:+.0f}%" for y, vv in sorted(ys.items())))
    L.append("")
txt = "\n".join(L)
io.open(OUT / f"chip_exec_{code.lower()}.txt", "w", encoding="utf-8").write(txt)
