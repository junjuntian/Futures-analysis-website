"""生猪 2026 每个主力末段 vs 次主力同期(DEC-123)。仓库根目录跑:python research/run_lh_2026_roll.py"""
import sys, pathlib, numpy as np, pandas as pd
sys.path.insert(0,'engine'); import hog_money as H
price=H.clean_price(pd.read_csv('research/data/lh_price.csv.gz')); H.use('LH'); mkt=H.main_series(price)
stx=price.pivot_table(index='trade_date',columns='contract',values='settle',aggfunc='first')
opx=price.pivot_table(index='trade_date',columns='contract',values='open',aggfunc='first') if 'open' in price.columns else None
def nxt(c,k=2):
    y,m=int(c[2:4]),int(c[4:6]); m+=k
    while m>12: m-=12; y+=1
    return f"LH{y:02d}{m:02d}"
m26=mkt[mkt.index>=pd.Timestamp('2026-01-01')]
print("=== 2026 每个主力合约:最后阶段走势 与 次主力(X+2)同期走势 ===")
for c in m26['main'].unique():
    seg=m26[m26['main']==c]
    n=nxt(c); n4=nxt(c,4)
    print(f"\n主力 {c}  任期 {seg.index[0].date()}~{seg.index[-1].date()}  窗口止点 {H.window_end(c).date()}  | 次主力 {n}  次次 {n4}")
    print(f"  {'剩天':>4}{'日期':<12}{c+'结算':>10}{'X自该日→止点%':>12}{n+'结算':>10}{n+' 后20日%':>12}{n4+' 后20日%':>12}{n+'−'+n4+' 价差':>14}")
    for dl in (30,25,22,20,15,10,5):
        r=seg[seg['dleft']==dl]
        if r.empty: continue
        d=r.index[0]; j=stx.index.get_loc(d)
        # X 自该日到窗口止点(最后可持日)的涨跌
        last=seg[seg['dleft']>=1].index[-1]
        x_end=(stx.at[last,c]/stx.at[d,c]-1)*100
        def r20(cc):
            s=stx[cc].iloc[j:j+21].dropna() if cc in stx.columns else pd.Series(dtype=float)
            return (s.iloc[-1]/s.iloc[0]-1)*100 if len(s)>5 else np.nan
        sp=stx.at[d,n]-stx.at[d,n4] if n4 in stx.columns else np.nan
        print(f"  {dl:>4}{str(d.date()):<12}{stx.at[d,c]:>10.0f}{x_end:>+12.1f}{stx.at[d,n]:>10.0f}{r20(n):>+12.1f}{r20(n4):>+12.1f}{sp:>+14.0f}")
