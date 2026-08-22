"""成本进场信号的闸门(运营者 2026-08-22 拍板「先走成本信号的闸门」)。

候选(REPORT_COST_ENTRY_v1):玻璃 = 成本+卸仓≤30%+机构还在加仓;
纯碱/鸡蛋 = 成本+卸仓≤30%。焦煤生猪不是候选(实测不如现行,保持流量信号)。

## 事先写死的过关判据(跑之前定,跑完不许改)

1. **逐年对比**:候选赢基线的年份 ≥ 半数,且候选自身正年份 ≥ 半数;
2. **选臂 walk-forward**:每年只用**之前年份**的夏普选「基线 or 候选」,
   链起来的结果不输给「从头到尾用基线」—— 这是防「全样本比较」的马后炮;
3. **价源敏感性**:成本与比价从结算价换成收盘价,候选仍赢基线(排序不翻);
4. **翻向日与换组余波**:排除轮龄 <2 的进场、排除换组后 5 日内的进场,
   候选仍赢基线 —— 如果利润全躲在「翻向当天成本=现价」这种构造性进场里,不算数;
5. **单笔 t**:如实报告,不设硬线(现行基线自己也没过 2),但必须 >0。

全部五关都过才提改引擎;任何一关翻脸,记下来收工。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402

CAND = {"FG": {"need_adding": True}, "SA": {}, "JD": {}}


def tstat(rets):
    a = np.array(rets, dtype=float)
    return a.mean() / a.std(ddof=1) * np.sqrt(len(a)) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan


def sharpe(daily):
    d = daily.fillna(0)
    return float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan


def yearly(daily):
    return {y: float((1 + daily[daily.index.year == y].fillna(0)).prod() - 1) * 100
            for y in sorted({d.year for d in daily.index})}


def run_candidate(sig, mkt, rdf, op, st, groups, unload, kw, **extra):
    H.RULES["long_enabled"] = True
    H.RULES["long_needs_dip"] = False
    z = R.build_entry(sig, mkt, groups, unload, 0.0, 0.3, **kw, **extra)
    orig = H.entry_exit_signals
    H.entry_exit_signals = lambda s, r, _z=z: (_z, r["rz"])
    try:
        tr, _, daily = H.replay(sig, mkt, rdf, op, st)
    finally:
        H.entry_exit_signals = orig
    return [t for t in tr if t["exit_date"]], daily


for code, kw in CAND.items():
    sig, mkt, rdf, op, st, groups, unload = R.load(code)
    # 基线要用**生产配置**回放,所以必须在改 RULES 之前跑
    tr_b, _, day_b = H.replay(sig, mkt, rdf, op, st)
    tr_b = [t for t in tr_b if t["exit_date"]]
    tr_c, day_c = run_candidate(sig, mkt, rdf, op, st, groups, unload, kw)

    print("=" * 96)
    print(f"[{code}] 候选={'成本+卸仓+还在加仓' if kw else '成本+卸仓'}   "
          f"基线 {len(tr_b)} 笔/{sharpe(day_b):.2f}   候选 {len(tr_c)} 笔/{sharpe(day_c):.2f}")
    print("=" * 96)

    # —— 闸门 1:逐年 ——
    yb, yc = yearly(day_b), yearly(day_c)
    wins = sum(1 for y in yb if yc.get(y, 0) > yb[y])
    pos = sum(1 for y in yc if yc[y] > 0)
    print("闸门1 逐年(候选 vs 基线):")
    for y in sorted(yb):
        m = "✓" if yc.get(y, 0) > yb[y] else " "
        print(f"    {y}: 候选 {yc.get(y, float('nan')):>+7.1f}%  基线 {yb[y]:>+7.1f}%  {m}")
    g1 = wins >= len(yb) / 2 and pos >= len(yc) / 2
    print(f"  → 赢 {wins}/{len(yb)} 年,正年份 {pos}/{len(yc)}  [{'过' if g1 else '不过'}]")

    # —— 闸门 2:选臂 walk-forward(只用之前年份的夏普选臂) ——
    years = sorted(yb)
    chain = []
    picks = []
    for j, y in enumerate(years):
        if j == 0:
            arm = "基线"
        else:
            prior = [x for x in years[:j]]
            sb = sharpe(day_b[day_b.index.year.isin(prior)])
            sc = sharpe(day_c[day_c.index.year.isin(prior)])
            arm = "候选" if (np.isfinite(sc) and (not np.isfinite(sb) or sc > sb)) else "基线"
        picks.append(arm[0])
        src = day_c if arm == "候选" else day_b
        chain.append(src[src.index.year == y])
    chain = pd.concat(chain)
    cum = lambda d: float((1 + d.fillna(0)).prod() - 1) * 100  # noqa: E731
    g2 = cum(chain) >= cum(day_b)
    print(f"闸门2 选臂 walk-forward:链 {cum(chain):+.1f}% vs 一直基线 {cum(day_b):+.1f}%"
          f"(逐年选臂 {''.join(picks)})  [{'过' if g2 else '不过'}]")

    # —— 闸门 3:价源换收盘价 ——
    tr_cl, day_cl = run_candidate(sig, mkt, rdf, op, st, groups, unload, kw,
                                  price_col="close")
    g3 = sharpe(day_cl) > sharpe(day_b)
    print(f"闸门3 收盘价源:候选 {cum(day_cl):+.1f}%/{sharpe(day_cl):.2f} vs "
          f"基线 {cum(day_b):+.1f}%/{sharpe(day_b):.2f}  [{'过' if g3 else '不过'}]")

    # —— 闸门 4:排除翻向日与换组余波 ——
    tr_a, day_a = run_candidate(sig, mkt, rdf, op, st, groups, unload, kw, min_age=2)
    tr_g, day_g = run_candidate(sig, mkt, rdf, op, st, groups, unload, kw,
                                skip_group_days=5)
    g4 = sharpe(day_a) > sharpe(day_b) and sharpe(day_g) > sharpe(day_b)
    print(f"闸门4 轮龄≥2:{cum(day_a):+.1f}%/{sharpe(day_a):.2f}({len(tr_a)} 笔);"
          f"避开换组 5 日:{cum(day_g):+.1f}%/{sharpe(day_g):.2f}({len(tr_g)} 笔)"
          f"  [{'过' if g4 else '不过'}]")

    # —— 闸门 5:单笔 t + 机制统计 ——
    tc = tstat([t["ret_pct"] for t in tr_c])
    tb = tstat([t["ret_pct"] for t in tr_b])
    side_s, cost_s, age_s = R.cost_series(sig, mkt, groups)
    px = mkt["settle"]

    def entry_stats(trades):
        ages, adv = [], []
        for t in trades:
            d = pd.Timestamp(t["entry_date"])
            c, a, p = cost_s.get(d, np.nan), age_s.get(d, np.nan), px.get(d, np.nan)
            sd = 1 if t["side"] == "long" else -1
            if np.isfinite(a):
                ages.append(a)
            if np.isfinite(c) and np.isfinite(p) and c > 0:
                adv.append(sd * (c - p) / c * 100)
        med = np.median(ages) if ages else np.nan
        madv = np.median(adv) if adv else np.nan
        return med, madv

    ca, cadv = entry_stats(tr_c)
    ba, badv = entry_stats(tr_b)
    g5 = np.isfinite(tc) and tc > 0
    print(f"闸门5 单笔 t:候选 {tc:+.2f}(基线 {tb:+.2f})  [{'过' if g5 else '不过'}]")
    print(f"  机制:进场时轮龄中位 候选 {ca:.0f} 日 vs 基线 {ba:.0f} 日;"
          f"成本优势中位 候选 {cadv:+.2f}% vs 基线 {badv:+.2f}%")
    n_pass = sum([g1, g2, g3, g4, g5])
    print(f"  ★ [{code}] 五关通过 {n_pass}/5\n")
