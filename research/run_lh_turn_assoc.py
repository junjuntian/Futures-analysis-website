"""生猪跨月价差「低位拐头」与 机构减仓/散户减仓 有没有关联;以及 2026 年「拐头 10% 直接进场」
会是什么样(运营者 2026-08-23:只看磨底年,不看五年)。

① 关联:对 2025~2026 生猪跨月价差的每次低位新拐头(首次穿线),列出当天与前后 5 日的
   机构组方向/本轮卸仓比例/卸仓 5 日变化、散户三家净持仓 5 日变化与反向 z。
   另列 7/24~8/4 那段的逐日时间线(价差触底 7/24、拐头 7/27、9 月/11 月 8/4 才反弹)。
② 2026 年:所有生猪跨月低位新拐头事件,不看合格门,逐笔 20 日 / 持至今 结果,
   并与「合格门」的判定并列 —— 看合格门在 2026 拦掉了什么。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402
from run_bounce_tier import detect_events  # noqa: E402

# —— 机构/散户状态(引擎口径) ——
sig, mkt, rdf, op, st, groups, unload = R.load("LH")
cc = H.inst_cost_series(sig, mkt, groups)
side = cc["side"].reindex(mkt.index)
unl = unload.reindex(mkt.index)
rnet = rdf["net"].reindex(mkt.index)
rz = rdf["rz"].reindex(mkt.index)


def at(s, d, k=0):
    i = mkt.index.get_indexer([d], method="pad")[0]
    j = i + k
    if i < 0 or j < 0 or j >= len(mkt.index):
        return np.nan
    return s.iloc[j]


ev = [e for e in detect_events() if e["inst"] == "LH" and e["side"] == "low" and e["date"].year >= 2025]
print("=" * 118)
print("① 2025~2026 生猪跨月价差低位新拐头(首次穿线)× 当天机构/散户状态")
print("=" * 118)
print(f"  {'拐头日':<11}{'组合':<16}{'合格?':<5}{'机构方向':<6}{'卸仓%':>6}{'卸仓5日变化':>10}{'散户净持仓':>10}{'散户5日变化':>10}{'散户z':>7}{'价差20日%':>10}")
rows = []
for e in ev:
    d = e["date"]
    u0, um5 = at(unl, d), at(unl, d, -5)
    sd = at(side, d)
    rn, rn5 = at(rnet, d), at(rnet, d, -5)
    q = "合格" if e["grp"] == "Q" else "—"
    rows.append(dict(d=d, u0=u0, du=u0 - um5 if np.isfinite(u0) and np.isfinite(um5) else np.nan,
                     rdelta=rn - rn5 if np.isfinite(rn) and np.isfinite(rn5) else np.nan, z=at(rz, d),
                     out=e["timed_pct"], hold=e["hold_pct"], sd=sd))
    print(f"  {d.date()}  {e['c1']}-{e['c2']:<9}{q:<5}{('净空' if sd<0 else '净多' if sd>0 else '—'):<6}"
          f"{(u0*100 if np.isfinite(u0) else float('nan')):>6.0f}{((u0-um5)*100 if np.isfinite(u0) and np.isfinite(um5) else float('nan')):>+10.0f}"
          f"{(rn if np.isfinite(rn) else float('nan')):>10.0f}{((rn-rn5) if np.isfinite(rn) and np.isfinite(rn5) else float('nan')):>+10.0f}"
          f"{(at(rz,d) if np.isfinite(at(rz,d)) else float('nan')):>7.2f}{e['timed_pct']:>+10.1f}")
df = pd.DataFrame(rows)
print()
print(f"  拐头日机构本轮卸仓 5 日变化:均值 {df.du.mean()*100:+.1f}pp,为正(在减仓)的占 {(df.du>0).mean()*100:.0f}%")
print(f"  拐头日散户净持仓 5 日变化:均值 {df.rdelta.mean():+.0f} 手,为负(在减多/加空)的占 {(df.rdelta<0).mean()*100:.0f}%")
print(f"  机构卸仓 5 日变化 与 价差 20 日结果 相关:{df.du.corr(df.out):+.2f};散户 5 日变化 与 结果 相关:{df.rdelta.corr(df.out):+.2f}")
print()
print("=" * 118)
print("7/24 → 8/4 逐日时间线:LH2611−LH2705 价差 / 机构净空卸仓% / 散户净持仓 / 9 月 11 月合约结算")
print("=" * 118)
price = H.clean_price(pd.read_csv(R.DATA / "lh_price.csv.gz"))
stx = price.pivot_table(index="trade_date", columns="contract", values="settle", aggfunc="first")
for d in pd.bdate_range("2026-07-20", "2026-08-18"):
    if d not in mkt.index:
        continue
    s = stx.at[d, "LH2611"] - stx.at[d, "LH2705"] if d in stx.index else np.nan
    print(f"  {d.date()}  价差 {s:>+7.0f}  机构 {('净空' if at(side,d)<0 else '净多' if at(side,d)>0 else '—')} 卸 {at(unl,d)*100:>4.0f}%"
          f"  散户净 {at(rnet,d):>+7.0f}  z {at(rz,d):>+5.2f}  | LH2609 {stx.at[d,'LH2609']:>6.0f}  LH2611 {stx.at[d,'LH2611']:>6.0f}  LH2701 {stx.at[d,'LH2701']:>6.0f}")
print()
print("=" * 118)
print("② 2026 年:生猪跨月低位新拐头「拐头 10% 直接进场」(不看合格门)逐笔")
print("=" * 118)
ev26 = [e for e in detect_events() if e["inst"] == "LH" and e["side"] == "low" and e["date"].year == 2026]
print(f"  {'拐头日':<11}{'组合':<16}{'合格门':<8}{'20日%':>7}{'持至今/到期%':>12}{'移动止盈1/3%':>12}")
for e in ev26:
    # 移动止盈 1/3(启动 0.2×move,硬止损 MAE 最大)
    s, i, j, dn, en = e["path"], e["i"], e["j_end"], e["dirn"], e["entry"]
    act = 0.2 * e["move"] if np.isfinite(e["move"]) and e["move"] > 0 else np.inf
    stop = e["mae_max"] if np.isfinite(e["mae_max"]) and e["mae_max"] > 0 else np.inf
    peak, px = -np.inf, s[j]
    for k in range(i + 2, j + 1):
        p = dn * (s[k] - en)
        peak = max(peak, p)
        if p <= -stop or (peak >= act and p <= peak * 2 / 3):
            px = s[k]
            break
    trail = dn * (px - en) / e["width"] * 100
    print(f"  {e['date'].date()}  {e['c1']}-{e['c2']:<9}{('合格' if e['grp']=='Q' else '被拦'):<8}{e['timed_pct']:>+7.1f}{e['hold_pct']:>+12.1f}{trail:>+12.1f}")
d26 = pd.DataFrame([{"q": e["grp"] == "Q", "t": e["timed_pct"], "h": e["hold_pct"]} for e in ev26])
if len(d26):
    print(f"\n  2026 合计 {len(d26)} 次:20 日 均 {d26.t.mean():+.1f}% 胜 {(d26.t>0).mean()*100:.0f}% | 被合格门拦掉的 {int((~d26.q).sum())} 次 20 日均 {d26[~d26.q].t.mean():+.1f}% 胜 {(d26[~d26.q].t>0).mean()*100:.0f}%")
