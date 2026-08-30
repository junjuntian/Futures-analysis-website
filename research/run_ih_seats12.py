# -*- coding: utf-8 -*-
"""三禾盈亏榜前 12 席位逐家择时检验(PLAN_IH_MODEL_v2 v2.1 追加)。
跑法:python research/run_ih_seats12.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / "ih_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "ih_seat.csv.gz"))
H.use("IH")
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
mains = [c for c in dict.fromkeys(mkt["main"]) if isinstance(c, str)]
CUT19 = pd.Timestamp("2019-04-22")
CUT23 = pd.Timestamp("2023-08-30")   # 三禾榜单窗口起点 = 主判窗口终点

ROSTER = ["广发期货", "国泰君安", "中银期货", "光大期货", "中泰期货", "海通期货",
          "海证期货", "兴证期货", "中金财富", "国信期货", "东海期货", "中财期货"]

L = [f"三禾盈亏榜前12·逐家择时检验(数据至 {mkt.index[-1].date()};主判窗口=2023-08-30 之前)", ""]


def member_sig(member):
    sub = seat[seat["member_key"] == member]
    sig = pd.Series(np.nan, index=mkt.index)
    for c in mains:
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = mkt.index[mkt["main"] == c]
        sig.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
    return sig


def sh_of(dd):
    dd = pd.Series(dd).dropna()
    if len(dd) < 60 or dd.std() == 0:
        return np.nan
    return float(dd.mean() / dd.std() * np.sqrt(242))


def cum_of(dd):
    dd = pd.Series(dd).dropna()
    return (float((1 + dd).prod()) - 1) * 100 if len(dd) else np.nan


bench = mkt["ret_open"]
L.append(f"基准恒多: 主判窗口(~2023-08)夏普 {sh_of(bench.loc[:CUT23]):.2f}"
         f"  2023-08后 {sh_of(bench.loc[CUT23:]):.2f}")
L.append("")
hdr = f"{'席位':<6}|主判窗口(至23-08): 夏普 剥beta后 |23-08后: 夏普 |翻向事件: 次数 5日均(t) 20日均(t) |IC(t)"
L.append(hdr)
L.append("-" * 100)

rows = []
for m in ROSTER:
    sig = member_sig(m)
    pos = np.sign(sig).replace(0, np.nan).ffill()
    base = pos.shift(2) * bench
    early, late = base.loc[:CUT23], base.loc[CUT23:]
    sh_e, sh_l = sh_of(early), sh_of(late)
    # beta 剥离:减去平均净暴露对应的恒多收益
    expo = float(pos.shift(2).loc[:CUT23].mean()) if pos.shift(2).loc[:CUT23].notna().any() else np.nan
    strip = early - expo * bench.loc[:CUT23]
    sh_strip = sh_of(strip)
    # 翻向事件研究(全样本;事件=方向变号)
    flips = pos[(pos != pos.shift()) & pos.notna() & pos.shift().notna()]
    ev5, ev20 = [], []
    for d, sd in flips.items():
        loc = mkt.index.get_indexer([d])[0]
        if loc + 21 < len(mkt):
            f5 = float((1 + bench.iloc[loc + 1:loc + 6]).prod() - 1) * 100
            f20 = float((1 + bench.iloc[loc + 1:loc + 21]).prod() - 1) * 100
            ev5.append(sd * f5)
            ev20.append(sd * f20)
    def tt(a):
        a = np.array(a)
        return (float(a.mean()), float(a.mean() / a.std(ddof=1) * np.sqrt(len(a))) if len(a) > 3 and a.std(ddof=1) > 0 else np.nan)
    m5, t5 = tt(ev5) if ev5 else (np.nan, np.nan)
    m20, t20 = tt(ev20) if ev20 else (np.nan, np.nan)
    dn = sig.diff(5)
    fwd = bench.rolling(5).sum().shift(-5)
    sel = pd.concat([dn, fwd], axis=1).dropna().iloc[::5]
    ra, rb = sel.iloc[:, 0].rank(), sel.iloc[:, 1].rank()
    ic = float(np.corrcoef(ra, rb)[0, 1]) if len(sel) > 10 else np.nan
    t_ic = ic * np.sqrt(max(len(sel) - 2, 1)) / np.sqrt(max(1 - ic ** 2, 1e-9)) if np.isfinite(ic) else np.nan
    presence = float(pos.notna().mean() * 100)
    rows.append((m, sh_e, sh_strip, sh_l, len(ev5), m5, t5, m20, t20, ic, t_ic, presence))
    L.append(f"{m:<6}| {sh_e:+.2f}  {sh_strip:+.2f} | {sh_l:+.2f} |"
             f" {len(ev5):>3} 次 {m5:+.2f}%(t{t5:+.1f}) {m20:+.2f}%(t{t20:+.1f}) |"
             f" {ic:+.3f}(t{t_ic:+.1f})  在榜{presence:.0f}%")

L.append("")
best = sorted([r for r in rows if np.isfinite(r[1])], key=lambda x: -x[1])[:3]
L.append("主判窗口夏普前三: " + "  ".join(f"{r[0]} {r[1]:+.2f}(剥beta {r[2]:+.2f})" for r in best))
sig_ev = [r for r in rows if np.isfinite(r[8]) and r[8] > 2]
L.append("翻向事件 20 日 t>2 的: " + ("、".join(f"{r[0]}({r[7]:+.2f}%,t{r[8]:+.1f})" for r in sig_ev) if sig_ev else "无"))

io.open(OUT / "ih_seats12.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
