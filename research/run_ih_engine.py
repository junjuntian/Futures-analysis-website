# -*- coding: utf-8 -*-
"""IH 阵营主引擎回放(PLAN_IH_MODEL_v1 续章 2,2026-08-30)。

套装 A(战役制)/B(单席位)不支持、全席位无人过筛之后,补上没测的主体:
**五品种现行主引擎的标准路** —— 滚动选人 + 阵营流向信号 + H.replay,
与生产五品种同一套代码,不另起口径。基准 = 恒定满仓多/空(区分 alpha 与 beta)。
跑法:python research/run_ih_engine.py
"""
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
op, st = H.contract_prices(price)
roll, _, _ = H.rolling_groups(seat, price, mkt.index)
rdf, _ = H.retail_series(seat, mkt.index)
CUT = pd.Timestamp("2019-04-22")

L = [f"IH 阵营主引擎回放(与生产五品种同一套 H.replay;数据至 {mkt.index[-1].date()})", ""]


def perf(daily, label):
    dd = pd.Series(daily).dropna()
    if len(dd) < 60:
        return f"{label}: 样本不足"
    eq = (1 + dd).cumprod()
    mdd = float((eq / eq.cummax() - 1).min()) * 100
    sh = float(dd.mean() / dd.std() * np.sqrt(242)) if dd.std() > 0 else np.nan
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in dd.groupby(dd.index.year)}
    pos = sum(1 for v in ys.values() if v > 0)
    inmkt = float((dd != 0).mean() * 100)
    return (f"{label}: 复利 {(float(eq.iloc[-1])-1)*100:+.1f}%  夏普 {sh:.2f}  回撤 {mdd:+.1f}%"
            f"  正年 {pos}/{len(ys)}  在场 {inmkt:.0f}%")


# 基准:恒定满仓(次日开盘口径,同 replay)
L.append("── 基准 ──")
L.append(perf(mkt["ret_open"], "恒定满仓多"))
L.append(perf(-mkt["ret_open"], "恒定满仓空"))
L.append(perf(mkt["ret_open"].loc[CUT:], "恒定满仓多(2019-04 后)"))
L.append("")

# 主引擎:阵营流向(标准 signal_series → replay)
L.append("── 阵营主引擎(流向 z,生产同款)──")
sig = H.signal_series(seat, roll)
trades, _, daily = H.replay(sig, mkt, rdf, op, st)
closed = [t for t in trades if t.get("ret_pct") is not None]
rets = np.array([t["ret_pct"] for t in closed])
L.append(perf(pd.Series(daily), "全期"))
L.append(perf(pd.Series(daily).loc[CUT:], "2019-04 后"))
if len(closed):
    t_stat = float(rets.mean() / rets.std(ddof=1) * np.sqrt(len(rets))) if len(rets) > 2 else np.nan
    L.append(f"  {len(closed)} 笔  均 {rets.mean():+.2f}%/笔  胜率 {(rets>0).mean()*100:.0f}%  t={t_stat:.2f}")
    # 安慰剂:进出场日期结构保持,起点随机平移(与套装同法)
    rng = np.random.default_rng(7)
    lens = []
    for t in closed:
        c = t["contract"]
        if c in st.columns:
            seg = st[c].dropna().loc[t["entry_date"]:t["exit_date"]] if t.get("exit_date") else None
            if seg is not None and len(seg) > 2:
                lens.append((c, 1.0 if t["side"] == "long" else -1.0, len(seg) - 1))
    sims = []
    for k in range(1000):
        tot = []
        for c, sd, n in lens:
            px = st[c].dropna()
            if len(px) <= n + 2:
                continue
            i0 = int(rng.integers(0, len(px) - n - 1))
            r = sd * (float(px.iloc[i0 + n]) / float(px.iloc[i0]) - 1) * 100
            if np.isfinite(r):
                tot.append(r)
        if tot:
            sims.append(np.mean(tot))
    sims = np.array(sims)
    p_val = float((sims >= rets.mean()).mean()) if len(sims) else np.nan
    L.append(f"  安慰剂: 随机均 {sims.mean():+.2f}%/笔  p = {p_val:.3f}")
L.append("")

# 阵营净持仓方向(最简单的跟随:前20阵营合计的 sign,T+1)
L.append("── 阵营净持仓方向(sign,T+1,无门槛最简版)──")
pos = np.sign(sig["net"]).replace(0, np.nan).ffill()
base = pos.shift(2) * mkt["ret_open"]
L.append(perf(base, "全期"))
L.append(perf(base.loc[CUT:], "2019-04 后"))
L.append("")

# 全体前20合计(不选人:官方口径所有上榜会员多空合计的方向)
L.append("── 全榜合计方向(不选人,官方前20全体)──")
all20 = seat.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
all20 = all20.reindex(mkt.index).ffill()
pos2 = np.sign(all20).replace(0, np.nan).ffill()
base2 = pos2.shift(2) * mkt["ret_open"]
L.append(perf(base2, "全期"))
L.append(perf(base2.loc[CUT:], "2019-04 后"))

io.open(OUT / "ih_engine.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")
