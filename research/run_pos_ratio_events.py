"""仓位比例转折事件探针(焦煤):建仓完成/卸仓开始,各届首次触发,测后 5/10/20 日。

预注册阈值(2026-08-24,不调参):
- 建仓事件:阵营加权满仓度在本届内从 <30% 首次上穿 >=60%,跟阵营方向。
- 卸仓事件:本届内阵营满仓度峰值 >=50%,首次回落到 峰值x0.8 以下,反着阵营方向
  (空头卸仓 -> 做多反弹;多头卸仓 -> 做空)。
- 另列人头口径参考:满仓(>=80%)家数 >=2 的首日(只列不判)。
依赖:先跑 run_pos_ratio.py 生成 out/pos_ratio_jm.csv。
跑法:python research/run_pos_ratio_events.py JM
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "JM"
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
adj = (1 + mkt["ret"].fillna(0)).cumprod()

cp = pd.read_csv(OUT / f"pos_ratio_{code.lower()}.csv", parse_dates=["date"]).set_index("date")


def fwd(t, days):
    i = mkt.index.get_loc(t)
    j = min(i + days, len(mkt.index) - 1)
    if j <= i:
        return np.nan
    return (adj.iloc[j] / adj.iloc[i] - 1) * 100


lines = [f"{v['name']} 仓位比例转折事件  数据至 {cp.index[-1].date()}",
         "口径:建仓=<30%首次上穿>=60%;卸仓=峰值(>=50%)回落到峰值x0.8 以下;各届首次。",
         "收益=事件日起复权主力 5/10/20 日;方向已按信号折算(正=信号方向对)。", ""]

events = []  # (类型, 阵营, 届, 日期, side, f5, f10, f20)
for c, seg in cp.groupby("main", sort=False):
    if len(seg) < 20:
        continue
    for camp, col, side_build in (("净空", "short_w", -1), ("净多", "long_w", +1)):
        w = seg[col].astype(float)
        # 建仓:<30 之后首次 >=60
        armed = False
        b_day = None
        for t, x in w.items():
            if not np.isfinite(x):
                continue
            if x < 30:
                armed = True
            elif x >= 60 and armed:
                b_day = t
                break
        if b_day is not None:
            f5, f10, f20 = fwd(b_day, 5), fwd(b_day, 10), fwd(b_day, 20)
            events.append(("建仓", camp, c, b_day, side_build,
                           side_build * f5, side_build * f10, side_build * f20))
        # 卸仓:峰值>=50 后首次 < 峰值x0.8
        peak = -np.inf
        u_day = None
        for t, x in w.items():
            if not np.isfinite(x):
                continue
            peak = max(peak, x)
            if peak >= 50 and x < peak * 0.8:
                u_day = t
                break
        if u_day is not None:
            side = -side_build  # 反着阵营方向
            f5, f10, f20 = fwd(u_day, 5), fwd(u_day, 10), fwd(u_day, 20)
            events.append(("卸仓", camp, c, u_day, side,
                           side * f5, side * f10, side * f20))

ev = pd.DataFrame(events, columns=["type", "camp", "main", "date", "side", "f5", "f10", "f20"])
for typ in ("建仓", "卸仓"):
    for camp in ("净空", "净多"):
        sub = ev[(ev["type"] == typ) & (ev["camp"] == camp)]
        if sub.empty:
            continue
        lines.append(f"=== {typ}·{camp}阵营(信号方向{'跟随' if typ=='建仓' else '反向'})  n={len(sub)} ===")
        for _, r in sub.iterrows():
            lines.append(f"  {r['main']}  {r['date'].date()}  side={'多' if r['side']>0 else '空'}  "
                         f"5日{r['f5']:+6.1f}%  10日{r['f10']:+6.1f}%  20日{r['f20']:+6.1f}%")
        for hz in ("f5", "f10", "f20"):
            x = sub[hz].dropna()
            if len(x):
                lines.append(f"    {hz}: 均值{x.mean():+.1f}%  中位{x.median():+.1f}%  "
                             f"胜{(x>0).mean()*100:.0f}%  n={len(x)}")
        lines.append("")

# 人头口径参考:满仓家数>=2 首日(各届各阵营)
lines.append("=== 参考:满仓(>=80%)家数>=2 首日(不判,只列)===")
for c, seg in cp.groupby("main", sort=False):
    if len(seg) < 20:
        continue
    for camp, col, sgn in (("净空", "short_full", -1), ("净多", "long_full", +1)):
        hit = seg[seg[col] >= 2]
        if len(hit):
            t0 = hit.index[0]
            lines.append(f"  {c} {camp}2家满仓 {t0.date()}  跟随方向20日 {sgn*fwd(t0,20):+.1f}%")

io.open(OUT / f"pos_ratio_events_{code.lower()}.txt", "w", encoding="utf-8").write("\n".join(lines))
ev.to_csv(OUT / f"pos_ratio_events_{code.lower()}.csv", index=False, encoding="utf-8")
print("ok")
