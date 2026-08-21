"""纯碱:把「机构出货程度」这条线索走完整流程。

`REPORT_INST_UNLOAD_v1.md` 里五个品种只有纯碱过了四道闸门。运营者要求单独深挖它。

**先做的不是显著性检验,是一个混淆项** —— 它要是成立,后面的置换检验只会证明
一个"真实但无意义"的模式:

  **出货程度可能measure的是「掉榜」而不是「出货」。**

`group_book` 计算合计净持仓时,某家掉榜就跳过它。于是五家里掉了两家,合计净持仓
就下降,我的指标读成"机构在出货" —— 而人家可能一手没动,只是掉出前二十了。
这正是 `research/PITFALLS.md` 第 4 条(掉榜≠清仓)落在派生指标上的样子。

判据:**只看「与峰值日在榜家数相同」的那些天**,效应还在不在。
在榜家数一变就不比 —— 那种下降分不清是出货还是掉榜。

过了这一关再做:
  · 逐年符号(docs/PITFALLS:玻璃全样本 t=+2.96,拆到逐年 7 正 7 负);
  · 非重叠样本(每 h 天取一个,消掉前瞻窗口的重叠);
  · 置换检验 —— 按 `research/PITFALLS.md` 第 5 条的做法,**随机平移席位序列**
    (不是打乱),保留两边各自的自相关只破坏对齐。纯随机世界里同一套机器能造出
    +4 个百分点/笔的假优势,不跟这个零分布比就不知道自己在第几百分位。
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from run_inst_cost import group_book  # noqa: E402

CODE = "SA"
HORIZONS = [5, 10, 20]
RNG = np.random.default_rng(20260821)


def retrace_with_peak(net: pd.Series, legs: pd.Series) -> pd.DataFrame:
    """回落比例,同时带出**峰值那天在榜几家** —— 用来分辨出货与掉榜。

    掉榜(net 为 NaN)时冻结这一轮,当天不给值;真归零或翻向才重开。
    """
    n_arr, l_arr = net.to_numpy(), legs.to_numpy()
    out = np.full(len(net), np.nan)
    peak_legs = np.full(len(net), np.nan)
    peak = np.nan
    peak_l = np.nan
    cur = 0
    for i, n in enumerate(n_arr):
        if not np.isfinite(n):
            continue                       # 不知道就是不知道,不重置也不给值
        if n == 0:
            peak, peak_l, cur = np.nan, np.nan, 0
            continue
        s = int(np.sign(n))
        if s != cur:
            cur, peak, peak_l = s, abs(n), l_arr[i]
        elif abs(n) > peak:
            peak, peak_l = abs(n), l_arr[i]
        out[i] = 1.0 - abs(n) / peak if peak > 0 else np.nan
        peak_legs[i] = peak_l
    return pd.DataFrame({"u": out, "peak_legs": peak_legs}, index=net.index)


def spread(x: pd.Series, r: pd.Series, k: int = 4) -> float:
    """末档均值 − 首档均值(百分点)。分不出档给 nan。"""
    df = pd.DataFrame({"x": x, "r": r}).dropna()
    if len(df) < 120:
        return np.nan
    try:
        codes = pd.qcut(df["x"], k, labels=False, duplicates="drop")
    except ValueError:
        return np.nan
    if pd.Series(codes).nunique() < 3:
        return np.nan
    g = df.groupby(codes, observed=True)["r"].mean()
    return float((g.iloc[-1] - g.iloc[0]) * 100)


def main() -> None:
    book = group_book(CODE)
    px = book["settle"]
    side = np.sign(book["net"])
    rp = retrace_with_peak(book["net"], book["n"])
    u, peak_legs = rp["u"], rp["peak_legs"]
    same_legs = book["n"] == peak_legs

    base = np.isfinite(u) & np.isfinite(side) & (side != 0)
    print(f"纯碱 SA   可判定 {int(base.sum()):,} 天\n")

    # ---- ① 掉榜混淆 ----
    print("=" * 92)
    print("① 掉榜混淆:出货程度measure的是出货,还是「掉出前二十」?")
    print("=" * 92)
    d_legs = (peak_legs - book["n"])[base]
    print(f"  峰值日在榜家数 − 当日在榜家数:  =0 的天占 {(d_legs == 0).mean():.0%};"
          f"  ≥1 的天占 {(d_legs >= 1).mean():.0%}")
    print(f"  出货程度与「掉了几家」的相关系数 {u[base].corr(d_legs):+.2f}"
          f"   ← 越高说明它越像在量掉榜")
    print("\n  末−首(百分点)。**同家数**那一列才是干净的:")
    print(f"    {'窗口':>6s} {'全部天':>10s} {'仅同家数':>10s} {'仅掉过家':>10s}")
    for h in HORIZONS:
        fwd = px.shift(-h) / px - 1
        r = side * fwd
        a = spread(u[base], r[base])
        b = spread(u[base & same_legs], r[base & same_legs])
        c = spread(u[base & ~same_legs], r[base & ~same_legs])
        print(f"    {h:>4} 日 {a:>+9.2f}% {b:>+9.2f}% {c:>+9.2f}%")
    n_same = int((base & same_legs).sum())
    print(f"  (同家数的天共 {n_same:,})")
    if n_same < 300:
        print("\n  同家数样本不足 300,后面几关不做了 —— 这条线索在纯碱上到此为止。")
        return

    ok = base & same_legs

    # ---- ② 逐年符号 ----
    print("\n" + "=" * 92)
    print("② 逐年符号(全样本显著不等于稳定;玻璃曾全样本 t=+2.96 而逐年 7 正 7 负)")
    print("=" * 92)
    # **三个窗口都要列。** 只列一个窗口的逐年,而通过置换检验的是另一个窗口,
    # 判定就不完整 —— 第一版只算了 10 日,而 5 日才是过关的那个。
    years = sorted({d.year for d in book.index[ok]})
    print(f"  {chr(24180):>6s}" + "".join(f"{h:>4} 日" for h in HORIZONS) + "   样本")
    tally = {h: [0, 0] for h in HORIZONS}
    for y in years:
        m = ok & (book.index.year == y)
        if m.sum() < 60:
            print(f"  {y}:  样本 {int(m.sum())},太薄不计")
            continue
        cells = ""
        for h in HORIZONS:
            sv = spread(u[m], (side * (px.shift(-h) / px - 1))[m])
            cells += f"{sv:>+7.2f}%" if np.isfinite(sv) else "     ——"
            if np.isfinite(sv):
                tally[h][1] += 1
                if sv < 0:
                    tally[h][0] += 1
        print(f"  {y}: {cells}   {int(m.sum())}")
    print("  → 为负的年数(负=符合假说): "
          + "  ".join(f"{h} 日 {tally[h][0]}/{tally[h][1]}" for h in HORIZONS))

    # ---- ③ 非重叠样本 ----
    print("\n" + "=" * 92)
    print("③ 非重叠样本:每 h 天取一个,消掉前瞻窗口造成的重叠")
    print("=" * 92)
    for h in HORIZONS:
        fwd = px.shift(-h) / px - 1
        r = side * fwd
        vals = []
        for off in range(h):
            m = ok.copy()
            m.iloc[:] = False
            m.iloc[off::h] = True
            m = m & ok
            s = spread(u[m], r[m])
            if np.isfinite(s):
                vals.append(s)
        if vals:
            print(f"    {h:>2} 日  {h} 个错开起点的末−首:"
                  f" 均值 {np.mean(vals):+.2f}%  范围 [{min(vals):+.2f}%, {max(vals):+.2f}%]"
                  f"  为负 {sum(v < 0 for v in vals)}/{len(vals)}")
        else:
            print(f"    {h:>2} 日  非重叠后每组样本不足")

    # ---- ④ 置换检验 ----
    print("\n" + "=" * 92)
    print("④ 置换检验:随机平移席位序列 500 次(保留各自自相关,只破坏对齐)")
    print("=" * 92)
    print("  按 research/PITFALLS 第 5 条:纯随机世界里同一套机器也能造出假优势,")
    print("  不跟这个零分布比,就不知道真值在第几百分位。")
    net, legs, n = book["net"], book["n"], len(net_len := book["net"])
    del net_len
    # **三个窗口都要跑。** 只跑一个再挑最好的那个,就是在零分布上做选择性报告;
    # 而且 5 日才是唯一扛住前两关的窗口,只测 10 日对它不公平。
    # 代价:测三次,阈值要相应收紧(5%/3 ≈ 1.7%)。
    shifts = [int(RNG.integers(60, n - 60)) for _ in range(500)]
    rolled = []
    for k in shifts:
        sh_net = pd.Series(np.roll(net.to_numpy(), k), index=net.index)
        sh_legs = pd.Series(np.roll(legs.to_numpy(), k), index=legs.index)
        rp2 = retrace_with_peak(sh_net, sh_legs)
        s2 = np.sign(sh_net)
        m = (np.isfinite(rp2["u"]) & np.isfinite(s2) & (s2 != 0)
             & (sh_legs == rp2["peak_legs"]))
        rolled.append((rp2["u"], s2, m))
    for h in HORIZONS:
        fwdv = px.shift(-h) / px - 1
        real = spread(u[ok], (side * fwdv)[ok])
        null = np.array([v for uu, s2, m in rolled
                         if np.isfinite(v := spread(uu[m], (s2 * fwdv)[m]))])
        if not len(null) or not np.isfinite(real):
            print(f"    {h:>2} 日  算不出")
            continue
        pct = float((null <= real).mean() * 100)
        print(f"    {h:>2} 日  真实 {real:+.2f}%  |  零分布均值 {null.mean():+.2f}% "
              f"标准差 {null.std():.2f}  5% 分位 {np.percentile(null, 5):+.2f}%"
              f"  |  **第 {pct:.1f} 百分位**")

    print("\n" + "=" * 92)
    print("判定线(事先写死):同家数那列符号不变且量级不塌 → 不是掉榜伪装;")
    print("逐年多数为负;非重叠后均值仍为负;置换百分位 < 5。四条都过才算这条线索成立。")


if __name__ == "__main__":
    main()
