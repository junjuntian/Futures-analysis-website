"""纯碱全席位侦察扫描(描述性,非闸门验收):谁在赚钱?跟得上吗?"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
price = H.clean_price(pd.read_csv(D / "sa_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "sa_seat.csv.gz"))
v = H.use("SA")
mkt = H.main_series(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
st = price.pivot_table(index="trade_date", columns="contract", values="settle", aggfunc="first").sort_index()
mains = [c for c in dict.fromkeys(mkt["main"]) if isinstance(c, str)]
mult = H.RULES["multiplier"]

cand = seat.groupby("member_key")["trade_date"].count()
cand = cand[cand >= 400].index.tolist()

rows = []
for m in cand:
    sub = seat[seat["member_key"] == m]
    # 该席位在全部合约上的自身盈亏(净持仓 × 次日结算变动,逐合约)
    pnl = 0.0
    covered = 0
    for c in sub["contract"].unique():
        if c not in st.columns:
            continue
        w = sub[sub["contract"] == c].pivot_table(index="trade_date", values="net_off",
                                                  aggfunc="sum").iloc[:, 0]
        px = st[c].dropna()
        wf = w.reindex(px.index).ffill()
        dpx = px.diff()
        leg = (wf.shift(1) * dpx).dropna()
        pnl += float(leg.sum()) * mult
        covered += int(leg.notna().sum())
    # 主力方向 T+1 跟随
    sig = pd.Series(np.nan, index=mkt.index)
    for c in mains:
        rws = sub[sub["contract"] == c]
        if rws.empty:
            continue
        w = rws.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = mkt.index[mkt["main"] == c]
        sig.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
    pos = np.sign(sig)
    pos[pos == 0] = np.nan
    pos = pos.ffill()
    base = (pos.shift(2) * mkt["ret_open"]).dropna()
    if len(base) < 300:
        continue
    eq = (1 + base).cumprod()
    sh = float(base.mean() / base.std() * np.sqrt(242)) if base.std() > 0 else np.nan
    ys = {y: (np.prod(1 + g) - 1) for y, g in base.groupby(base.index.year)}
    posy = sum(1 for x in ys.values() if x > 0)
    flips = int((pos != pos.shift()).sum())
    rows.append({"m": m, "pnl_yi": pnl / 1e8, "sh": sh,
                 "cum": (float(eq.iloc[-1]) - 1) * 100,
                 "posy": f"{posy}/{len(ys)}", "flips": flips,
                 "inmkt": float(pos.shift(2).notna().mean() * 100)})

rows.sort(key=lambda r: -r["pnl_yi"])
L = [f"纯碱全席位侦察(数据至 {mkt.index[-1].date()};自身盈亏=全合约净持仓×结算变动,亿元;跟随=主力T+1)",
     f"入围 {len(rows)} 家(样本≥400行)", "",
     f"{'席位':<8} 自身盈亏(亿)  跟随累计%  夏普  正年  翻转  在场%"]
for r in rows:
    L.append(f"{r['m']:<8} {r['pnl_yi']:+8.2f}    {r['cum']:+8.1f}  {r['sh']:5.2f}  {r['posy']:>4}  {r['flips']:>4}  {r['inmkt']:.0f}")
L.append("")
L.append("按跟随夏普排序前5: " + "; ".join(
    f"{r['m']}({r['sh']:.2f}/{r['posy']})" for r in sorted(rows, key=lambda x: -x["sh"])[:5]))
txt = "\n".join(L)
io.open(OUT / "sa_seat_scan.txt", "w", encoding="utf-8").write(txt)
