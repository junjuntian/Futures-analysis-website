"""生猪只看 2026:低点/逐周快照/候选规则(DEC-123)。仓库根目录跑:python research/run_lh_2026_lows.py"""
import sys, runpy, io, contextlib
# 复用 run_lh_lows 的全部中间量(把它的打印吞掉),然后只看 2026
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    g = runpy.run_path("research/run_lh_lows.py")
import numpy as np, pandas as pd
cont, idx, mkt, side, unl, dunl5, z, rz, px_vs_cost, past20, pos20, base, evaluate = (g[k] for k in
    ("cont","idx","mkt","side","unl","dunl5","z","rz","px_vs_cost","past20","pos20","base","evaluate"))
Y = idx >= pd.Timestamp("2026-01-01")
b26 = base[Y]
print(f"=== 2026 年基准(1/1~8/18,{int(Y.sum())} 日):5日{b26[5].mean():+.1f}% 10日{b26[10].mean():+.1f}% 20日{b26[20].mean():+.1f}% 胜{(b26[20]>0).mean()*100:.0f}%  主力连续指数年内 {(cont[Y].iloc[-1]/cont[Y].iloc[0]-1)*100:+.1f}%")
# 2026 低点:放宽到 ±7 日最低、反弹≥3%
W, B = 7, 0.03
lows = []
for i in range(W, len(idx) - 5):
    if not Y[i]: continue
    seg = cont.iloc[i-W:i+W+1]
    fwd = cont.iloc[i+1:i+21]
    if cont.iloc[i] == seg.min() and len(fwd) and fwd.max()/cont.iloc[i]-1 >= B:
        if not lows or i - lows[-1] > W: lows.append(i)
print(f"\n=== 2026 阶段性低点(±{W} 日最低、20 日内反弹≥{B*100:.0f}%){len(lows)} 个 ===")
print(f"{'低点日':<11}{'主力':<8}{'反弹%':>6}{'机构':>5}{'卸仓%':>6}{'卸5Δ':>6}{'z':>6}{'z-5':>6}{'z+5max':>7}{'散z':>6}{'价-成本':>8}{'过去20':>7}{'区间位':>6}{'剩天':>5}")
for i in lows:
    d = idx[i]; bb = (cont.iloc[i+1:i+21].max()/cont.iloc[i]-1)*100
    print(f"{d.date()}  {mkt['main'].iloc[i]:<8}{bb:>6.1f}{('净空' if side.iloc[i]<0 else '净多' if side.iloc[i]>0 else '—'):>5}"
          f"{unl.iloc[i]*100:>6.0f}{dunl5.iloc[i]*100:>+6.0f}{z.iloc[i]:>+6.2f}{z.iloc[i-5]:>+6.2f}{z.iloc[i:i+6].max():>+7.2f}{rz.iloc[i]:>+6.2f}"
          f"{px_vs_cost.iloc[i]:>+8.1f}{past20.iloc[i]:>+7.1f}{pos20.iloc[i]:>6.0f}{mkt['dleft'].iloc[i]:>5d}")
# 2026 逐周快照:主力价、机构净持仓/卸仓/成本、z、散户z —— 看磨底结构
print("\n=== 2026 逐周(每周五)快照 ===")
net = g["net"]; cost = g["cost"]
wk = [d for d in idx[Y] if d.weekday() == 4]
print(f"{'日期':<11}{'主力':<8}{'收盘':>7}{'机构净':>8}{'卸仓%':>6}{'成本':>7}{'价-成本':>8}{'z':>6}{'散z':>6}{'过去20':>7}")
for d in wk:
    i = idx.get_loc(d)
    print(f"{d.date()}  {mkt['main'].iloc[i]:<8}{mkt['close'].iloc[i]:>7.0f}{net.iloc[i]:>8.0f}{unl.iloc[i]*100:>6.0f}{cost.iloc[i]:>7.0f}{px_vs_cost.iloc[i]:>+8.1f}{z.iloc[i]:>+6.2f}{rz.iloc[i]:>+6.2f}{past20.iloc[i]:>+7.1f}")

print("\n=== 候选规则只在 2026 评(同一波首次)===")
def ev26(name, mask):
    m = mask.fillna(False) & pd.Series(Y, index=idx)
    first = m & ~m.shift(1, fill_value=False).rolling(10, min_periods=1).max().astype(bool)
    e = base[first].dropna(subset=[5])
    if len(e)==0: print(f"  {name:<44} 0 次"); return
    ds = " ".join(f"{d.strftime('%m-%d')}({v:+.1f})" for d, v in e[20].items())
    print(f"  {name:<44}{len(e):>2}次 5日{e[5].mean():+.1f}% 10日{e[10].mean():+.1f}% 20日{e[20].mean():+.1f}% 胜{(e[20]>0).mean()*100:.0f}% | {ds}")
ns = side < 0
low20 = cont.rolling(20).min(); off_low = (cont/low20-1)*100
ev26("现行:净空 & 卸仓≥50%", ns & (unl>=0.5))
ev26("净空 & 卸仓≥30%", ns & (unl>=0.3))
ev26("价<机构成本 5%", ns & (px_vs_cost<=-5))
ev26("价<机构成本 10%", ns & (px_vs_cost<=-10))
ev26("价<机构成本 10% & 距20日低反弹≥2%", ns & (px_vs_cost<=-10) & (off_low>=2) & (off_low.shift(1)<2))
ev26("过去20日≤-5% & 距20日低反弹≥2%(首日)", (past20<=-5) & (off_low>=2) & (off_low.shift(1)<2))
ev26("过去20日≤-5% & 反弹≥3%(首日)", (past20<=-5) & (off_low>=3) & (off_low.shift(1)<3))
ev26("距20日低反弹≥3%(首日,不看别的)", (off_low>=3) & (off_low.shift(1)<3))
ev26("z≥1(首日)", z>=1)
ev26("z≥1 & 区间位≤30%", (z>=1) & (pos20<=30))
ev26("共振 z≥1 & 散z≥1", (z>=1) & (rz>=1))
ev26("共振 & 价<成本", (z>=1) & (rz>=1) & (px_vs_cost<0))
ev26("散户z≥1 & 区间位≤30%", (rz>=1) & (pos20<=30))
ev26("机构翻多(side 由≤0转>0)", (side>0) & (side.shift(1)<=0))
ev26("机构净空减仓:卸仓5日Δ≥+10pp & 区间位≤30%", ns & (dunl5>=0.10) & (pos20<=30))
