"""玻璃×纯碱:能不能用 SA 把 FG 信号里的套利腿剔掉。

运营者 2026-08-19 的思路:FG 与 SA 存在套利关系,玻璃信号失效的推测原因正是
「持仓变化里混着 FG-SA 套利腿的调整」。若真如此,把两个品种放一起看应当能分离:

  同向变化(都加空/都加多) → 更可能是对地产链的方向判断
  反向变化(一边加空一边加多) → 套利腿在调,与单品种方向无关

如果「同向时 FG 信号有效、反向时无效」,那玻璃就不是不能做,是**要用 SA 过滤**。
"""
import numpy as np, pandas as pd
import lhlib as L
from run_flow_skill import build, seat_alpha, power

H = 20
fg, fg_main = build("FG")
sa, sa_main = build("SA")
# 纯碱 2019-12 才有,两品种只能在共同区间比
lo = max(fg["trade_date"].min(), sa["trade_date"].min())
print(f"共同区间自 {lo:%Y-%m-%d};FG {fg['trade_date'].nunique()} 日 / SA {sa['trade_date'].nunique()} 日")

fg_a = seat_alpha(fg[fg["trade_date"] >= lo], "FG").sort_values("alpha", ascending=False)
sa_a = seat_alpha(sa, "SA").sort_values("alpha", ascending=False)
fg5, sa5 = fg_a.head(5).index.tolist(), sa_a.head(5).index.tolist()
print(f"\nFG alpha 前5:{'、'.join(fg5)}")
print(f"SA alpha 前5:{'、'.join(sa5)}")
print(f"重合 {len(set(fg5)&set(sa5))} 家:{'、'.join(set(fg5)&set(sa5)) or '无'}")

print("\n① 同一席位在两品种上的持仓变化,是不是负相关(负相关=在做套利)")
rows = []
for m in set(fg["member_key"]) & set(sa["member_key"]):
    a = fg[fg["member_key"] == m].groupby("trade_date")["net"].sum().diff(5)
    b = sa[sa["member_key"] == m].groupby("trade_date")["net"].sum().diff(5)
    j = pd.concat([a.rename("f"), b.rename("s")], axis=1).dropna()
    if len(j) > 250:
        rows.append((m, j["f"].corr(j["s"]), len(j)))
t = pd.DataFrame(rows, columns=["席位", "FGvsSA相关", "N"]).sort_values("FGvsSA相关")
print(f"  可比 {len(t)} 家,相关系数:中位 {t['FGvsSA相关'].median():+.3f}  "
      f"负相关的 {(t['FGvsSA相关']<0).sum()} 家 / 正相关 {(t['FGvsSA相关']>0).sum()} 家")
print("  最负 3 家(最像在做套利):"); print(t.head(3).to_string(index=False))
print("  最正 3 家(两边同向,更像方向判断):"); print(t.tail(3).to_string(index=False))

print("\n② 分解:两品种合计流向同向 / 反向 时,FG 信号各自灵不灵")
f_sig = fg[fg["member_key"].isin(fg5)].groupby("trade_date")["net"].sum().sort_index().diff(5)
s_sig = sa[sa["member_key"].isin(sa5)].groupby("trade_date")["net"].sum().sort_index().diff(5)
j = pd.concat([f_sig.rename("fsig"), s_sig.rename("ssig"), fg_main], axis=1, sort=True).dropna()
same = np.sign(j["fsig"]) == np.sign(j["ssig"])
for label, sub in [("两品种同向", j[same]), ("两品种反向", j[~same])]:
    r = power(sub["fsig"], sub)
    if r: print(f"  {label}  偏相关 {r[0]:+.3f}  t={r[1]:+.2f}  N={r[2]}")

print("\n③ 对照:同样分解用在 SA 上")
j2 = pd.concat([f_sig.rename("fsig"), s_sig.rename("ssig"), sa_main], axis=1, sort=True).dropna()
same2 = np.sign(j2["fsig"]) == np.sign(j2["ssig"])
for label, sub in [("两品种同向", j2[same2]), ("两品种反向", j2[~same2])]:
    r = power(sub["ssig"], sub)
    if r: print(f"  {label}  偏相关 {r[0]:+.3f}  t={r[1]:+.2f}  N={r[2]}")

print("\n④ 两品种流向之差,能不能预测 FG-SA 价差")
px_f = fg_main["settle"]; px_s = sa_main["settle"]
spread = (px_f - px_s).dropna()
fwd_sp = spread.shift(-H) - spread          # 价差是绝对数,用差不用比
z = ((f_sig - f_sig.rolling(120).mean()) / f_sig.rolling(120).std()
     - (s_sig - s_sig.rolling(120).mean()) / s_sig.rolling(120).std())
k = pd.concat([z.rename("d"), fwd_sp.rename("fwd"),
               (spread - spread.shift(20)).rename("past")], axis=1).dropna()
if len(k) > 100:
    ry = k["fwd"] - np.polyval(np.polyfit(k["past"], k["fwd"], 1), k["past"])
    rx = k["d"] - np.polyval(np.polyfit(k["past"], k["d"], 1), k["past"])
    pr = float(np.corrcoef(ry, rx)[0, 1])
    print(f"  偏相关 {pr:+.3f}  t={pr*np.sqrt((len(k)-3)/max(1e-12,1-pr**2)):+.2f}  N={len(k)}")
    k["y"] = k.index.year
    sgn = [(y, np.corrcoef(g["d"], g["fwd"])[0,1]) for y, g in k.groupby("y") if len(g) > 80]
    print("  逐年:" + "  ".join(f"{y}{'正' if c>0 else '负'}" for y, c in sgn))

# ---------------------------------------------------------------- ⑤
# ④ 那个 t=+5.43 是全样本算的。玻璃的教训就是全样本显著、逐年翻转,
# 所以必须补滚动样本外 + 逐年 t,过不了就不算数。
print("\n⑤ 价差信号的稳健性(玻璃就栽在只看全样本)")
def spread_power(sub):
    if len(sub) < 100: return None
    ry = sub["fwd"] - np.polyval(np.polyfit(sub["past"], sub["fwd"], 1), sub["past"])
    rx = sub["d"] - np.polyval(np.polyfit(sub["past"], sub["d"], 1), sub["past"])
    pr = float(np.corrcoef(ry, rx)[0, 1])
    return pr, pr * np.sqrt((len(sub) - 3) / max(1e-12, 1 - pr ** 2)), len(sub)

print("  逐年(带 t 值,不只看符号):")
for y, g in k.groupby("y"):
    r = spread_power(g)
    if r: print(f"    {y}  偏相关 {r[0]:+.3f}  t={r[1]:+6.2f}  N={r[2]:4d}  {'正' if r[0]>0 else '负'}")

print("  滚动样本外(席位组只用当下之前的数据选):")
for cut in [pd.Timestamp(f"{y}-01-01") for y in (2022, 2023, 2024, 2025)]:
    fa = seat_alpha(fg[fg["trade_date"] < cut], "FG", min_days=120)
    sa_ = seat_alpha(sa[sa["trade_date"] < cut], "SA", min_days=120)
    if fa.empty or sa_.empty: continue
    g5 = fa.sort_values("alpha", ascending=False).head(5).index.tolist()
    h5 = sa_.sort_values("alpha", ascending=False).head(5).index.tolist()
    fs = fg[(fg["trade_date"] >= cut) & fg["member_key"].isin(g5)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    ss = sa[(sa["trade_date"] >= cut) & sa["member_key"].isin(h5)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    zz = ((fs - fs.rolling(120).mean()) / fs.rolling(120).std()
          - (ss - ss.rolling(120).mean()) / ss.rolling(120).std())
    kk = pd.concat([zz.rename("d"), fwd_sp.rename("fwd"),
                    (spread - spread.shift(20)).rename("past")], axis=1).dropna()
    kk = kk[kk.index >= cut]
    r = spread_power(kk)
    if r: print(f"    训练<{cut:%Y-%m}  偏相关 {r[0]:+.3f}  t={r[1]:+6.2f}  N={r[2]:4d}")

print("\n  分档:信号最强/最弱两端,未来 20 日价差怎么走(元/吨)")
k["b"] = pd.qcut(k["d"], 5, labels=["最负", "偏负", "中", "偏正", "最正"])
for b, g in k.groupby("b", observed=True):
    print(f"    {b}  平均 {g['fwd'].mean():+7.1f}   N={len(g)}")
