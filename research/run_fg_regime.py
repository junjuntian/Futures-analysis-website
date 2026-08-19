"""玻璃:信号符号翻转,是不是因为牛熊市况不同?

运营者 2026-08-19 质疑:玻璃跟着房地产周期走,有牛有熊。若信号在涨势/跌势里
方向相反,那不是「信号不成立」而是「要分市况用」——结论完全不同,必须验。

生猪只有熊市一种市况,这个检验在生猪上做不了;玻璃 14 年跨完整周期,做得了。
"""
import numpy as np, pandas as pd
import lhlib as L

CODE="FG"; H=20
price=L.load_price(CODE); seat=L.load_seat(CODE)
fwd=L.forward_returns(price,(5,10,H)); mc=L.main_contract(price)
df=seat.merge(fwd[["contract","trade_date","settle",f"fwd{H}"]],on=["contract","trade_date"],how="inner")
m=mc.set_index("trade_date")["main"]; px=fwd.set_index(["contract","trade_date"])
rows=[(d,px.loc[(c,d)]["settle"],px.loc[(c,d)][f"fwd{H}"]) for d,c in m.items() if (c,d) in px.index]
main=pd.DataFrame(rows,columns=["trade_date","settle","fwd"]).set_index("trade_date")
main["past"]=main["settle"].pct_change(20)
main["ma120"]=main["settle"].rolling(120).mean()
main["ma250"]=main["settle"].rolling(250).mean()

def alpha_upto(hi=None, min_days=250):
    d=df if hi is None else df[df["trade_date"]<hi]
    d=d.sort_values(["member_key","contract","trade_date"]).copy()
    g=d.groupby(["member_key","contract"])
    d["pn"]=g["net"].shift(); d["ps"]=g["settle"].shift()
    gap=(d["trade_date"]-g["trade_date"].shift()).dt.days
    d=d[d["pn"].notna()&(gap<=5)]
    d=d.assign(dpx=(d["settle"]-d["ps"])*L.multiplier(CODE))
    gr=d.groupby("member_key")
    a=(gr.apply(lambda s:(s["dpx"]*s["pn"]).sum(),include_groups=False)
       -gr.apply(lambda s:(s["dpx"]*s["pn"].mean()).sum(),include_groups=False))
    return a[gr["trade_date"].nunique()>=min_days].sort_values(ascending=False)

grp=alpha_upto().head(5).index.tolist()
print(f"席位组(全样本 alpha 前 5)={'、'.join(grp)}")
sig=df[df["member_key"].isin(grp)].groupby("trade_date")["net"].sum().sort_index().diff(5)

def stat(sub):
    j=sub.dropna()
    if len(j)<80: return None
    ry=j["fwd"]-np.polyval(np.polyfit(j["past"],j["fwd"],1),j["past"])
    rx=j["sig"]-np.polyval(np.polyfit(j["past"],j["sig"],1),j["past"])
    pr=float(np.corrcoef(ry,rx)[0,1])
    return pr, pr*np.sqrt((len(j)-3)/max(1e-12,1-pr**2)), len(j)

j=pd.concat([sig.rename("sig"),main],axis=1,sort=True)

print("\n① 按趋势状态分(主力相对半年线)")
for label,mask in [("价格在 MA120 之上(涨势)", j["settle"]>j["ma120"]),
                   ("价格在 MA120 之下(跌势)", j["settle"]<j["ma120"])]:
    r=stat(j[mask][["sig","fwd","past"]])
    if r: print(f"  {label:26s} 偏相关 {r[0]:+.3f}  t={r[1]:+.2f}  N={r[2]}")

print("\n② 按年线分(主力相对 MA250)")
for label,mask in [("MA250 之上", j["settle"]>j["ma250"]), ("MA250 之下", j["settle"]<j["ma250"])]:
    r=stat(j[mask][["sig","fwd","past"]])
    if r: print(f"  {label:26s} 偏相关 {r[0]:+.3f}  t={r[1]:+.2f}  N={r[2]}")

print("\n③ 按过去 60 日涨跌分三档")
j["p60"]=j["settle"].pct_change(60)
q=j["p60"].quantile([1/3,2/3])
for label,mask in [("跌得多", j["p60"]<=q.iloc[0]), ("横盘", (j["p60"]>q.iloc[0])&(j["p60"]<q.iloc[1])),
                   ("涨得多", j["p60"]>=q.iloc[1])]:
    r=stat(j[mask][["sig","fwd","past"]])
    if r: print(f"  {label:26s} 偏相关 {r[0]:+.3f}  t={r[1]:+.2f}  N={r[2]}")

print("\n④ 逐年(看符号稳不稳,而不是拼几个大段)")
j["y"]=j.index.year
ok=0; tot=0
for y,sub in j.groupby("y"):
    r=stat(sub[["sig","fwd","past"]])
    if r:
        tot+=1; ok+= 1 if r[0]>0 else 0
        flag="正" if r[0]>0 else "负"
        print(f"  {y}  偏相关 {r[0]:+.3f}  t={r[1]:+6.2f}  N={r[2]:4d}  {flag}")
print(f"  → {tot} 年里 {ok} 年为正、{tot-ok} 年为负")

print("\n⑤ 对照:生猪逐年(它只有熊市,但符号该是一边倒的)")
lp=L.load_price("LH"); ls=L.load_seat("LH")
lf=L.forward_returns(lp,(5,10,H)); lmc=L.main_contract(lp)
ld=ls.merge(lf[["contract","trade_date","settle",f"fwd{H}"]],on=["contract","trade_date"],how="inner")
lm=lmc.set_index("trade_date")["main"]; lpx=lf.set_index(["contract","trade_date"])
lrows=[(d,lpx.loc[(c,d)]["settle"],lpx.loc[(c,d)][f"fwd{H}"]) for d,c in lm.items() if (c,d) in lpx.index]
lmain=pd.DataFrame(lrows,columns=["trade_date","settle","fwd"]).set_index("trade_date")
lmain["past"]=lmain["settle"].pct_change(20)
lgrp=["东证期货","国泰君安","东吴期货","浙商期货","兴证期货"]
lsig=ld[ld["member_key"].isin(lgrp)].groupby("trade_date")["net"].sum().sort_index().diff(5)
lj=pd.concat([lsig.rename("sig"),lmain],axis=1,sort=True); lj["y"]=lj.index.year
for y,sub in lj.groupby("y"):
    r=stat(sub[["sig","fwd","past"]])
    if r: print(f"  {y}  偏相关 {r[0]:+.3f}  t={r[1]:+6.2f}  N={r[2]:4d}  {'正' if r[0]>0 else '负'}")
