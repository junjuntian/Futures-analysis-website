"""玻璃:合计流向/单家事件/分市况/SA过滤都不行之后,还剩三条没试的路。

运营者问「玻璃真的没办法做跟随聪明钱的策略吗」。在回答"不行"之前,把没排除的
可能性排完:

  A 对手盘信号——垫底席位(徽商 -21.5 亿、东方财富 -15.9 亿)持续亏,
    反着跟他们做会不会反而稳?散户的错法可能比机构的对法更一致。
  B 稳定席位组——之前一直用 alpha 前 5;换成「在所有逐年留一口径里都靠前」
    的那几家,把只在某一年冒头的排除掉。
  C 20 日窗——窗口扫描里它全样本 t 最高(+4.94),但当时没看逐年符号。
"""
import numpy as np, pandas as pd
import lhlib as L
from run_flow_skill import build, seat_alpha, power

CODE="FG"
df, main = build(CODE)
full = seat_alpha(df, CODE).sort_values("alpha", ascending=False)

def yearly(sig, label):
    j = pd.concat([sig.rename("sig"), main], axis=1, sort=True); j["y"]=j.index.year
    out=[]
    for y,g in j.groupby("y"):
        r = power(g["sig"], g)
        if r: out.append((y, r[0], r[1]))
    pos=sum(1 for _,c,_ in out if c>0)
    allr = power(sig, main)
    print(f"  {label}")
    print(f"    逐年:" + " ".join(f"{y}{'+' if c>0 else '-'}" for y,c,_ in out)
          + f"   → {pos} 正 / {len(out)-pos} 负")
    if allr: print(f"    全样本 偏相关 {allr[0]:+.3f}  t={allr[1]:+.2f}")
    return pos, len(out)

def flow(members, win=5):
    return df[df["member_key"].isin(members)].groupby("trade_date")["net"].sum().sort_index().diff(win)

print("A 对手盘:反向跟随最亏的那几家")
for k in (3, 5, 8):
    losers = full.tail(k).index.tolist()
    # 反向:他们加多我就看空,所以信号取负
    yearly(-flow(losers), f"垫底 {k} 家取反 ({'、'.join(losers[:3])}…)")

print("\nB 稳定席位组:在逐年留一里都靠前的那几家")
years = sorted(df["trade_date"].dt.year.unique())
ranks = {}
for y in years:
    a = seat_alpha(df[df["trade_date"].dt.year != y], CODE)
    if not a.empty:
        ranks[y] = a["alpha"].rank(ascending=False)
rk = pd.DataFrame(ranks).dropna()
stable = rk.max(axis=1).sort_values()          # 最差名次都还靠前 = 稳
print(f"  各留一口径下「最差名次」最靠前的 8 家:")
print("    " + "、".join(f"{m}({int(v)})" for m, v in stable.head(8).items()))
for k in (3, 5, 8):
    yearly(flow(stable.head(k).index.tolist()), f"稳定 {k} 家")

print("\nC 换信号窗口(全样本 t 最高的 20 日,逐年到底稳不稳)")
grp = full.head(5).index.tolist()
for w in (5, 10, 20, 40):
    yearly(flow(grp, w), f"{w} 日窗")

print("\nD 对照:同样三条路用在生猪上,看这些检验本身有没有分辨力")
ldf, lmain = build("LH")
lfull = seat_alpha(ldf, "LH", min_days=200).sort_values("alpha", ascending=False)
def lflow(members, win=5):
    return ldf[ldf["member_key"].isin(members)].groupby("trade_date")["net"].sum().sort_index().diff(win)
def lyearly(sig, label):
    j = pd.concat([sig.rename("sig"), lmain], axis=1, sort=True); j["y"]=j.index.year
    out=[]
    for y,g in j.groupby("y"):
        r = power(g["sig"], g)
        if r: out.append((y, r[0]))
    pos=sum(1 for _,c in out if c>0)
    print(f"  {label}: " + " ".join(f"{y}{'+' if c>0 else '-'}" for y,c in out)
          + f"   → {pos} 正 / {len(out)-pos} 负")
lyearly(lflow(lfull.head(5).index.tolist()), "生猪 alpha 前5    ")
lyearly(-lflow(lfull.tail(5).index.tolist()), "生猪 垫底5家取反  ")

# ---------------------------------------------------------------- E
# A 段发现「反着跟散户」11 正 3 负。玻璃的教训就是全样本显著会骗人,
# 所以必须补滚动样本外:垫底席位也只能用当下之前的数据选。
print("\nE 对手盘信号的稳健性(垫底席位只用当下之前的数据选)")
print(f"  {'训练截止':11s}{'K':>3s}{'测试N':>7s}{'偏相关':>9s}{'t':>7s}  当时的垫底席位")
for k in (3, 5):
    for y in (2016, 2018, 2020, 2022, 2024):
        cut = pd.Timestamp(f"{y}-01-01")
        tr = seat_alpha(df[df["trade_date"] < cut], CODE, min_days=120)
        if tr.empty or len(tr) < k: continue
        losers = tr.sort_values("alpha").head(k).index.tolist()
        te = df[df["trade_date"] >= cut]
        sig = -te[te["member_key"].isin(losers)].groupby("trade_date")["net"].sum().sort_index().diff(5)
        r = power(sig, main[main.index >= cut])
        if r: print(f"  <{cut:%Y-%m}  {k:>3d}{r[2]:>7d}{r[0]:>+9.3f}{r[1]:>+7.2f}  "
                    f"{'、'.join(losers[:3])}")

print("\n  五档:信号分位 → 未来 20 日主力涨跌%")
losers = full.tail(3).index.tolist()
sig = -flow(losers)
j = pd.concat([sig.rename("sig"), main], axis=1, sort=True).dropna()
j["b"] = pd.qcut(j["sig"], 5, labels=["最负", "偏负", "中", "偏正", "最正"])
for b, g in j.groupby("b", observed=True):
    print(f"    {b}  {g['fwd'].mean()*100:+6.2f}%   N={len(g)}")

print("\n  这几家到底是谁、在玻璃上是什么样子:")
for m in losers:
    row = full.loc[m]
    print(f"    {m}: alpha {row['alpha']:+.1f} 亿  平均净持仓 {row['avg_net']:+,.0f} 手  在榜 {int(row['days'])} 日")
