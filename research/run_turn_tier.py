"""拐头回撤档扫描:某品种的 X 该定几 %(预注册 PLAN_MA_TURN_TIER_v1)。

**为什么进仓库**:DEC-070 的脚本在 `qh:/tmp/arb2`、DEC-252(燃油定 5%)那次的
脚本也已经找不到了 —— 两次定档都无法复现。这份写成按品种传参,下个品种直接复用。

判定**照抄生产**:采集 SQL `compute-spread-monitor.sql`(turn_crosses)与
API `monitor.rs`(monitor_turn / monitor_turn_is_new / days_to_window_end)、
前端 `revert.ts` + `SpreadMonitorView.isQualifiedEntry`(合格门与红线)。
`turn_crosses` 依赖档位必须按 X 重算;`revert_*_drift` 与档位无关,直接用库里的。

用法:
    python research/run_turn_tier.py FU      # G0 校准:对 DEC-252 公布的表
    python research/run_turn_tier.py MA      # 正题
"""

import sys
from datetime import date, timedelta

if hasattr(sys.stdout, "reconfigure"):      # Windows 控制台默认 GBK,勾号会炸
    sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("research/data/turn_tier.csv.gz")
TURN_BAND = 0.03            # 与 monitor.rs 的 TURN_BAND、采集 SQL 的 0.97/0.03 同值
RED_LINE = 15               # isRedLine:days_left <= 15 不进场
CROSS_WIN = 20              # 近 20 个交易日的穿线计数
TIERS = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25]
DEFAULT_TIER = 0.10

# DEC-252 公布的燃油表,G0 校准用(笔数差 ≤2、中位差 ≤1.0 点、单调同序)。
FU_PUBLISHED = {
    0.05: dict(n=135, mean=20.8, med=17.5, win=59, early=88, mae=14.7),
    0.08: dict(n=132, mean=16.7, med=6.5, win=55, early=86, mae=17.7),
    0.10: dict(n=132, mean=15.5, med=6.5, win=55, early=86, mae=19.5),
    0.20: dict(n=125, mean=9.7, med=-2.4, win=50, early=84, mae=30.2),
    0.25: dict(n=123, mean=5.6, med=-7.1, win=45, early=81, mae=33.3),
}


# ------------------------------------------------------------------ 生产口径
def deadline(code: str):
    """照抄 monitor.rs::days_to_window_end 的 deadline():交割月前月最后一个非周末日。"""
    digits = next((i for i, c in enumerate(code) if c.isdigit()), None)
    raw = code[digits:]
    if len(raw) != 4:
        return None
    year, month = 2000 + int(raw[:2]), int(raw[2:])
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    d = date(py + (pm == 12 and 0), pm, 28)
    while True:
        nxt = d + timedelta(days=1)
        if nxt.month != pm:
            break
        d = nxt
    while d.weekday() >= 5:          # 5=周六 6=周日
        d -= timedelta(days=1)
    return d


def days_left(end: date, today: date) -> int:
    """照抄:数 (today, end] 之间的非周末日。**是工作日不是真交易日**,照抄不修。"""
    if end <= today:
        return 0
    n, cur = 0, today + timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def monitor_turn(pos, hi20, lo20, x):
    """照抄 monitor.rs::monitor_turn。两侧同时成立取离自家门槛更近的一侧(DEC-088)。"""
    if not np.isfinite(pos):
        return None
    high = np.isfinite(hi20) and hi20 >= 1.0 - TURN_BAND and pos <= 1.0 - x
    low = np.isfinite(lo20) and lo20 <= TURN_BAND and pos >= x
    if high and not low:
        return "high"
    if low and not high:
        return "low"
    if high and low:
        return "high" if (1.0 - x - pos) <= (pos - x) else "low"
    return None


def verify_crosses(df: pd.DataFrame, x: float):
    """把重算的穿线计数对库里存的 `turn_crosses_*`。

    **这是本脚本唯一有权威参照物的一格**:生产自己把 turn_crosses 存了下来,
    而它正是按该品种当前档位算的(FU 5%、MA 10%)。对上就证明拐头/刚拐头/
    首穿这三层我抄对了;对不上就别往下走。
    """
    same_h = same_l = total = 0
    for _, g in df.groupby(["contract_1", "contract_2"], sort=False):
        g = g.sort_values("trade_date")
        pos = g["pair_position"].to_numpy(float)
        prev = g["prev_pair_position"].to_numpy(float)
        hi20 = g["pair_pos_hi20"].to_numpy(float)
        lo20 = g["pair_pos_lo20"].to_numpy(float)
        ch = ((prev > 1 - x) & (pos <= 1 - x) & (hi20 >= 1 - TURN_BAND)).astype(int)
        cl = ((prev < x) & (pos >= x) & (lo20 <= TURN_BAND)).astype(int)
        mh = pd.Series(ch).rolling(CROSS_WIN, min_periods=1).sum().to_numpy()
        ml = pd.Series(cl).rolling(CROSS_WIN, min_periods=1).sum().to_numpy()
        same_h += int((mh == g["turn_crosses_high_20"].to_numpy()).sum())
        same_l += int((ml == g["turn_crosses_low_20"].to_numpy()).sum())
        total += len(g)
    return same_h / total, same_l / total, total


# ------------------------------------------------------------------ 一档扫描
def scan(df: pd.DataFrame, x: float) -> pd.DataFrame:
    """按档位 x 重算进场,回每一笔的结局。每实例每侧只取首笔(DEC-070 口径)。"""
    trades = []
    for (c1, c2), g in df.groupby(["contract_1", "contract_2"], sort=False):
        g = g.sort_values("trade_date").reset_index(drop=True)
        pos = g["pair_position"].to_numpy(float)
        prev = g["prev_pair_position"].to_numpy(float)
        hi20 = g["pair_pos_hi20"].to_numpy(float)
        lo20 = g["pair_pos_lo20"].to_numpy(float)
        spread = g["spread"].to_numpy(float)
        width = (g["pair_high"] - g["pair_low"]).to_numpy(float)
        dates = list(g["trade_date"])

        # 照抄采集 SQL 的 cross_h / cross_l,再按 20 行开窗求和。
        cross_h = ((prev > 1 - x) & (pos <= 1 - x) & (hi20 >= 1 - TURN_BAND)).astype(int)
        cross_l = ((prev < x) & (pos >= x) & (lo20 <= TURN_BAND)).astype(int)
        cum_h = pd.Series(cross_h).rolling(CROSS_WIN, min_periods=1).sum().to_numpy()
        cum_l = pd.Series(cross_l).rolling(CROSS_WIN, min_periods=1).sum().to_numpy()

        end = deadline(c1)
        e2 = deadline(c2)
        if end is None or e2 is None:
            continue
        end = min(end, e2)
        # 出场 = 窗口止点之内的最后一行(= 采集 SQL hist_fwd 的 flast)。
        within = [i for i, d in enumerate(dates) if d <= end]
        if not within:
            continue
        last = within[-1]

        taken = set()
        for i in range(len(g)):
            if i >= last:                       # 进场日必须在出场之前
                continue
            side = monitor_turn(pos[i], hi20[i], lo20[i], x)
            if side is None or side in taken:
                continue
            # 今天刚拐头
            if side == "high" and not (np.isfinite(prev[i]) and prev[i] > 1 - x):
                continue
            if side == "low" and not (np.isfinite(prev[i]) and prev[i] < x):
                continue
            # 本轮首次穿线
            if (cum_h[i] if side == "high" else cum_l[i]) != 1:
                continue
            # 未进交割红线
            if days_left(end, dates[i]) <= RED_LINE:
                continue
            # 资格:拐头侧的 drift > 0(与档位无关,用库里的值)
            drift = g["revert_high_drift"].iloc[i] if side == "high" else g["revert_low_drift"].iloc[i]
            if not (pd.notna(drift) and float(drift) > 0):
                continue

            w = width[i]
            if not np.isfinite(w) or w <= 0:
                continue
            sign = -1.0 if side == "high" else 1.0
            fwd = slice(i + 1, last + 1)
            pnl = sign * (spread[last] - spread[i]) / w * 100
            adverse = -sign * (spread[fwd] - spread[i]) / w * 100
            mae = float(max(adverse.max(), 0.0)) if len(adverse) else 0.0
            band = pos[fwd]
            early = bool((band >= 1 - TURN_BAND).any()) if side == "high" \
                else bool((band <= TURN_BAND).any())

            taken.add(side)
            trades.append(dict(c1=c1, c2=c2, side=side, entry=dates[i],
                               year=dates[i].year, pnl=pnl, mae=mae, early=early))
    return pd.DataFrame(trades)


def summarise(t: pd.DataFrame) -> dict:
    if t.empty:
        return dict(n=0, mean=np.nan, med=np.nan, win=np.nan, early=np.nan, mae=np.nan,
                    early_med=np.nan)
    e = t[t["early"]]
    return dict(
        n=len(t), mean=float(t["pnl"].mean()), med=float(t["pnl"].median()),
        win=float((t["pnl"] > 0).mean() * 100), early=float(t["early"].mean() * 100),
        mae=float(t["mae"].median()),
        early_med=float(e["pnl"].median()) if len(e) else np.nan)


def table(res: dict, title: str):
    print(f"\n{title}")
    print(f"  {'档位':<8}{'笔数':>6}{'均值':>9}{'中位':>9}{'胜率':>8}{'早进率':>8}"
          f"{'中位MAE':>9}{'早进组中位':>11}")
    for x in TIERS:
        s = res[x]
        if s["n"] == 0:
            print(f"  {x:<8.0%}{0:>6}   —")
            continue
        print(f"  {x:<8.0%}{s['n']:>6}{s['mean']:>+9.1f}{s['med']:>+9.1f}"
              f"{s['win']:>7.0f}%{s['early']:>7.0f}%{s['mae']:>9.1f}"
              f"{s['early_med']:>+11.1f}")


def main() -> int:
    code = (sys.argv[1] if len(sys.argv) > 1 else "MA").upper()
    raw = pd.read_csv(DATA, parse_dates=["trade_date"])
    raw["trade_date"] = raw["trade_date"].dt.date
    df = raw[(raw["instrument_1"] == code) & (raw["instrument_2"] == code)].copy()
    print("=" * 92)
    print(f"拐头回撤档扫描 · {code}  —— {len(df)} 行 / "
          f"{df.groupby(['contract_1', 'contract_2']).ngroups} 个组合 / "
          f"{min(df['trade_date'])} ~ {max(df['trade_date'])}")

    live = 0.05 if code == "FU" else DEFAULT_TIER
    hi_ok, lo_ok, n = verify_crosses(df, live)
    print(f"\n[口径自证] 按该品种线上档位 {live:.0%} 重算穿线计数,对库里存的 "
          f"turn_crosses:high {hi_ok:.4%} / low {lo_ok:.4%} 一致({n} 行)")
    if min(hi_ok, lo_ok) < 1.0:
        print("  ⚠ 没有 100%,说明拐头三层里有一层抄错了,下面的数不要信")

    per_tier = {x: scan(df, x) for x in TIERS}
    res = {x: summarise(t) for x, t in per_tier.items()}
    table(res, "[全样本] 逐档")

    # ---- G0:燃油对 DEC-252 公布表 ----
    if code == "FU":
        print("\n[G0 校准] 对 DEC-252 公布的表(笔数差 ≤2、中位差 ≤1.0 点、单调同序)")
        print(f"  {'档位':<8}{'公布n':>7}{'本次n':>7}{'Δn':>5}"
              f"{'公布中位':>10}{'本次中位':>10}{'Δ中位':>9}  判定")
        ok = True
        for x, pub in FU_PUBLISHED.items():
            s = res[x]
            dn, dm = s["n"] - pub["n"], s["med"] - pub["med"]
            good = abs(dn) <= 2 and abs(dm) <= 1.0
            ok &= good
            print(f"  {x:<8.0%}{pub['n']:>7}{s['n']:>7}{dn:>+5}"
                  f"{pub['med']:>+10.1f}{s['med']:>+10.1f}{dm:>+9.1f}  "
                  f"{'✓' if good else '✗'}")
        order_pub = [FU_PUBLISHED[x]["med"] for x in FU_PUBLISHED]
        order_mine = [res[x]["med"] for x in FU_PUBLISHED]
        same = all((a > b) == (c > d) for a, b, c, d in
                   zip(order_pub, order_pub[1:], order_mine, order_mine[1:]))
        print(f"  单调同序:{'✓' if same else '✗'}")
        print(f"\n  **G0 {'通过' if ok and same else '不通过'}**")

    # ---- 逐年 ----
    print("\n[逐年] 每年的最优档(中位幅度最高者)与该年 10% 的中位")
    years = sorted({y for t in per_tier.values() if not t.empty for y in t["year"]})
    best_by_year = {}
    for y in years:
        meds = {x: (t[t["year"] == y]["pnl"].median() if not t.empty and (t["year"] == y).any()
                    else np.nan) for x, t in per_tier.items()}
        valid = {x: v for x, v in meds.items() if np.isfinite(v)}
        if not valid:
            continue
        b = max(valid, key=valid.get)
        best_by_year[y] = b
        n_y = int((per_tier[DEFAULT_TIER]["year"] == y).sum()) if not per_tier[DEFAULT_TIER].empty else 0
        print(f"  {y}  最优 {b:.0%}(中位 {valid[b]:+6.1f})   "
              f"10% 中位 {meds.get(DEFAULT_TIER, float('nan')):+6.1f}   n(10%)={n_y}")

    # ---- G3:逐年留一挑参 OOS ----
    print("\n[G3 逐年留一] 用其余年份挑最优档,应用到留出的那一年")
    oos, fixed = [], []
    for y in years:
        meds = {}
        for x, t in per_tier.items():
            other = t[t["year"] != y]
            if not other.empty:
                meds[x] = other["pnl"].median()
        if not meds:
            continue
        pick = max(meds, key=meds.get)
        held = per_tier[pick]
        held = held[held["year"] == y]["pnl"]
        base = per_tier[DEFAULT_TIER]
        base = base[base["year"] == y]["pnl"]
        if len(held):
            oos.extend(held.tolist())
        if len(base):
            fixed.extend(base.tolist())
        if len(held):
            print(f"  {y}  挑中 {pick:.0%}  留出年中位 {held.median():+6.1f}  "
                  f"(固定 10% 同年 {base.median() if len(base) else float('nan'):+6.1f})")
    o = float(np.median(oos)) if oos else float("nan")
    f = float(np.median(fixed)) if fixed else float("nan")
    print(f"\n  OOS 中位 {o:+.1f}(n={len(oos)})  vs 固定 10% {f:+.1f}(n={len(fixed)})"
          f"  差 {o - f:+.1f} 点   G3 门槛 ≥ +3.0 → {'过' if o - f >= 3.0 else '**不过**'}")

    # ---- 逐年一致性 ----
    if best_by_year:
        from collections import Counter
        cnt = Counter(best_by_year.values())
        top, k = cnt.most_common(1)[0]
        print(f"\n[逐年一致性] 最常当最优的是 {top:.0%},{k}/{len(best_by_year)} 年;"
              f"分布 {{{', '.join(f'{x:.0%}:{c}' for x, c in sorted(cnt.items()))}}}")

    # ---- G2 单调 ----
    meds = [res[x]["med"] for x in TIERS]
    diffs = [b - a for a, b in zip(meds, meds[1:]) if np.isfinite(a) and np.isfinite(b)]
    mono = all(d <= 0 for d in diffs) or all(d >= 0 for d in diffs)
    print(f"\n[G2 全样本单调] 七档中位 {['%+.1f' % m for m in meds]} → "
          f"{'单调,过' if mono else '**有翻转,不过**'}")

    # ---- 位置日抖动(G1 机制 B 的素材) ----
    jitter = (df.sort_values("trade_date")
                .groupby(["contract_1", "contract_2"])["pair_position"]
                .apply(lambda s: s.diff().abs().median()))
    print(f"[G1 机制B 素材] 位置日抖动中位 {float(jitter.median()) * 100:.2f}pp "
          f"(全品种对照见报告;10% 线 = {0.10 / max(float(jitter.median()), 1e-9):.1f} 倍日抖动)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
