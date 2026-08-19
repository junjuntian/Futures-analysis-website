"""生猪 Q6：机构合计信号相对「纯动量」有没有增量。

run_lh_skill.py 的 Q5 发现机构合计净持仓的变化能预测跌幅,但那还不够——
如果机构只是跟着价格跌在加空,那这个信号不过是动量的马甲,不值得为它做一套引擎。
这里把动量控制住再看机构信号还剩多少:相关性、偏相关 t 值、以及在同一动量档
内机构信号还有没有区分度。

结论见 REPORT_LH_SKILL_v1.md 第 Q6 节。
"""
import numpy as np, pandas as pd, lhlib as L

price = L.load_price(); seat = L.load_seat()
fwd = L.forward_returns(price, (5, 10, 20)); mc = L.main_contract(price)
df = seat.merge(fwd[["contract","trade_date","settle","fwd5","fwd10","fwd20"]],
                on=["contract","trade_date"], how="inner")
smart = ["国泰君安","永安期货","格林大华","中粮期货","国投期货","东证期货","东吴期货","南华期货"]
inst = df[df["member_key"].isin(smart)].groupby("trade_date")["net"].sum().sort_index()

m = mc.set_index("trade_date")["main"]; px = fwd.set_index(["contract","trade_date"])
rows=[]
for d,c in m.items():
    if (c,d) in px.index:
        r = px.loc[(c,d)]
        rows.append((d, r["settle"], r["fwd5"], r["fwd10"], r["fwd20"]))
mret = pd.DataFrame(rows, columns=["trade_date","settle","fwd5","fwd10","fwd20"]).set_index("trade_date")
# 过去收益也逐合约：主力换月那天不跨合约相除
mret["past20"] = mret.groupby(mret.index.map(m))["settle"].pct_change(20)

j = pd.concat([inst.diff(5).rename("chg5"), inst.diff(20).rename("chg20"), mret], axis=1).dropna()
print(f"N={len(j)}")
print("\n① 机构调仓是不是只在跟着行情走(与过去 20 日收益的相关):")
print(f"   chg5  vs past20 : {j['chg5'].corr(j['past20']):+.3f}")
print(f"   chg20 vs past20 : {j['chg20'].corr(j['past20']):+.3f}")

print("\n② 预测力对比(相关系数,越负=越能预测跌):")
print(f"   {'':14s}{'fwd5':>9s}{'fwd10':>9s}{'fwd20':>9s}")
for name, col in [("纯动量 past20", "past20"), ("机构 chg5", "chg5"), ("机构 chg20", "chg20")]:
    print(f"   {name:14s}" + "".join(f"{j[col].corr(j[f'fwd{h}']):>+9.3f}" for h in (5,10,20)))

print("\n③ 控制动量后机构信号的偏相关(OLS 残差法):")
for h in (5,10,20):
    y = j[f"fwd{h}"]; x1 = j["past20"]; x2 = j["chg5"]
    ry = y - np.polyval(np.polyfit(x1, y, 1), x1)
    rx = x2 - np.polyval(np.polyfit(x1, x2, 1), x1)
    r = np.corrcoef(ry, rx)[0,1]
    t = r*np.sqrt((len(j)-3)/(1-r**2))
    print(f"   fwd{h:<3d} 偏相关 {r:+.3f}   t={t:+.2f}")

print("\n④ 双分档:动量 × 机构信号 的 fwd20 均值%(看机构在同一动量档内还有没有区分度)")
j["mq"] = pd.qcut(j["past20"], 3, labels=["跌","平","涨"])
j["iq"] = pd.qcut(j["chg5"], 3, labels=["加空","中","减空"])
print((j.pivot_table(index="mq", columns="iq", values="fwd20", aggfunc="mean", observed=True)*100).round(2).to_string())
