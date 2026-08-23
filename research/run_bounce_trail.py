"""反弹候选 + 移动止盈出场 —— 按 PLAN_BOUNCE_TRAIL_v1 预注册执行,判据原样。

事件与分组复用 run_bounce_tier.detect_events(同一套检测),只换出场:
  启动:peak ≥ 0.2×历年 move 中位;启动后自 peak 回撤 ≥ peak×frac 即走(主规格 1/3;1/4、1/2 敏感性);
  硬止损 历年 MAE 最大;窗口止点兜底;不设时间止。
判据:B 组中位 >0、t>2、胜率 ≥55%、不劣于 Q 同口径;2025 不翻大负;不只靠鸡蛋。
"""
import numpy as np
import pandas as pd

from run_bounce_tier import detect_events, tstat

FRACS = (1 / 3, 1 / 4, 1 / 2)
ACTIVATE = 0.2


def trail_exit(ev, frac):
    spread, i, j_end, dirn, entry = ev["path"], ev["i"], ev["j_end"], ev["dirn"], ev["entry"]
    move, mae_max = ev["move"], ev["mae_max"]
    act = ACTIVATE * move if np.isfinite(move) and move > 0 else np.inf
    stop = mae_max if np.isfinite(mae_max) and mae_max > 0 else np.inf
    peak = -np.inf
    exit_px, reason, hold = spread[j_end], "窗口", j_end - (i + 1)
    for j in range(i + 2, j_end + 1):
        pnl = dirn * (spread[j] - entry)
        peak = max(peak, pnl)
        if pnl <= -stop:
            exit_px, reason, hold = spread[j], "止损", j - (i + 1)
            break
        if peak >= act and pnl <= peak * (1 - frac):
            exit_px, reason, hold = spread[j], "移动止盈", j - (i + 1)
            break
    return dirn * (exit_px - entry) / ev["width"] * 100, reason, hold


events = detect_events()
rows = []
for e in events:
    r = {k: e[k] for k in ("c1", "c2", "inst", "date", "side", "grp", "hold_pct", "timed_pct", "mfe_pct")}
    for f in FRACS:
        pct, reason, hold = trail_exit(e, f)
        r[f"t{f:.2f}"], r[f"r{f:.2f}"], r[f"h{f:.2f}"] = pct, reason, hold
    rows.append(r)
ev = pd.DataFrame(rows)
ev["year"] = ev.date.dt.year


def line(tag, s, col):
    if s.empty:
        print(f"  {tag:<18} 0")
        return
    x = s[col]
    print(f"  {tag:<18}{len(s):>5} | 中位 {x.median():>+6.1f}%  均 {x.mean():>+6.1f}%  胜 {(x>0).mean()*100:>4.0f}%  t {tstat(x):>+4.1f}")


print("=" * 100)
print("移动止盈(启动 peak≥0.2×move;回撤 peak×frac 走;硬止损 MAE 最大;不设时间止)")
print("=" * 100)
for f in FRACS:
    col = f"t{f:.2f}"
    tag = "主规格 1/3" if abs(f - 1 / 3) < 1e-9 else f"敏感性 {f:.2f}"
    print(f"—— {tag} ——")
    for grp, name in (("Q", "Q 合格"), ("B", "B 反弹候选"), ("R", "R 其余")):
        line(name, ev[ev.grp == grp], col)
    b = ev[ev.grp == "B"]
    print(f"  B 出场原因 {b[f'r{f:.2f}'].value_counts().to_dict()}  均持有 {b[f'h{f:.2f}'].mean():.0f} 日")
    print()
print("对照(上一份):B 半 move 择时 中位 %+.1f%% 均 %+.1f%%;B 持到期 中位 %+.1f%%" % (
    ev[ev.grp == "B"].timed_pct.median(), ev[ev.grp == "B"].timed_pct.mean(), ev[ev.grp == "B"].hold_pct.median()))
print()
print("=" * 100)
print("主规格 1/3:B 组按品种 / 按年")
print("=" * 100)
b = ev[ev.grp == "B"]
for inst, s in b.groupby("inst"):
    line(inst, s, "t0.33")
print()
for y, s in b.groupby("year"):
    x = s["t0.33"]
    print(f"  {y}: {len(s):>3} 次  中位 {x.median():>+6.1f}%  均 {x.mean():>+6.1f}%  胜 {(x>0).mean()*100:>4.0f}%")
print()
q, bb = ev[ev.grp == "Q"]["t0.33"], b["t0.33"]
g1 = bb.median() > 0
g2 = tstat(bb) > 2
g3 = (bb > 0).mean() >= 0.55
g4 = bb.mean() >= q.mean() and bb.median() >= q.median()
print(f"判据:中位>0 {g1} | t>2 {g2}(t={tstat(bb):+.2f}) | 胜率≥55% {g3} | 不劣于 Q {g4}(B 均 {bb.mean():+.1f} vs Q {q.mean():+.1f})  → {sum([g1,g2,g3,g4])}/4")
lh = ev[(ev.c1 == "LH2611") & (ev.c2 == "LH2705") & (ev.grp == "B")]
print("LH2611−LH2705 7/27 在移动止盈 1/3 下:", lh[["date", "t0.33", "r0.33", "h0.33"]].to_string(index=False) if len(lh) else "无")
