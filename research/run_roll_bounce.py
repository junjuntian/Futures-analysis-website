"""换月反弹探针(焦煤):主力剩 1~2 个月、机构落袋为安时,主力/次主力是否反弹。

口径(2026-08-24,运营者理论:剩1-2个月机构落袋->价格反弹;主力不弹次主力也弹):
- 锚点A:该届主力距窗口止点首次 <=40 交易日(约2个月);锚点B:首次 <=20(约1个月);
- 锚点C(条件版):在 <=45 交易日窗口内,主导阵营(|净|大的一方)首次落袋
  (|净| < 生命周期运行峰值的 85%)。
- 每个锚点量:主导阵营方向;此后 10/20 交易日,主力自身与次主力(X+4)的
  「反弹」收益 = 与主导阵营相反方向的价格变动(空主导 -> 涨为正)。
跑法:python research/run_roll_bounce.py JM
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "JM"
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
raw = pd.read_csv(D / f"{code.lower()}_price.csv.gz")
price = H.clean_price(raw)
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
GRP_seat = seat  # 全部行
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
roll, _, _ = H.rolling_groups(seat, price, mkt.index)
GRP = list(roll.dropna().iloc[-1])
CONTRACTS = ["JM2401", "JM2405", "JM2409", "JM2501", "JM2505",
             "JM2509", "JM2601", "JM2605", "JM2609", "JM2701"]
settle_w = price.pivot_table(index="trade_date", columns="contract", values="settle", aggfunc="first").sort_index()


def next_contract(c):
    yy, mm = int(c[2:4]), int(c[4:6])
    mm += 4
    if mm > 12:
        mm -= 12; yy += 1
    return f"{code}{yy:02d}{mm:02d}"


def fwd(series, t, days):
    s = series.dropna()
    s = s[s.index >= t]
    if len(s) <= days:
        return np.nan
    return (float(s.iloc[days]) / float(s.iloc[0]) - 1) * 100


lines = [f"{v['name']} 换月反弹探针  组: " + "、".join(GRP),
         "反弹收益 = 与主导阵营相反方向(空主导时=涨幅);次主力 = X+4。", ""]
rows = {"A剩40日": [], "B剩20日": [], "C落袋": []}
for c in CONTRACTS:
    if c not in settle_w.columns:
        continue
    px = settle_w[c].dropna()
    sub = seat[(seat["member_key"].isin(GRP)) & (seat["contract"] == c)]
    if sub.empty:
        continue
    w = sub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first").reindex(px.index).ffill()
    nlong = w.where(w > 0).abs().sum(axis=1)
    nshort = w.where(w < 0).abs().sum(axis=1)
    nc = next_contract(c)
    px2 = settle_w[nc].dropna() if nc in settle_w.columns else pd.Series(dtype=float)
    dleft = pd.Series([H.days_to_window_end(c, t) for t in px.index], index=px.index)
    anchors = {}
    a = dleft[dleft <= 40]
    if len(a):
        anchors["A剩40日"] = a.index[0]
    b = dleft[dleft <= 20]
    if len(b):
        anchors["B剩20日"] = b.index[0]
    # C:<=45 日窗口内主导阵营首次落袋
    win = dleft[(dleft <= 45) & (dleft > 0)].index
    cc = None
    for t in win:
        dom = +1 if nlong.get(t, 0) >= nshort.get(t, 0) else -1
        ser = nlong if dom > 0 else nshort
        peak = ser[ser.index <= t].max()
        if peak and peak > 0 and float(ser[t]) < peak * 0.85:
            cc = t
            break
    if cc is not None:
        anchors["C落袋"] = cc
    for k, t in anchors.items():
        dom = +1 if nlong.get(t, 0) >= nshort.get(t, 0) else -1
        bounce = -dom
        m10, m20 = bounce * fwd(px, t, 10), bounce * fwd(px, t, 20)
        n10, n20 = bounce * fwd(px2, t, 10), bounce * fwd(px2, t, 20)
        rows[k].append((c, t.date(), "空" if dom < 0 else "多", m10, m20, n10, n20))

for k, rr in rows.items():
    lines.append(f"=== 锚点 {k}(n={len(rr)})===")
    lines.append(f"{'合约':8s}{'日期':12s}{'主导':4s}{'主力10日':>9s}{'主力20日':>9s}{'次主力10日':>10s}{'次主力20日':>10s}")
    for c, d, dom, m10, m20, n10, n20 in rr:
        f = lambda x: f"{x:+8.1f}" if np.isfinite(x) else "      —"
        lines.append(f"{c:8s}{d!s:12s}{dom:4s}{f(m10)}%{f(m20)}%{f(n10)}%{f(n20)}%")
    for col, name in ((3, "主力10日"), (4, "主力20日"), (5, "次主力10日"), (6, "次主力20日")):
        x = pd.Series([r[col] for r in rr]).dropna()
        if len(x):
            lines.append(f"  {name}: 均值{x.mean():+.2f}%  中位{x.median():+.2f}%  正比例{(x>0).mean()*100:.0f}%  n={len(x)}")
    lines.append("")
io.open(OUT / f"roll_bounce_{code.lower()}.txt", "w", encoding="utf-8").write("\n".join(lines))
print("ok")
