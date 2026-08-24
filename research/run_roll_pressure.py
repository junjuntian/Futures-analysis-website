"""移仓强制流压力表探针(PLAN_ROLL_PRESSURE_v1):拥挤方剩仓 vs 交割前近远月价差。

跑法:仓库根目录 python research/run_roll_pressure.py LH
"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H
import campaign as C

code = sys.argv[1] if len(sys.argv) > 1 else "LH"
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code)
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
GRP = list(H.RULES["fixed_members"]) if H.RULES.get("fixed_members") else None
RETAIL = [m for m in H.RULES["retail_seed"] if m in set(seat["member_key"])]
st = price.pivot_table(index="trade_date", columns="contract", values="settle", aggfunc="first").sort_index()
oi = price.pivot_table(index="trade_date", columns="contract", values="open_interest", aggfunc="first").sort_index()
vol = price.pivot_table(index="trade_date", columns="contract", values="volume", aggfunc="first").sort_index()


def next_contract(c, step=2):
    yy, mm = int(c[2:4]), int(c[4:6])
    mm += step
    if mm > 12:
        mm -= 12; yy += 1
    return f"{code}{yy:02d}{mm:02d}"


mains = [c for c in dict.fromkeys(mkt["main"]) if isinstance(c, str)]
rows = []
for c in mains:
    n = next_contract(c)
    if c not in st.columns or n not in st.columns:
        continue
    px_c = st[c].dropna()
    spread = (st[c] - st[n]).dropna()
    if len(spread) < 30:
        continue
    w = C.camp_frame(seat, c, GRP, px_c)
    rsub = seat[(seat["member_key"].isin(RETAIL)) & (seat["contract"] == c)]
    rw = (rsub.pivot_table(index="trade_date", columns="member_key", values="net_off", aggfunc="first")
              .reindex(px_c.index).ffill()) if len(rsub) else pd.DataFrame(index=px_c.index)
    dleft = pd.Series([H.days_to_window_end(c, t) for t in px_c.index], index=px_c.index)
    for thresh in (30, 20, 10):
        hit = dleft[(dleft <= thresh) & (dleft > 5)]
        if not len(hit):
            continue
        t0 = hit.index[0]
        # 主导侧与剩仓比(截至 t0,PIT)
        upto = w[w.index <= t0]
        long_net = upto.where(upto > 0).sum(axis=1)
        short_net = upto.where(upto < 0).abs().sum(axis=1)
        cl, cs = float(long_net.iloc[-1] or 0), float(short_net.iloc[-1] or 0)
        side = -1 if cs >= cl else +1          # -1=空头拥挤
        cur = cs if side < 0 else cl
        peak = float((short_net if side < 0 else long_net).max() or 0)
        if peak < 1000:
            continue
        remain = cur / peak
        # 散户剩仓(同向于拥挤方对面即散户接盘方,只记录)
        r_net = float(rw[rw.index <= t0].sum(axis=1).iloc[-1]) if len(rw.columns) else np.nan
        # 承接:近月 5 日均量 / 20 日均量
        vv = vol[c].reindex(px_c.index).ffill()
        vr = float(vv.rolling(5).mean().get(t0, np.nan) / vv.rolling(20).mean().get(t0, np.nan))
        # 结果:t0 -> dleft=5 的价差变动,按主导侧折算(空拥挤 -> 价差涨为正)
        end_hit = dleft[dleft <= 5]
        t1 = end_hit.index[0] if len(end_hit) else px_c.index[-1]
        s0, s1 = spread.asof(t0), spread.asof(t1)
        if not (np.isfinite(s0) and np.isfinite(s1)):
            continue
        move = float(s1 - s0) * (-side)        # side=-1(空) -> +(s1-s0)
        rows.append({"c": c, "thresh": thresh, "t0": str(t0.date()), "side": "空" if side < 0 else "多",
                     "cur": round(cur), "peak": round(peak), "remain": round(remain, 3),
                     "retail": round(r_net) if np.isfinite(r_net) else None,
                     "volr": round(vr, 2) if np.isfinite(vr) else None,
                     "move": round(move, 1),
                     "move_pct": round(move / float(px_c.asof(t0)) * 100, 2)})

df = pd.DataFrame(rows)
lines = [f"{v['name']} 移仓压力探针  届数 {df['c'].nunique()}  锚点行 {len(df)}", ""]
for thresh in (30, 20, 10):
    sub = df[df["thresh"] == thresh].copy()
    if len(sub) < 6:
        continue
    lines.append(f"=== 锚点 dleft<={thresh}(n={len(sub)})===")
    lines.append(f"  无条件基线(方向折算后价差变动): 均值 {sub['move'].mean():+.1f} 元/吨"
                 f"({sub['move_pct'].mean():+.2f}%)  正比例 {(sub['move']>0).mean()*100:.0f}%")
    med = sub["remain"].median()
    hi = sub[sub["remain"] >= med]
    lo = sub[sub["remain"] < med]
    lines.append(f"  高剩仓组(remain>={med:.2f},n={len(hi)}): 均值 {hi['move'].mean():+.1f}({hi['move_pct'].mean():+.2f}%) 正比例 {(hi['move']>0).mean()*100:.0f}%")
    lines.append(f"  低剩仓组(n={len(lo)}): 均值 {lo['move'].mean():+.1f}({lo['move_pct'].mean():+.2f}%) 正比例 {(lo['move']>0).mean()*100:.0f}%")
    ra, rb = sub["remain"].rank(), sub["move_pct"].rank()
    rho = float(np.corrcoef(ra, rb)[0, 1])
    lines.append(f"  剩仓比 vs 折算变动 秩相关: {rho:+.2f}")
    lines.append("")
lines.append("=== 逐届明细(dleft<=20 锚点)===")
for _, r in df[df["thresh"] == 20].sort_values("c").iterrows():
    lines.append(f"  {r['c']} {r['t0']} {r['side']}拥挤 剩仓{r['remain']:.0%}({r['cur']:,}/{r['peak']:,}) "
                 f"散户{r['retail']} 量比{r['volr']} -> 价差折算 {r['move']:+.1f}元({r['move_pct']:+.2f}%)")
io.open(OUT / f"roll_pressure_{code.lower()}.txt", "w", encoding="utf-8").write("\n".join(lines))
df.to_csv(OUT / f"roll_pressure_{code.lower()}.csv", index=False, encoding="utf-8")
print("ok", len(df))
