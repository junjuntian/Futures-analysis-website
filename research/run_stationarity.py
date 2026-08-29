"""所有在线策略的平稳性 / 伪回归体检(运营者 2026-08-28 要求)。
跑法:python research/run_stationarity.py
"""
import sys, pathlib, io
import numpy as np, pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "research"))
import hog_money as H
import campaign as C
import statlib as S

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
rows = []      # (类别, 名称, 序列)


def load(code):
    price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
    H.use(code)
    rs = pd.Timestamp(H.RULES["replay_start"])
    if code == "JD":                                     # 采集坏日剔除(REPORT_JD_MODEL_v1)
        ok = price.dropna(subset=["open_interest"])["trade_date"].unique()
        price, seat = price[price["trade_date"].isin(ok)], seat[seat["trade_date"].isin(ok)]
    price, seat = price[price["trade_date"] >= rs], seat[seat["trade_date"] >= rs]
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= rs]
    op, st = H.contract_prices(price)
    groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        groups, log = H.apply_group_overrides(groups, log, cuts, H.RULES["group_overrides"], seat, price)
    return price, seat, mkt, op, st, groups


def member_pos(seat, mkt, member):
    sub = seat[seat["member_key"] == member]
    sig = pd.Series(np.nan, index=mkt.index)
    for c in dict.fromkeys(mkt["main"]):
        if not isinstance(c, str):
            continue
        r = sub[sub["contract"] == c]
        if r.empty:
            continue
        w = r.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = mkt.index[mkt["main"] == c]
        sig.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
    return sig


for code in ("LH", "JM", "FG", "JD", "SA"):
    price, seat, mkt, op, st, groups = load(code)
    sig = H.signal_series(seat, groups)
    if H.RULES.get("signal_source") == "cost":
        sig = H.attach_cost_signal(sig, seat, mkt, groups)
    if H.RULES.get("exit_mode") == "inst":
        sig = H.attach_inst_exit(sig, seat, mkt, groups)
    rdf, _ = H.retail_series(seat, mkt.index)
    if H.RULES.get("strategy") == "campaign":
        out = C.run(seat, mkt, op, st, list(groups.dropna().iloc[-1]), H.RULES)
        daily = out["daily"]
    else:
        _, _, daily = H.replay(sig, mkt, rdf, op, st)
    rows.append(("① 策略日收益", f"{code} 主引擎", pd.Series(daily).dropna()))
    # 信号变量:阵营净持仓水平 & z
    rows.append(("② 信号变量", f"{code} 阵营净持仓(水平)", sig["net"].dropna()))
    if "z" in sig:
        rows.append(("② 信号变量", f"{code} 流向 z", sig["z"].dropna()))
    # 成本进场品种:价格 − 机构成本(协整关键)
    if "cost" in sig.columns:
        gap = (mkt["settle"] - sig["cost"]).dropna() if "settle" in mkt else None
        if gap is None or gap.empty:
            px = pd.Series([st[c].get(d, np.nan) if isinstance(c, str) and c in st.columns else np.nan
                            for d, c in zip(mkt.index, mkt["main"])], index=mkt.index)
            gap = (px - sig["cost"]).dropna()
        rows.append(("③ 价格−机构成本(协整)", f"{code} 价格−成本", gap))
    # 第二引擎
    for member, tag in (("华泰期货", "JM"), ("永安期货", "FG")):
        if code == tag:
            p2 = np.sign(member_pos(seat, mkt, member)).replace(0, np.nan).ffill()
            d2 = (p2.shift(2) * mkt["ret_open"]).dropna()
            rows.append(("① 策略日收益", f"{code} 第二引擎(跟{member[:2]})", d2))
            rows.append(("② 信号变量", f"{code} {member[:2]}净持仓(水平)", member_pos(seat, mkt, member).dropna()))
            rows.append(("② 信号变量", f"{code} {member[:2]}方向(sign)", p2.dropna()))

# 玻纯对冲簿(DEC-142)
fg = load("FG"); sa = load("SA")
idx = fg[2].index.intersection(sa[2].index)
ya_fg = member_pos(fg[1], fg[2], "永安期货").reindex(idx)
ya_sa = member_pos(sa[1], sa[2], "永安期货").reindex(idx)
f_, s_ = np.sign(ya_fg), np.sign(ya_sa)
pos = pd.Series(0.0, index=idx)
m = f_.notna() & s_.notna() & (f_ * s_ < 0)
pos[m] = f_[m]
ret_sp = (fg[2]["ret_open"].reindex(idx).fillna(0) - sa[2]["ret_open"].reindex(idx).fillna(0))
rows.append(("① 策略日收益", "玻纯对冲簿(DEC-142)", (pos.shift(2) * ret_sp).dropna()))
rows.append(("② 信号变量", "玻纯对冲簿状态(0/±1)", pos))
rows.append(("④ 价差本身", "FG−SA 价差日收益", ret_sp.dropna()))

L = ["所有在线策略 · 平稳性与伪回归体检", ""]
L.append(f"{'序列':<34}{'N':>6}{'ADF':>9}{'KPSS':>8}  判定        前半夏普/后半夏普")
L.append("-" * 96)
cur = None
for cat, name, ser in rows:
    ser = pd.Series(ser).dropna()
    if len(ser) < 60:
        continue
    if cat != cur:
        L.append(f"【{cat}】")
        cur = cat
    a, k = S.adf(ser.values), S.kpss(ser.values)
    half = len(ser) // 2
    def sh(x):
        x = pd.Series(x).dropna()
        return float(x.mean() / x.std() * np.sqrt(242)) if x.std() > 0 else np.nan
    h1, h2 = sh(ser.iloc[:half]), sh(ser.iloc[half:])
    tail = f"{h1:+.2f} / {h2:+.2f}" if cat.startswith("①") else ""
    L.append(f"  {name:<32}{len(ser):>6}{a['stat']:>9.2f}{k['stat']:>8.3f}  {S.verdict_pair(a,k):<12}{tail}")
io.open(OUT / "stationarity.txt", "w", encoding="utf-8").write("\n".join(L))
