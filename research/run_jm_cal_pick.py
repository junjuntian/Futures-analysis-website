# -*- coding: utf-8 -*-
"""焦煤:跨月跟随该跟哪一家(2026-09-01 运营者:「测一下有没有价差做得更好的席位,
经常在榜,方便跟随的」)。

**先厘清一个会选错人的陷阱。** 上一轮 `run_jm_seat_pnl.py` 里中信的「价差部分」
是 +3.36 亿,看着比东证(−1.16 亿)好得多,运营者据此想把卡换成中信。但那一列是
**全部 737 天**的残差,而中信 737 天里只有 114 天真在做跨月 —— 它那 3.36 亿来自
**不做跨月的那些日子**;真正跨月的 114 天里,中信价差是 **−9,947 万**,比东证的
−8,125 万还差。**换卡要看的是「它做跨月时赚不赚」,不是「它一年到头的残差」。**

所以本文的排序键是 `跨月期价差 ÷ 跨月天数`(日均),并按运营者的三条硬要求过滤:
  1. **价差做得好** —— 跨月期价差为正;
  2. **经常在榜** —— 在榜率 ≥80%(掉榜的日子跟不了,信号是断的);
  3. **方便跟随** —— 跨月天数 ≥60(一年至少二十天能跟,否则这张卡常年空着)。

**丑话写在前面**:这是 57 家里事后挑最强,与 IH 那轮同一个选择偏差陷阱
(`research/PITFALLS.md` 第 5 条)。所以每家同时报**逐年**,只有年年为正才谈得上
可跟;一年赌对撑起三年的,不算。

跑法:python research/run_jm_cal_pick.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

MULT = 60.0
MIN_ON_RATE = 0.80               # 在榜率下限:掉榜就跟不了
MIN_CAL_DAYS = 60                # 跨月天数下限:太少这张卡常年空着
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "jm_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "jm_seat.csv.gz"))
H.use("JM")
mkt = H.main_series(price)
op, st = H.contract_prices(price)
idx = mkt.index[mkt.index >= seat["trade_date"].min()]
main_settle = pd.Series(
    [st[m].asof(d) if isinstance(m, str) and m in st.columns else np.nan
     for d, m in zip(idx, mkt["main"].reindex(idx))], index=idx)
d_main = main_settle.diff()


def shape_of(row):
    r = row.dropna()
    lo = r[r > 0].sum()
    sh = -r[r < 0].sum()
    if lo <= 0 or sh <= 0:
        return "单边", np.nan
    if min(lo, sh) * 3 < max(lo, sh):
        return "纯趋势", max(lo, sh) / min(lo, sh)
    ratio = max(lo, sh) / min(lo, sh)
    return ("多远空近" if r[r > 0].idxmax() > r[r < 0].idxmin() else "多近空远"), ratio


def analyse(member):
    sub = seat[seat["member_key"] == member]
    net = sub.pivot_table(index="trade_date", columns="contract",
                          values="net_off", aggfunc="sum").reindex(idx)
    daily = pd.Series(0.0, index=idx)
    for c in net.columns:
        if not isinstance(c, str) or c not in st.columns:
            continue
        ds = st[c].reindex(idx).diff()
        ok = net[c].shift(1).notna() & ds.notna()
        daily += (net[c].shift(1) * ds * MULT).where(ok, 0.0).fillna(0.0)
    dir_pnl = (net.sum(axis=1, min_count=1).shift(1) * d_main * MULT).fillna(0.0)
    sp = daily - dir_pnl
    shp = pd.Series({d: shape_of(net.loc[d])[0] for d in idx})
    cal = shp.isin(["多远空近", "多近空远"])
    on = net.notna().any(axis=1)
    if cal.sum() < MIN_CAL_DAYS or on.mean() < MIN_ON_RATE:
        return None
    years = {}
    for y, g in sp[cal].groupby(sp[cal].index.year):
        years[y] = g.sum() / 1e4
    last_shape, last_ratio = shape_of(net.iloc[-1])
    return {"m": member, "on_rate": on.mean() * 100, "cal_days": int(cal.sum()),
            "cal_sp": sp[cal].sum(), "per_day": sp[cal].sum() / cal.sum(),
            "cal_total": daily[cal].sum(), "years": years,
            "pos_years": sum(1 for v in years.values() if v > 0), "ny": len(years),
            "shape": last_shape, "ratio": last_ratio}


counts = seat.groupby("member_key")["trade_date"].nunique()
rows = [r for r in (analyse(m) for m, c in counts.items() if c >= 200) if r]
rows.sort(key=lambda r: -r["per_day"])

L = [f"焦煤跨月跟随:该跟哪一家(样本 {idx[0].date()} ~ {idx[-1].date()})", ""]
L.append(f"筛子(运营者 2026-09-01 的三条):价差为正 · 在榜率 ≥{MIN_ON_RATE*100:.0f}% · "
         f"跨月天数 ≥{MIN_CAL_DAYS}。过筛 {len(rows)} 家,按**跨月期日均价差**排序。")
L.append("")
L.append(f"{'席位':<10}{'在榜率':>7}{'跨月天':>7}{'跨月期价差':>11}{'日均(万)':>10}"
         f"{'跨月期总账':>11}{'正年':>6}  当前形态")
L.append("-" * 82)
for r in rows:
    L.append(f"{r['m']:<10}{r['on_rate']:>6.0f}%{r['cal_days']:>7}{r['cal_sp']/1e4:>+11.0f}"
             f"{r['per_day']/1e4:>+10.1f}{r['cal_total']/1e4:>+11.0f}"
             f"{r['pos_years']:>3}/{r['ny']:<2}  {r['shape']}"
             + (f" 1:{r['ratio']:.2f}" if np.isfinite(r['ratio']) else ""))
L.append("-" * 82)
L.append("")
L.append("## 逐年(万元):一年赌对撑起三年的不算")
L.append("")
yrs = sorted({y for r in rows for y in r["years"]})
L.append(f"{'席位':<10}" + "".join(f"{y:>12}" for y in yrs))
L.append("-" * (10 + 12 * len(yrs)))
for r in rows:
    L.append(f"{r['m']:<10}" + "".join(
        f"{r['years'].get(y, 0.0):>+12.0f}" for y in yrs))
L.append("")
solid = [r for r in rows if r["pos_years"] == r["ny"]]
L.append(f"**年年为正 = {len(solid)} 家**:" + ("、".join(r["m"] for r in solid) if solid else "(无)"))
L.append("")
L.append("**必须一起读的丑话**:①这是 57 家里事后挑最强,与 IH 那轮同一个选择偏差")
L.append("陷阱(PITFALLS 第 5 条),真正的检验是「每年初只用过去的数据选人」;")
L.append("②「价差部分」是总盯市减方向部分的**残差**,含各合约对主力的基差变动,")
L.append("不是干净的套利损益;③席位=该会员名下全部客户+自营,不是它自己的钱;")
L.append("④看不到成交价,做市型套利靠买卖价差赚的这套完全看不见。")
io.open(OUT / "jm_cal_pick.txt", "w", encoding="utf-8").write("\n".join(L))
print(f"done: {len(rows)} candidates, {len(solid)} positive every year")
