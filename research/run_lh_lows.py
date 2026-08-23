"""生猪做多进场:阶段性低点那天(及前后)机构/散户/成本/价差都是什么状态?(DEC-122 后续立项)

① 阶段性低点 = 主力连续收益序列(逐合约 ret 连乘,不跨合约相除)上,前后各 10 日的最低,
   且之后 20 日内反弹 ≥4%。
② 每个低点列出:低点当天与 −5/+5 日的 机构方向/本轮卸仓/5 日流向 z/刚翻多、散户 z、
   价 vs 机构成本、过去 20 日涨跌、20 日区间位置、主力距窗口止点天数。
③ 对照:全部交易日上同一批指标的分布,看哪些指标在低点附近显著偏离。
④ 合约选择:低点日若主力剩 ≤22 个交易日(约一个月),换次主力 X+2;比较两者后 20 日收益。
跑法:仓库根目录 python research/run_lh_lows.py
"""
import sys, pathlib, numpy as np, pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
D = pathlib.Path(__file__).resolve().parent / "data"
price = H.clean_price(pd.read_csv(D / "lh_price.csv.gz")); seat = H.clean_seat(pd.read_csv(D / "lh_seat.csv.gz"))
H.use("LH"); mkt = H.main_series(price); mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
groups, _, _ = H.fixed_groups(H.RULES["fixed_members"], seat, price, mkt.index, "2026-08-23")
sig = H.signal_series(seat, groups); rdf, _ = H.retail_series(seat, mkt.index)
unl = H.unload_series(sig, seat, groups)["pct"].reindex(mkt.index)
cc = H.inst_cost_series(sig, mkt, groups).reindex(mkt.index)
idx = mkt.index
# 连续指数(逐合约 ret 连乘)
cont = (1 + mkt["ret"].fillna(0)).cumprod()
z = sig["z"].reindex(idx); net = sig["net"].reindex(idx); rz = rdf["rz"].reindex(idx)
side = cc["side"]; cost = cc["cost"]
px_vs_cost = (mkt["close"] / cost - 1) * 100
win20 = cont.rolling(20)
pos20 = (cont - win20.min()) / (win20.max() - win20.min()) * 100
past20 = mkt["past"] * 100
# 机构刚翻多 / 卸仓 5 日变化 / 净持仓 5 日变化方向
dunl5 = unl - unl.shift(5)
flip = (side > 0) & (side.shift(1) <= 0)
# 低点
W, BOUNCE = 10, 0.04
lows = []
for i in range(W, len(idx) - 20):
    seg = cont.iloc[i - W:i + W + 1]
    if cont.iloc[i] == seg.min() and cont.iloc[i + 1:i + 21].max() / cont.iloc[i] - 1 >= BOUNCE:
        lows.append(i)
# 去重:相邻 10 日内只留一个
keep = []
for i in lows:
    if not keep or i - keep[-1] > W: keep.append(i)
lows = keep
print(f"样本 {idx[0].date()}~{idx[-1].date()},阶段性低点(±{W} 日最低且 20 日内反弹≥{BOUNCE*100:.0f}%) {len(lows)} 个\n")
def val(s, i, k=0):
    j = i + k
    return s.iloc[j] if 0 <= j < len(s) else np.nan
rows = []
hdr = f"{'低点日':<11}{'主力':<8}{'20日反弹%':>8}{'机构':>5}{'卸仓%':>6}{'卸仓5日Δ':>8}{'z':>6}{'z前5日':>7}{'散户z':>6}{'价-成本%':>8}{'过去20日%':>8}{'区间位%':>7}{'剩天':>5}"
print(hdr)
for i in lows:
    d = idx[i]; b = (cont.iloc[i + 1:i + 21].max() / cont.iloc[i] - 1) * 100
    r = dict(d=d, main=mkt["main"].iloc[i], bounce=b, side=val(side, i), unl=val(unl, i), dunl=val(dunl5, i), z=val(z, i), z5=val(z, i, -5),
             rz=val(rz, i), pvc=val(px_vs_cost, i), past=val(past20, i), pos=val(pos20, i), dleft=mkt["dleft"].iloc[i],
             zmax5=z.iloc[max(0, i - 5):i + 6].max(), flip10=bool(flip.iloc[i:i + 11].any()), unl_max10=unl.iloc[i:i + 11].max())
    rows.append(r)
    print(f"{d.date()}  {r['main']:<8}{b:>8.1f}{('净空' if r['side']<0 else '净多' if r['side']>0 else '—'):>5}"
          f"{r['unl']*100 if np.isfinite(r['unl']) else np.nan:>6.0f}{r['dunl']*100 if np.isfinite(r['dunl']) else np.nan:>+8.0f}"
          f"{r['z']:>+6.2f}{r['z5']:>+7.2f}{r['rz']:>+6.2f}{r['pvc']:>+8.1f}{r['past']:>+8.1f}{r['pos']:>7.0f}{r['dleft']:>5d}")
df = pd.DataFrame(rows)
print("\n=== 低点日 vs 全部交易日(均值 / 占比)===")
allv = pd.DataFrame(dict(side=side, unl=unl, dunl=dunl5, z=z, rz=rz, pvc=px_vs_cost, past=past20, pos=pos20)).dropna(subset=["z"])
def pct(s, f): return f"{(f(s)).mean()*100:.0f}%"
print(f"{'指标':<22}{'低点日':>12}{'全部日':>12}")
print(f"{'机构净空占比':<22}{pct(df.side, lambda s: s<0):>12}{pct(allv.side, lambda s: s<0):>12}")
print(f"{'本轮卸仓% 均值':<22}{df.unl.mean()*100:>11.0f}%{allv.unl.mean()*100:>11.0f}%")
print(f"{'卸仓≥50% 占比':<22}{pct(df.unl, lambda s: s>=0.5):>12}{pct(allv.unl, lambda s: s>=0.5):>12}")
print(f"{'卸仓 5 日Δ 均值(pp)':<22}{df.dunl.mean()*100:>+12.1f}{allv.dunl.mean()*100:>+12.1f}")
print(f"{'z 均值(当天)':<22}{df.z.mean():>+12.2f}{allv.z.mean():>+12.2f}")
print(f"{'z 前5日 均值':<22}{df.z5.mean():>+12.2f}{allv.z.shift(5).mean():>+12.2f}")
print(f"{'z>0 占比':<22}{pct(df.z, lambda s: s>0):>12}{pct(allv.z, lambda s: s>0):>12}")
print(f"{'±5 日内 z 最大 均值':<22}{df.zmax5.mean():>+12.2f}{z.rolling(11, center=True).max().mean():>+12.2f}")
print(f"{'散户 z 均值':<22}{df.rz.mean():>+12.2f}{allv.rz.mean():>+12.2f}")
print(f"{'散户 z>1 占比':<22}{pct(df.rz, lambda s: s>1):>12}{pct(allv.rz, lambda s: s>1):>12}")
print(f"{'价<机构成本 占比':<22}{pct(df.pvc, lambda s: s<0):>12}{pct(allv.pvc, lambda s: s<0):>12}")
print(f"{'价-成本% 均值':<22}{df.pvc.mean():>+12.1f}{allv.pvc.mean():>+12.1f}")
print(f"{'过去20日% 均值':<22}{df.past.mean():>+12.1f}{allv.past.mean():>+12.1f}")
print(f"{'过去20日<-5% 占比':<22}{pct(df.past, lambda s: s<-5):>12}{pct(allv.past, lambda s: s<-5):>12}")
print(f"{'20日区间位 均值':<22}{df.pos.mean():>12.0f}{allv.pos.mean():>12.0f}")
print(f"{'低点后10日内机构翻多':<22}{df.flip10.mean()*100:>11.0f}%{'':>12}")
print(f"{'低点后10日内卸仓最大≥50%':<22}{(df.unl_max10>=0.5).mean()*100:>11.0f}%{'':>12}")

print("\n=== ④ 合约选择:低点日买主力 vs 买次主力(X+2);主力剩 ≤22 日则按规则应换次主力 ===")
stx = price.pivot_table(index="trade_date", columns="contract", values="settle", aggfunc="first")
def nxt(c):  # X+2 月
    y, m = int(c[2:4]), int(c[4:6]); m += 2
    if m > 12: m -= 12; y += 1
    return f"LH{y:02d}{m:02d}"
print(f"{'低点日':<11}{'主力':<8}{'剩天':>5}{'主力20日%':>9}{'次主力':<8}{'次主力20日%':>10}{'规则选':<6}{'规则20日%':>9}")
acc = []
for r in rows:
    d = r["d"]; m = r["main"]; n = nxt(m)
    if d not in stx.index: continue
    j = stx.index.get_loc(d); end = min(j + 20, len(stx) - 1)
    def r20(c):
        s = stx[c].iloc[j:end + 1].dropna() if c in stx.columns else pd.Series(dtype=float)
        return (s.iloc[-1] / s.iloc[0] - 1) * 100 if len(s) > 5 else np.nan
    rm, rn = r20(m), r20(n)
    pick = n if r["dleft"] <= 22 else m
    rp = rn if pick == n else rm
    acc.append(dict(rm=rm, rn=rn, rp=rp, sw=pick == n))
    print(f"{d.date()}  {m:<8}{r['dleft']:>5d}{rm:>+9.1f}  {n:<8}{rn:>+10.1f}  {pick:<8}{rp:>+9.1f}")
a = pd.DataFrame(acc)
print(f"\n均值:主力 {a.rm.mean():+.1f}%  次主力 {a.rn.mean():+.1f}%  规则(剩≤22日换次主力,{int(a.sw.sum())}/{len(a)} 次换) {a.rp.mean():+.1f}%")

print("\n=== ⑤ 反过来验:按低点共性拟一条进场规则,全样本每天判,看命中后 5/10/20 日收益(主力连续指数)===")
fwd = {k: (cont.shift(-k) / cont - 1) * 100 for k in (5, 10, 20)}
base = pd.DataFrame(fwd)
def evaluate(name, mask):
    m = mask.fillna(False)
    # 同一波只算首次:前 10 日内已触发过的不重复计
    first = m & ~m.shift(1, fill_value=False).rolling(10, min_periods=1).max().astype(bool)
    e = base[first]
    if len(e) == 0: print(f"  {name:<46} 0 次"); return
    yrs = e.groupby(e.index.year)["20"] if "20" in e else None
    by = " ".join(f"{y}:{g.mean():+.1f}%({len(g)})" for y, g in base[first][20].groupby(base[first].index.year))
    print(f"  {name:<46}{len(e):>3}次  5日{e[5].mean():+.1f}%  10日{e[10].mean():+.1f}%  20日{e[20].mean():+.1f}% 胜{(e[20]>0).mean()*100:.0f}%  | {by}")
print(f"  {'全部交易日(基准)':<46}{len(base.dropna()):>3}日  5日{base[5].mean():+.1f}%  10日{base[10].mean():+.1f}%  20日{base[20].mean():+.1f}% 胜{(base[20]>0).mean()*100:.0f}%")
ns = side < 0
evaluate("机构净空 & 卸仓≥50%(现行)", ns & (unl >= 0.5))
evaluate("价<机构成本 3%", ns & (px_vs_cost <= -3))
evaluate("价<机构成本 3% & 过去20日≤-5%", ns & (px_vs_cost <= -3) & (past20 <= -5))
evaluate("价<机构成本 3% & 区间位≤20%", ns & (px_vs_cost <= -3) & (pos20 <= 20))
evaluate("价<成本 3% & 区间位≤20% & 过去20日≤-5%", ns & (px_vs_cost <= -3) & (pos20 <= 20) & (past20 <= -5))
evaluate("过去20日≤-5% & 区间位≤20%(不看机构)", (past20 <= -5) & (pos20 <= 20))
evaluate("价<成本 5%", ns & (px_vs_cost <= -5))
evaluate("价<成本 5% & 区间位≤20%", ns & (px_vs_cost <= -5) & (pos20 <= 20))
evaluate("价<成本 3% & 区间位≤20% & 当日收阳(ret>0)", ns & (px_vs_cost <= -3) & (pos20 <= 20) & (mkt['ret'] > 0))
evaluate("价<成本 3% & 区间位≤20% & z 由负转正", ns & (px_vs_cost <= -3) & (pos20 <= 20) & (z > 0) & (z.shift(1) <= 0))

print("\n=== ⑥ 低点只能靠价格自己拐头确认?—— 「超跌状态 + 价格拐头」两段式 ===")
low20 = cont.rolling(20).min()
off_low = (cont / low20 - 1) * 100                  # 距 20 日最低反弹了多少
cond_recent = (ns & (px_vs_cost <= -3) & (pos20 <= 40)).rolling(10, min_periods=1).max().astype(bool)  # 10 日内出现过超跌状态
hi5 = cont.rolling(5).max().shift(1)
evaluate("10日内超跌 & 今日收盘创 5 日新高", cond_recent & (cont > hi5))
for k in (2, 3, 4):
    evaluate(f"10日内超跌 & 距20日最低已反弹≥{k}%(首日)", cond_recent & (off_low >= k) & (off_low.shift(1) < k))
for k in (2, 3, 4):
    evaluate(f"不看机构:距20日最低反弹≥{k}%(首日) & 过去20日≤-4%", (off_low >= k) & (off_low.shift(1) < k) & (past20 <= -4))
evaluate("不看机构:距20日最低反弹≥3%(首日)", (off_low >= 3) & (off_low.shift(1) < 3))

print("\n=== ⑦ 两个最大反弹(2024-02-21 +8.4%、2026-04-13 +12.3%)低点日都是 z>1 & 散户z>1 的共振 —— 共振做多在全样本上如何 ===")
evaluate("共振:z≥1 & 散户z≥1(首日)", (z >= 1) & (rz >= 1))
evaluate("共振 & 区间位≤30%", (z >= 1) & (rz >= 1) & (pos20 <= 30))
evaluate("共振 & 价<机构成本", (z >= 1) & (rz >= 1) & (px_vs_cost < 0))
evaluate("共振 & 过去20日≤-5%", (z >= 1) & (rz >= 1) & (past20 <= -5))
evaluate("仅 z≥1(首日)", (z >= 1))
evaluate("z≥1 & 区间位≤30%", (z >= 1) & (pos20 <= 30))
