"""拐头回撤档扫描 v2:自己定死一把尺,同时量燃油与甲醇(预注册 PLAN_MA_TURN_TIER_v2)。

与 v1 的唯一区别是**记分**,进场判定一字不改(v1 已证明逐行等价于生产)。

- **主口径 = 位置点数**,有界 ±100:门槛 X 本来就活在位置这把尺上
  (`monitor.rs`:「自极值回撤区间的 X%」= 「位置退 X 个百分点」),
  判进场与记分该用同一把尺;而且有界意味着均值能看 —— v1 用当年轨区间宽做分母,
  1% 分位是 0.0,单笔到过 −9700%,均值整个失效。
  **它自己的毛病**:出场日的区间通常比进场日宽,两个位置不在同一刻度上,
  **系统性低估幅度**;对所有档位一视同仁,所以档位比较不受影响,绝对值不能当收益读。
- **副口径 = 价格点数 ÷ 历年轨宽**,只用来验主口径的方向(G5)。
- 两个出场点都报:主 = 窗口止点、副 = 红线日(days_left 首次 ≤15 的前一日)。

用法:
    python research/run_turn_tier_v2.py FU
    python research/run_turn_tier_v2.py MA
"""

import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, "research")
from run_turn_tier import (  # noqa: E402  —— 进场判定原样复用,不再抄第二遍
    CROSS_WIN,
    DEFAULT_TIER,
    RED_LINE,
    TIERS,
    TURN_BAND,
    deadline,
    days_left,
    monitor_turn,
    verify_crosses,
)

DATA = Path("research/data/turn_tier.csv.gz")


def scan(df: pd.DataFrame, x: float) -> pd.DataFrame:
    """按档位 x 重算进场,回每一笔的两个口径 × 两个出场点。"""
    rows = []
    for (c1, c2), g in df.groupby(["contract_1", "contract_2"], sort=False):
        g = g.sort_values("trade_date").reset_index(drop=True)
        pos = g["pair_position"].to_numpy(float)
        prev = g["prev_pair_position"].to_numpy(float)
        hi20 = g["pair_pos_hi20"].to_numpy(float)
        lo20 = g["pair_pos_lo20"].to_numpy(float)
        spread = g["spread"].to_numpy(float)
        ywidth = (g["years_high"] - g["years_low"]).to_numpy(float)
        dates = list(g["trade_date"])

        cross_h = ((prev > 1 - x) & (pos <= 1 - x) & (hi20 >= 1 - TURN_BAND)).astype(int)
        cross_l = ((prev < x) & (pos >= x) & (lo20 <= TURN_BAND)).astype(int)
        cum_h = pd.Series(cross_h).rolling(CROSS_WIN, min_periods=1).sum().to_numpy()
        cum_l = pd.Series(cross_l).rolling(CROSS_WIN, min_periods=1).sum().to_numpy()

        e1, e2 = deadline(c1), deadline(c2)
        if e1 is None or e2 is None:
            continue
        end = min(e1, e2)
        within = [i for i, d in enumerate(dates) if d <= end]
        if not within:
            continue
        last = within[-1]
        # 副出场:红线日 = days_left 首次 ≤15 的前一日(仍在窗口内)。
        red = [i for i in within if days_left(end, dates[i]) > RED_LINE]
        red_exit = red[-1] if red else None

        taken = set()
        for i in range(last):
            side = monitor_turn(pos[i], hi20[i], lo20[i], x)
            if side is None:
                continue
            if side == "high" and not (np.isfinite(prev[i]) and prev[i] > 1 - x):
                continue
            if side == "low" and not (np.isfinite(prev[i]) and prev[i] < x):
                continue
            if (cum_h[i] if side == "high" else cum_l[i]) != 1:
                continue
            if days_left(end, dates[i]) <= RED_LINE:
                continue
            drift = (g["revert_high_drift"] if side == "high" else g["revert_low_drift"]).iloc[i]
            if not (pd.notna(drift) and float(drift) > 0):
                continue

            first = side not in taken
            taken.add(side)
            sign = 1.0 if side == "high" else -1.0        # 位置口径:high 位置跌为盈
            psign = -sign                                  # 价格口径:high 价差跌为盈

            pnl = sign * (pos[i] - pos[last]) * 100
            fwd = slice(i + 1, last + 1)
            # MAE:进场之后朝不利方向走得最远的一次,同为位置点数,下限 0。
            adverse = -sign * (pos[i] - pos[fwd]) * 100
            mae = float(max(adverse.max(), 0.0)) if len(adverse) else 0.0

            yw = ywidth[i]
            alt = (psign * (spread[last] - spread[i]) / yw * 100) \
                if np.isfinite(yw) and yw > 0 else np.nan
            red_pnl = sign * (pos[i] - pos[red_exit]) * 100 \
                if red_exit is not None and red_exit > i else np.nan

            band = pos[fwd]
            early = bool((band >= 1 - TURN_BAND).any()) if side == "high" \
                else bool((band <= TURN_BAND).any())

            rows.append(dict(c1=c1, c2=c2, side=side, entry=dates[i], year=dates[i].year,
                             first=first, pnl=pnl, mae=mae, alt=alt, red=red_pnl,
                             early=early))
    return pd.DataFrame(rows)


def summarise(t: pd.DataFrame) -> dict:
    if t.empty:
        return dict(n=0, **{k: np.nan for k in
                            ("mean", "med", "win", "early", "mae", "early_med", "alt", "red")})
    e = t[t["early"]]
    return dict(
        n=len(t), mean=float(t["pnl"].mean()), med=float(t["pnl"].median()),
        win=float((t["pnl"] > 0).mean() * 100), early=float(t["early"].mean() * 100),
        mae=float(t["mae"].median()),
        early_med=float(e["pnl"].median()) if len(e) else np.nan,
        alt=float(t["alt"].median(skipna=True)), red=float(t["red"].median(skipna=True)))


def main() -> int:
    code = (sys.argv[1] if len(sys.argv) > 1 else "MA").upper()
    raw = pd.read_csv(DATA, parse_dates=["trade_date"])
    raw["trade_date"] = raw["trade_date"].dt.date
    df = raw[(raw["instrument_1"] == code) & (raw["instrument_2"] == code)].copy()
    live = 0.05 if code == "FU" else DEFAULT_TIER

    print("=" * 100)
    print(f"拐头回撤档 v2 · {code} —— {len(df)} 行 / "
          f"{df.groupby(['contract_1', 'contract_2']).ngroups} 个组合 / "
          f"{min(df['trade_date'])} ~ {max(df['trade_date'])}  (线上档 {live:.0%})")

    # ---------------- G0′ 第一格:穿线自证 ----------------
    hi_ok, lo_ok, n = verify_crosses(df, live)
    g0a = (hi_ok == 1.0 and lo_ok == 1.0)
    print(f"\n[G0′-1 穿线自证] 按 {live:.0%} 重算 turn_crosses,对库里存的:"
          f"high {hi_ok:.4%} / low {lo_ok:.4%}({n} 行) → {'过' if g0a else '**不过**'}")

    all_t = {x: scan(df, x) for x in TIERS}
    dedup = {x: t[t["first"]] if not t.empty else t for x, t in all_t.items()}

    # ---------------- G0′ 第二格:有界性 ----------------
    worst = max((float(t["pnl"].abs().max()) for t in dedup.values() if not t.empty),
                default=0.0)
    g0b = worst <= 100.0 + 1e-9
    print(f"[G0′-2 有界自证] 主口径单笔绝对值最大 {worst:.1f} "
          f"(必须 ≤100) → {'过' if g0b else '**不过**'}")
    if not (g0a and g0b):
        print("\n**G0′ 不过,按预注册不报档位。**")
        return 1

    res = {x: summarise(t) for x, t in dedup.items()}
    print("\n[全样本] 逐档(去重:每实例每侧首笔;主口径 = 位置点数,持到窗口止点)")
    print(f"  {'档位':<7}{'笔数':>6}{'不去重':>8}{'均值':>8}{'中位':>8}{'胜率':>7}"
          f"{'早进率':>8}{'MAE中位':>9}{'早进组中位':>11}{'副口径中位':>11}{'红线出场':>10}")
    for x in TIERS:
        s, raw_n = res[x], len(all_t[x])
        if s["n"] == 0:
            print(f"  {x:<7.0%}{0:>6}")
            continue
        print(f"  {x:<7.0%}{s['n']:>6}{raw_n:>8}{s['mean']:>+8.1f}{s['med']:>+8.1f}"
              f"{s['win']:>6.0f}%{s['early']:>7.0f}%{s['mae']:>9.1f}"
              f"{s['early_med']:>+11.1f}{s['alt']:>+11.1f}{s['red']:>+10.1f}")

    meds = [res[x]["med"] for x in TIERS]
    best = max(TIERS, key=lambda x: res[x]["med"] if np.isfinite(res[x]["med"]) else -1e9)
    base = res[DEFAULT_TIER]

    # ---------------- G1 机制 ----------------
    early_meds = [res[x]["early_med"] for x in TIERS]
    mech_a = (np.isfinite(res[best]["early_med"]) and res[best]["early_med"] > 0
              and all(b <= a + 1e-9 for a, b in zip(early_meds, early_meds[1:])))
    jitter = float(df.sort_values("trade_date")
                     .groupby(["contract_1", "contract_2"])["pair_position"]
                     .apply(lambda s: s.diff().abs().median()).median())
    print(f"\n[G1 机制A 早进不受罚] 最优档 {best:.0%} 的早进组中位 "
          f"{res[best]['early_med']:+.1f};逐档早进组中位 "
          f"{['%+.1f' % v for v in early_meds]} → {'成立' if mech_a else '**不成立**'}")
    print(f"[G1 机制B 抖动] 位置日抖动中位 {jitter * 100:.2f}pp,"
          f"10% 线 = {0.10 / max(jitter, 1e-9):.1f} 倍日抖动(全场排名见报告)")

    # ---------------- G2 单调 ----------------
    diffs = [b - a for a, b in zip(meds, meds[1:]) if np.isfinite(a) and np.isfinite(b)]
    g2 = all(d <= 1e-9 for d in diffs) or all(d >= -1e-9 for d in diffs)
    print(f"\n[G2 全样本单调] 七档中位 {['%+.1f' % m for m in meds]} → "
          f"{'单调,过' if g2 else '**有翻转,不过**'}")

    # ---------------- G3 逐年留一 ----------------
    years = sorted({y for t in dedup.values() if not t.empty for y in t["year"]})
    oos, fixed, picks = [], [], {}
    print("\n[G3 逐年留一] 用其余年份挑档,应用到留出的那一年")
    for y in years:
        m = {x: t[t["year"] != y]["pnl"].median()
             for x, t in dedup.items() if not t[t["year"] != y].empty}
        if not m:
            continue
        pick = max(m, key=m.get)
        picks[y] = pick
        held = dedup[pick][dedup[pick]["year"] == y]["pnl"]
        b = dedup[DEFAULT_TIER][dedup[DEFAULT_TIER]["year"] == y]["pnl"]
        oos.extend(held.tolist())
        fixed.extend(b.tolist())
        if len(held):
            print(f"  {y}  挑中 {pick:<5.0%} 留出年中位 {held.median():+7.1f}"
                  f"  (固定 10% 同年 {b.median() if len(b) else float('nan'):+7.1f}, n={len(b)})")
    o = float(np.median(oos)) if oos else float("nan")
    f = float(np.median(fixed)) if fixed else float("nan")
    g3 = np.isfinite(o) and np.isfinite(f) and (o - f) >= 3.0
    print(f"\n  OOS 中位 {o:+.1f}(n={len(oos)}) vs 固定 10% {f:+.1f}(n={len(fixed)})"
          f"  差 {o - f:+.1f} 点 → {'过' if g3 else '**不过**(门槛 +3.0)'}")

    # ---------------- 逐年一致性 ----------------
    by_year = {}
    for y in years:
        m = {x: t[t["year"] == y]["pnl"].median()
             for x, t in dedup.items() if not t[t["year"] == y].empty}
        if m:
            by_year[y] = max(m, key=m.get)
    cnt = Counter(by_year.values())
    if cnt:
        top, k = cnt.most_common(1)[0]
        print(f"\n[逐年一致性] 最常当最优的是 {top:.0%},{k}/{len(by_year)} 年;"
              f"分布 {{{', '.join(f'{x:.0%}:{c}' for x, c in sorted(cnt.items()))}}}")

    # ---------------- G4 方向一致 ----------------
    g4 = all(np.isfinite(res[best][k]) and np.isfinite(base[k])
             and (res[best][k] - base[k]) >= 0 for k in ("mean", "med", "win")) \
        or best == DEFAULT_TIER
    print(f"\n[G4 方向一致] 最优 {best:.0%} vs 10%:均值 "
          f"{res[best]['mean'] - base['mean']:+.1f}、中位 {res[best]['med'] - base['med']:+.1f}、"
          f"胜率 {res[best]['win'] - base['win']:+.0f}pp → {'过' if g4 else '**不过**'}")

    # ---------------- G5 口径稳健 ----------------
    best_alt = max(TIERS, key=lambda x: res[x]["alt"] if np.isfinite(res[x]["alt"]) else -1e9)
    same_sign = (np.sign(res[best]["med"] - base["med"])
                 == np.sign(res[best]["alt"] - base["alt"]))
    g5 = (best_alt == best) or same_sign or best == DEFAULT_TIER
    print(f"[G5 口径稳健] 副口径最优档 {best_alt:.0%}(主口径 {best:.0%});"
          f"最优档相对 10% 的改进:主 {res[best]['med'] - base['med']:+.1f}、"
          f"副 {res[best]['alt'] - base['alt']:+.1f} → {'过' if g5 else '**不过**'}")

    verdict = "维持 10%(默认结局)" if best == DEFAULT_TIER or not all((mech_a, g2, g3, g4, g5)) \
        else f"建议改 {best:.0%}"
    print(f"\n{'=' * 100}\n**{code} 结论:{verdict}**   "
          f"(G1机制A {'✓' if mech_a else '✗'} / G2 {'✓' if g2 else '✗'} / "
          f"G3 {'✓' if g3 else '✗'} / G4 {'✓' if g4 else '✗'} / G5 {'✓' if g5 else '✗'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
