"""玻璃(FG):合计流向的样本外不成立之后,换方向探还有没有别的形态。

①单家席位事件(金银那套形态) ②信号窗口扫描 ③分时期符号稳定性。
结论见 REPORT_FG_SKILL_v1.md:三条都不支持立项,**玻璃不做这套策略**。
留着这个脚本是为了让下一个人能一条命令复现,不用从头再推一遍。
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

print("① 单家席位的加减仓事件预测力(金银那套形态)")
d=df[df["dnet"].notna()&(df["dnet"]!=0)].sort_values(["member_key","trade_date"])
thr=d.groupby("member_key")["dnet"].transform(lambda s:s.abs().expanding(min_periods=60).quantile(0.80))
ev=d[(d["dnet"].abs()>=thr)&thr.notna()]
mkt=df.groupby("trade_date")[f"fwd{H}"].mean()
rows=[]
for mem,sub in ev.groupby("member_key"):
    if len(sub)<100: continue
    sign=np.sign(sub["dnet"]); r=sign*sub[f"fwd{H}"]
    ex=(r-sign*sub["trade_date"].map(mkt)).dropna()
    if len(ex)>30 and ex.std()>0:
        rows.append((mem,len(sub),ex.mean()*100,ex.mean()/(ex.std()/np.sqrt(len(ex)))))
t=pd.DataFrame(rows,columns=["席位","事件数","20日超额%","t"]).sort_values("t",ascending=False)
print(t.head(6).to_string(index=False))
print(f"  |t|>2 的席位:{(t['t'].abs()>2).sum()} / {len(t)} 家")

print("\n② 合计流向换信号窗口(用全样本 alpha 前 5,只为探形态)")
def alpha_all():
    dd=df.sort_values(["member_key","contract","trade_date"]).copy()
    g=dd.groupby(["member_key","contract"])
    dd["pn"]=g["net"].shift(); dd["ps"]=g["settle"].shift()
    gap=(dd["trade_date"]-g["trade_date"].shift()).dt.days
    dd=dd[dd["pn"].notna()&(gap<=5)]
    dd=dd.assign(dpx=(dd["settle"]-dd["ps"])*L.multiplier(CODE))
    gr=dd.groupby("member_key")
    a=gr.apply(lambda s:(s["dpx"]*s["pn"]).sum(),include_groups=False)-gr.apply(lambda s:(s["dpx"]*s["pn"].mean()).sum(),include_groups=False)
    return a[gr["trade_date"].nunique()>=250].sort_values(ascending=False)
grp=alpha_all().head(5).index.tolist()
print(f"  组={'、'.join(grp)}")
def power(sig,mn):
    j=pd.concat([sig.rename("sig"),mn[["fwd","past"]]],axis=1,sort=True).dropna()
    if len(j)<60: return None
    ry=j["fwd"]-np.polyval(np.polyfit(j["past"],j["fwd"],1),j["past"])
    rx=j["sig"]-np.polyval(np.polyfit(j["past"],j["sig"],1),j["past"])
    pr=float(np.corrcoef(ry,rx)[0,1])
    return pr, pr*np.sqrt((len(j)-3)/max(1e-12,1-pr**2)), len(j)
s=df[df["member_key"].isin(grp)].groupby("trade_date")["net"].sum().sort_index()
for w in (3,5,10,20,40):
    r=power(s.diff(w),main)
    if r: print(f"  窗口 {w:2d} 日:偏相关 {r[0]:+.3f}  t={r[1]:+.2f}  N={r[2]}")

print("\n③ 分时期(5 日窗):这个信号是不是被套利掉了")
for lo,hi in [("2013","2016"),("2016","2019"),("2019","2022"),("2022","2025"),("2025","2027")]:
    mn=main[(main.index>=lo)&(main.index<hi)]
    ss=s[(s.index>=lo)&(s.index<hi)]
    r=power(ss.diff(5),mn)
    if r: print(f"  {lo}~{hi}:偏相关 {r[0]:+.3f}  t={r[1]:+.2f}  N={r[2]}")
