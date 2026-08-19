"""任意品种的合计流向能力验证:`python run_flow_skill.py SA`

由 run_fg_skill.py 泛化而来(2026-08-19 加纯碱时)。测一个品种能不能做这套策略,
一条命令跑完:alpha 排行 → 逐年留一 → 滚动样本外 → 选择准则对照 → 逐年符号。

**逐年符号那一节是重点**:玻璃的教训是全样本 t=+2.96 看着能用,拆到逐年是
7 正 7 负——显著性被一段行情单独撑起来。符号一半一半的因子不能用。
"""
from __future__ import annotations
import sys
import numpy as np, pandas as pd
import lhlib as L

pd.set_option("display.width", 210); pd.set_option("display.max_columns", 60)
CODE = (sys.argv[1] if len(sys.argv) > 1 else "SA").upper()
H = 20


def section(t): print("\n" + "=" * 80); print(t); print("=" * 80)


def build(code):
    price = L.load_price(code); seat = L.load_seat(code)
    fwd = L.forward_returns(price, (5, 10, H)); mc = L.main_contract(price)
    df = seat.merge(fwd[["contract", "trade_date", "settle", f"fwd{H}"]],
                    on=["contract", "trade_date"], how="inner")
    m = mc.set_index("trade_date")["main"]; px = fwd.set_index(["contract", "trade_date"])
    rows = [(d, px.loc[(c, d)]["settle"], px.loc[(c, d)][f"fwd{H}"])
            for d, c in m.items() if (c, d) in px.index]
    main = pd.DataFrame(rows, columns=["trade_date", "settle", "fwd"]).set_index("trade_date")
    main["past"] = main["settle"].pct_change(20)
    return df, main


def seat_alpha(df, code, hi=None, min_days=250):
    d = df if hi is None else df[df["trade_date"] < hi]
    d = d.sort_values(["member_key", "contract", "trade_date"]).copy()
    g = d.groupby(["member_key", "contract"])
    d["pn"] = g["net"].shift(); d["ps"] = g["settle"].shift()
    gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
    d = d[d["pn"].notna() & (gap <= 5)]
    if d.empty: return pd.DataFrame()
    d = d.assign(dpx=(d["settle"] - d["ps"]) * L.multiplier(code))
    gr = d.groupby("member_key")
    out = pd.DataFrame({
        "pnl": gr.apply(lambda s: (s["dpx"] * s["pn"]).sum(), include_groups=False),
        "beta": gr.apply(lambda s: (s["dpx"] * s["pn"].mean()).sum(), include_groups=False),
        "days": gr["trade_date"].nunique(), "avg_net": gr["pn"].mean()})
    out["alpha"] = out["pnl"] - out["beta"]
    for c in ("pnl", "beta", "alpha"): out[c] /= 1e8
    return out[out["days"] >= min_days].round(2)


def signal(df, members, win=5):
    return df[df["member_key"].isin(members)].groupby("trade_date")["net"].sum().sort_index().diff(win)


def power(sig, main):
    j = pd.concat([sig.rename("sig"), main[["fwd", "past"]]], axis=1, sort=True).dropna()
    if len(j) < 60: return None
    ry = j["fwd"] - np.polyval(np.polyfit(j["past"], j["fwd"], 1), j["past"])
    rx = j["sig"] - np.polyval(np.polyfit(j["past"], j["sig"], 1), j["past"])
    pr = float(np.corrcoef(ry, rx)[0, 1])
    return pr, pr * np.sqrt((len(j) - 3) / max(1e-12, 1 - pr ** 2)), len(j)


if __name__ == "__main__":
    df, main = build(CODE)
    print(f"{CODE} 席位 {df['trade_date'].nunique()} 日 / {df['member_key'].nunique()} 家 "
          f"[{df['trade_date'].min():%Y-%m-%d} ~ {df['trade_date'].max():%Y-%m-%d}]  "
          f"点值 {L.multiplier(CODE)}")

    section("① alpha 排行(全样本,仅参照)")
    full = seat_alpha(df, CODE).sort_values("alpha", ascending=False)
    print(full.head(8).to_string()); print("\n垫底 4 家:"); print(full.tail(4).to_string())

    section("② 滚动样本外(只用当下之前的数据选组)")
    yrs = sorted(df["trade_date"].dt.year.unique())
    cuts = [pd.Timestamp(f"{y}-01-01") for y in yrs[2::2]]
    print(f"  {'训练截止':11s}{'K':>3s}{'测试N':>7s}{'偏相关':>9s}{'t':>7s}  组")
    for k in (3, 5, 8):
        for cut in cuts:
            tr = seat_alpha(df, CODE, hi=cut, min_days=120)
            if tr.empty or len(tr) < k: continue
            grp = tr.sort_values("alpha", ascending=False).head(k).index.tolist()
            te = df[df["trade_date"] >= cut]; tm = main[main.index >= cut]
            r = power(signal(te, grp), tm)
            if r: print(f"  <{cut:%Y-%m}  {k:>3d}{r[2]:>7d}{r[0]:>+9.3f}{r[1]:>+7.2f}  "
                        f"{'、'.join(grp[:3])}" + ("…" if k > 3 else ""))

    section("③ 逐年符号(重点:符号一半一半的因子不能用)")
    grp = full.head(5).index.tolist()
    print(f"  组={'、'.join(grp)}")
    j = pd.concat([signal(df, grp).rename("sig"), main], axis=1, sort=True)
    j["y"] = j.index.year
    pos = tot = 0
    for y, sub in j.groupby("y"):
        r = power(sub["sig"], sub)
        if r:
            tot += 1; pos += 1 if r[0] > 0 else 0
            print(f"  {y}  偏相关 {r[0]:+.3f}  t={r[1]:+6.2f}  N={r[2]:4d}  {'正' if r[0]>0 else '负'}")
    print(f"  → {tot} 年里 {pos} 年为正、{tot-pos} 年为负")
    r = power(signal(df, grp), main)
    if r: print(f"  全样本:偏相关 {r[0]:+.3f}  t={r[1]:+.2f}  N={r[2]}")
