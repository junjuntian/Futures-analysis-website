"""套利监控「⚡反弹进场」档位的留一法回放(运营者 2026-08-23 选项 2,预注册)。

## 起因
LH2611−LH2705 2026-07-27 新拐头(0.000→0.285,本轮首次穿线),历年 5/5 回归、择时可吃
+485~945 点,但**持到期 −745**,按现行「合格=持到期>0」被压住,没亮 ⚡;实际随后
+540 点反弹。现行合格门天然排除「回归率高、拿到期亏」的反弹型模板。

## 数据与口径(与生产同一张表、同一套数:`spread_monitor_daily` 全历史 2013~2026)
每行已有当年轨位置/前一日位置/20 日极值位置/穿线次数/**历年同模板其他年份**的统计
(hit/n/move_med/drift_med/mae_med/mae_max)—— 排除了自身那一年,即 DEC-063 的留一法。
拐头/新拐头/首次穿线/红线按 monitor.rs 同样的常量复刻:
  TURN_BAND 0.03;回撤量 JM 0.20 / JD 0.05 / FG-SA 0.08 / 其余 0.10;红线 剩余 ≤15 日。

## 事先写死的规格
进场事件 = 今天刚拐头 × 本轮首次穿线 × 未进红线(与 ⚡ 的前三条相同),按拐头侧方向,
**次日价差**成交。分三组:
  Q 合格:drift_med > 0(现行 ⚡ 门);
  B 反弹候选:drift_med ≤ 0 且 hit/n ≥ 0.8 且 move_med > 0 且 n ≥ 3;
  R 其余。
出场两套:
  持到期:持到窗口止点(复现 DEC-063 的「合格段 +29% / 不合格 −26%」是对本回放的校准);
  择时:目标 = +0.5×move_med;止损 = −mae_max;20 个交易日;窗口止点 —— 先到为准。
成绩按「占当年轨区间宽度的 %」报(与 DEC-063 同单位),同时给点数。
判据:B 组在择时口径下中位 > 0、t > 2、胜率 ≥ 55%,且不劣于 Q 组择时 —— 才提新档位。
"""
import pathlib

import numpy as np
import pandas as pd

DATA = pathlib.Path(__file__).resolve().parent / "data"
TURN_BAND = 0.03
RED = 15
HORIZON = 20


def retreat(i1, i2):
    if i1 != i2:
        return 0.08 if (i1, i2) == ("FG", "SA") else 0.10
    return {"JM": 0.20, "JD": 0.05}.get(i1, 0.10)


def yymm(code):
    raw = "".join(ch for ch in code if ch.isdigit())
    return 2000 + int(raw[:2]), int(raw[2:])


def deadline(c1, c2):
    """先到期腿:交割月前月最后一个非周末日(与 window_end 同口径)。"""
    y, m = min(yymm(c1), yymm(c2))
    d = pd.Timestamp(year=y, month=m, day=1) - pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d


def tstat(a):
    a = np.array(a, dtype=float)
    a = a[np.isfinite(a)]
    return a.mean() / a.std(ddof=1) * np.sqrt(len(a)) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan


df = pd.read_csv(DATA / "spread_monitor_daily.csv.gz", parse_dates=["trade_date"])
df = df.sort_values(["contract_1", "contract_2", "trade_date"]).reset_index(drop=True)
events = []
for (c1, c2), g in df.groupby(["contract_1", "contract_2"], sort=False):
    g = g.reset_index(drop=True)
    i1, i2 = g["instrument_1"].iloc[0], g["instrument_2"].iloc[0]
    rt = retreat(i1, i2)
    dl = deadline(c1, c2)
    spread = g["spread"].astype(float).values
    dates = g["trade_date"].values
    n = len(g)
    for i in range(n - 1):
        pos, prev = g["pair_position"].iloc[i], g["prev_pair_position"].iloc[i]
        hi20, lo20 = g["pair_pos_hi20"].iloc[i], g["pair_pos_lo20"].iloc[i]
        if not np.isfinite(pos) or not np.isfinite(prev):
            continue
        high = np.isfinite(hi20) and hi20 >= 1 - TURN_BAND and pos <= 1 - rt
        low = np.isfinite(lo20) and lo20 <= TURN_BAND and pos >= rt
        if high and low:
            side = "high" if (1 - rt - pos) <= (pos - rt) else "low"
        elif high:
            side = "high"
        elif low:
            side = "low"
        else:
            continue
        is_new = prev > 1 - rt if side == "high" else prev < rt
        if not is_new:
            continue
        crosses = g[f"turn_crosses_{side}_20"].iloc[i]
        if not (np.isfinite(crosses) and int(crosses) == 1):
            continue
        d = pd.Timestamp(dates[i])
        days_left = int(np.busday_count((d + pd.Timedelta(days=1)).date(), (dl + pd.Timedelta(days=1)).date())) if dl > d else 0
        if days_left <= RED:
            continue
        hit, nn = g[f"revert_{side}_hit"].iloc[i], g[f"revert_{side}_n"].iloc[i]
        move, drift = g[f"revert_{side}_move"].iloc[i], g[f"revert_{side}_drift"].iloc[i]
        mae_max = g[f"revert_{side}_mae_max"].iloc[i]
        if not (np.isfinite(nn) and nn > 0 and np.isfinite(drift)):
            continue
        width = float(g["pair_high"].iloc[i] - g["pair_low"].iloc[i])
        if not np.isfinite(width) or width <= 0:
            continue
        dirn = 1.0 if side == "low" else -1.0
        entry = spread[i + 1]
        if not np.isfinite(entry):
            continue
        # 持到期:窗口止点前最后一行
        j_end = i + 1
        for j in range(i + 1, n):
            if pd.Timestamp(dates[j]) <= dl:
                j_end = j
            else:
                break
        hold = dirn * (spread[j_end] - entry)
        # 择时
        target = 0.5 * move if np.isfinite(move) and move > 0 else np.inf
        stop = mae_max if np.isfinite(mae_max) and mae_max > 0 else np.inf
        exit_px, reason = spread[j_end], "窗口"
        mfe = -np.inf
        for j in range(i + 2, j_end + 1):
            pnl = dirn * (spread[j] - entry)
            mfe = max(mfe, pnl)
            if pnl >= target:
                exit_px, reason = spread[j], "目标"
                break
            if pnl <= -stop:
                exit_px, reason = spread[j], "止损"
                break
            if j - (i + 1) >= HORIZON:
                exit_px, reason = spread[j], "20日"
                break
        timed = dirn * (exit_px - entry)
        grp = ("Q" if drift > 0 else
               "B" if (hit / nn >= 0.8 and np.isfinite(move) and move > 0 and nn >= 3) else "R")
        events.append(dict(c1=c1, c2=c2, inst=i1, date=d, side=side, grp=grp, n=int(nn), hit=int(hit),
                           move=move, drift=drift, mae_max=mae_max, days_left=days_left,
                           hold_pts=hold, hold_pct=hold / width * 100,
                           timed_pts=timed, timed_pct=timed / width * 100, reason=reason,
                           mfe_pct=(mfe if np.isfinite(mfe) else 0.0) / width * 100))

ev = pd.DataFrame(events)
print(f"进场事件 {len(ev)} 次(今天刚拐头 × 首次穿线 × 未进红线;2013~2026 全历史,618 组合)")
print()


def summ(tag, s):
    if s.empty:
        print(f"  {tag:<22} 0")
        return
    print(f"  {tag:<22}{len(s):>5} | 持到期 中位 {s.hold_pct.median():>+6.1f}% 胜 {(s.hold_pct>0).mean()*100:>4.0f}%"
          f" | 择时 中位 {s.timed_pct.median():>+6.1f}% 均 {s.timed_pct.mean():>+6.1f}% 胜 {(s.timed_pct>0).mean()*100:>4.0f}% t {tstat(s.timed_pct):>+4.1f}"
          f" | 20 日 MFE 中位 {s.mfe_pct.median():>+5.1f}%")


print("=" * 120)
print("一、校准:持到期(复现 DEC-063 的合格 +29% / 不合格 −26%)")
print("=" * 120)
summ("Q 合格(现行 ⚡)", ev[ev.grp == "Q"])
summ("不合格(B+R)", ev[ev.grp != "Q"])
print()
print("=" * 120)
print("二、三组 × 两种出场")
print("=" * 120)
for grp, name in (("Q", "Q 合格"), ("B", "B 反弹候选"), ("R", "R 其余")):
    summ(name, ev[ev.grp == grp])
print()
print("  B 组择时出场原因:", ev[ev.grp == "B"].reason.value_counts().to_dict())
print("  Q 组择时出场原因:", ev[ev.grp == "Q"].reason.value_counts().to_dict())
print()
print("=" * 120)
print("三、B 组按品种")
print("=" * 120)
for inst, s in ev[ev.grp == "B"].groupby("inst"):
    summ(inst, s)
print()
print("=" * 120)
print("四、B 组按年份(看是不是某一年撑起来的)")
print("=" * 120)
b = ev[ev.grp == "B"].copy()
b["year"] = b.date.dt.year
for y, s in b.groupby("year"):
    print(f"  {y}: {len(s):>3} 次  择时中位 {s.timed_pct.median():>+6.1f}%  胜 {(s.timed_pct>0).mean()*100:>4.0f}%")
print()
lh = ev[(ev.c1 == "LH2611") & (ev.c2 == "LH2705")]
print("LH2611−LH2705 在本回放里:", lh[["date", "side", "grp", "n", "hit", "move", "drift", "timed_pct", "reason"]].to_string(index=False) if len(lh) else "无事件")
ev.to_csv(DATA / "bounce_tier_events.csv", index=False)
