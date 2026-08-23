"""生猪跨月 反弹进场 + 移动止盈 + 未启动时间止 —— 按 PLAN_LH_BOUNCE_v1 预注册执行。
事件复用 run_bounce_tier.detect_events,只取 LH;出场见 PLAN。"""
import numpy as np
import pandas as pd

from run_bounce_tier import detect_events, tstat

ACTIVATE, FRAC = 0.2, 1 / 3


def exit_pct(ev, time_stop):
    spread, i, j_end, dirn, entry = ev["path"], ev["i"], ev["j_end"], ev["dirn"], ev["entry"]
    act = ACTIVATE * ev["move"] if np.isfinite(ev["move"]) and ev["move"] > 0 else np.inf
    stop = ev["mae_max"] if np.isfinite(ev["mae_max"]) and ev["mae_max"] > 0 else np.inf
    peak, armed = -np.inf, False
    exit_px, reason, hold = spread[j_end], "窗口", j_end - (i + 1)
    for j in range(i + 2, j_end + 1):
        pnl = dirn * (spread[j] - entry)
        peak = max(peak, pnl)
        held = j - (i + 1)
        if pnl <= -stop:
            exit_px, reason, hold = spread[j], "止损", held
            break
        if peak >= act:
            armed = True
        if armed and pnl <= peak * (1 - FRAC):
            exit_px, reason, hold = spread[j], "移动止盈", held
            break
        if (not armed) and time_stop is not None and held >= time_stop:
            exit_px, reason, hold = spread[j], f"未启动{time_stop}日", held
            break
    return dirn * (exit_px - entry) / ev["width"] * 100, reason, hold


events = [e for e in detect_events() if e["inst"] == "LH"]
print(f"生猪进场事件 {len(events)} 次(B {sum(1 for e in events if e['grp']=='B')} / Q {sum(1 for e in events if e['grp']=='Q')} / R {sum(1 for e in events if e['grp']=='R')})")
rows = []
for e in events:
    r = {k: e[k] for k in ("c1", "c2", "date", "side", "grp", "hold_pct", "timed_pct")}
    for ts in (None, 10, 20, 30):
        pct, reason, hold = exit_pct(e, ts)
        key = "none" if ts is None else str(ts)
        r[f"p{key}"], r[f"r{key}"], r[f"h{key}"] = pct, reason, hold
    rows.append(r)
ev = pd.DataFrame(rows)
ev["year"] = ev.date.dt.year


def line(tag, s, col):
    if s.empty:
        print(f"  {tag:<26} 0")
        return
    x = s[col]
    print(f"  {tag:<26}{len(s):>4} | 中位 {x.median():>+6.1f}%  均 {x.mean():>+6.1f}%  胜 {(x>0).mean()*100:>4.0f}%  t {tstat(x):>+4.1f}")


print()
for ts, tag in ((None, "移动止盈·无时间止(上一份)"), (10, "未启动 10 日止"), (20, "**主规格 未启动 20 日止**"), (30, "未启动 30 日止")):
    key = "none" if ts is None else str(ts)
    print(f"—— {tag} ——")
    for grp, name in (("B", "生猪 B 反弹候选"), ("Q", "生猪 Q 合格")):
        line(name, ev[ev.grp == grp], f"p{key}")
    b = ev[ev.grp == "B"]
    print(f"  B 出场原因 {b[f'r{key}'].value_counts().to_dict()}  均持有 {b[f'h{key}'].mean():.0f} 日")
    print()
print("对照:生猪 B 半 move 择时 中位 %+.1f%% 均 %+.1f%%;持到期 中位 %+.1f%% 均 %+.1f%%" % (
    ev[ev.grp == "B"].timed_pct.median(), ev[ev.grp == "B"].timed_pct.mean(),
    ev[ev.grp == "B"].hold_pct.median(), ev[ev.grp == "B"].hold_pct.mean()))
print()
print("主规格 20 日:生猪 B 逐年")
b = ev[ev.grp == "B"]
for y, s in b.groupby("year"):
    x = s["p20"]
    print(f"  {y}: {len(s):>3} 次  中位 {x.median():>+6.1f}%  均 {x.mean():>+6.1f}%  胜 {(x>0).mean()*100:>4.0f}%")
print()
bb, q = b["p20"], ev[ev.grp == "Q"]["p20"]
yrs = b.groupby("year")["p20"].mean()
g = [bb.median() > 0, tstat(bb) > 2, (bb > 0).mean() >= 0.55, bb.mean() >= q.mean(), (yrs > 0).mean() >= 0.5]
print(f"判据:中位>0 {g[0]} | t>2 {g[1]}(t={tstat(bb):+.2f}) | 胜率≥55% {g[2]} | 均值不劣于 Q {g[3]}(B {bb.mean():+.1f} vs Q {q.mean():+.1f}) | 逐年均值为正≥半数 {g[4]}({int((yrs>0).sum())}/{len(yrs)})  → {sum(g)}/5")
print()
print("生猪 B 逐笔(主规格 20 日):")
for _, r in b.sort_values("date").iterrows():
    print(f"  {r.date.date()}  {r.c1}-{r.c2}  {r.side:<4}  {r.p20:>+7.1f}%  {r.r20}  持 {r.h20:.0f} 日")
