"""玻纯联动 v2(PLAN_FGSA_LINK_v2):只在"两边吃"状态持仓价差。跑法:python research/run_fgsa_link2.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"

data = {}
for code in ("FG", "SA"):
    price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
    v = H.use(code)
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        groups, log = H.apply_group_overrides(groups, log, cuts, H.RULES["group_overrides"], seat, price)
    sig = H.signal_series(seat, groups)
    sub = seat[seat["member_key"] == "永安期货"]
    ya = pd.Series(np.nan, index=mkt.index)
    for c in dict.fromkeys(mkt["main"]):
        if not isinstance(c, str):
            continue
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = mkt.index[mkt["main"] == c]
        ya.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
    data[code] = {"mkt": mkt, "net": sig["net"], "ya": ya}

idx = data["FG"]["mkt"].index.intersection(data["SA"]["mkt"].index)
idx = idx[idx >= pd.Timestamp("2020-06-01")]
ret_sp = (data["FG"]["mkt"]["ret_open"].reindex(idx).fillna(0)
          - data["SA"]["mkt"]["ret_open"].reindex(idx).fillna(0))


def hedge_pos(fg, sa):
    """反向态 -> sign(fg);同向/缺数 -> 0。"""
    f, s = np.sign(fg.reindex(idx)), np.sign(sa.reindex(idx))
    pos = pd.Series(0.0, index=idx)
    m = f.notna() & s.notna() & (f * s < 0)
    pos[m] = f[m]
    return pos


def perf(d):
    d = pd.Series(d).dropna()
    if not len(d):
        return np.nan, np.nan, np.nan
    eq = (1 + d).cumprod()
    return ((float(eq.iloc[-1]) - 1) * 100,
            float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan,
            float((eq / eq.cummax() - 1).min()) * 100)


L = [f"玻纯联动 v2:对冲簿状态门(样本 {idx[0].date()} ~ {idx[-1].date()},n={len(idx)})", ""]
rng = np.random.default_rng(31)
for name, fg, sa in (("D1 阵营对冲簿", data["FG"]["net"], data["SA"]["net"]),
                     ("D2 永安对冲簿", data["FG"]["ya"], data["SA"]["ya"])):
    pos = hedge_pos(fg, sa)
    held = pos.shift(2)
    base = (held * ret_sp).dropna()
    cum, sh, mdd = perf(base)
    inmkt = float((held != 0).mean() * 100)
    flips = int((pos != pos.shift()).sum())
    arr, n = pos.values, len(pos)
    sh_l = []
    for k in range(500):
        off = int(rng.integers(20, n - 20))
        d2 = (pd.Series(np.roll(arr, off), index=idx).shift(2) * ret_sp).dropna()
        sh_l.append(float(d2.mean() / d2.std() * np.sqrt(242)) if d2.std() > 0 else 0.0)
    p_pl = float((np.array(sh_l) >= sh).mean())
    _, sh_t2, _ = perf(pos.shift(3) * ret_sp)
    turn = (pos.shift(2) != pos.shift(3)).astype(float)
    cum_n, sh_n, _ = perf(held * ret_sp - turn * 0.002)
    ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in base.groupby(base.index.year)}
    # 附:状态内 vs 状态外的价差表现(按持仓方向折算)
    in_state = base[held.reindex(base.index) != 0]
    ok = p_pl < 0.05 and (np.isfinite(sh_t2) and sh_t2 >= 0.8 * sh) and cum_n > 0
    L.append(f"{name}: 在场 {inmkt:.0f}%  累计 {cum:+.1f}%  夏普 {sh:.2f}  回撤 {mdd:+.1f}%  段翻转 {flips}")
    L.append(f"  状态内日均(按方向) {in_state.mean()*1e4:+.1f}bp(n={len(in_state)});"
             f"状态外价差绝对日波动 {ret_sp[held.reindex(idx).fillna(0)==0].abs().mean()*1e4:.0f}bp")
    L.append(f"  闸门: 安慰剂 p={p_pl:.3f}  T+2 {sh_t2:.2f}(需≥{0.8*sh:.2f})  扣成本 {cum_n:+.1f}%/{sh_n:.2f}"
             f"  -> {'全过' if ok else '不过'}(Bonferroni×2 p={min(p_pl*2,1):.3f})")
    L.append("  逐年: " + "  ".join(f"{y}:{vv:+.1f}%" for y, vv in sorted(ys.items())))
    L.append("")

txt = "\n".join(L)
io.open(OUT / "fgsa_link2.txt", "w", encoding="utf-8").write(txt)
