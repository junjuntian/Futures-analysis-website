"""玻璃(FG)席位能力验证:Phase 0 + Phase 1 合并。

按生猪那套流程走,但**不假设结论一样**——PITFALLS 六刚记过「同一套方法换个品种
可能整个反过来」。玻璃与生猪的先天条件差别很大:

  席位样本  FG 3,329 个交易日(2012-12 起) vs LH 731 天  → 4.5 倍,且跨牛熊
  数据来源  FG 纯 czce_official 单一来源   vs LH 四个源并存
  合约偏离  FG 6.6%/25.8%                 vs LH 7.1%/43.4%

样本跨 14 年是玻璃最大的优势:生猪最致命的弱点(只有一种市况)在这里不存在,
逐年留一能真的做起来。
"""
from __future__ import annotations
import numpy as np, pandas as pd
import lhlib as L

pd.set_option("display.width", 210); pd.set_option("display.max_columns", 60)
CODE = "FG"
MIN_DAYS = 250
HORIZON = 20


def section(t): print("\n" + "=" * 80); print(t); print("=" * 80)


def seat_alpha(df, lo=None, hi=None, min_days=MIN_DAYS):
    d = df
    if lo is not None: d = d[d["trade_date"] >= lo]
    if hi is not None: d = d[d["trade_date"] < hi]
    d = d.sort_values(["member_key", "contract", "trade_date"]).copy()
    g = d.groupby(["member_key", "contract"])
    d["prev_net"] = g["net"].shift(); d["prev_settle"] = g["settle"].shift()
    gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
    d = d[d["prev_net"].notna() & (gap <= 5)]
    if d.empty: return pd.DataFrame()
    d = d.assign(dpx=(d["settle"] - d["prev_settle"]) * L.multiplier(CODE))
    grp = d.groupby("member_key")
    out = pd.DataFrame({
        "pnl": grp.apply(lambda s: (s["dpx"] * s["prev_net"]).sum(), include_groups=False),
        "beta": grp.apply(lambda s: (s["dpx"] * s["prev_net"].mean()).sum(), include_groups=False),
        "days": grp["trade_date"].nunique(),
        "avg_net": grp["prev_net"].mean(),
    })
    out["alpha"] = out["pnl"] - out["beta"]
    for c in ("pnl", "beta", "alpha"): out[c] /= 1e8
    return out[out["days"] >= min_days].round(2)


def group_signal(df, members, win=5):
    s = df[df["member_key"].isin(members)].groupby("trade_date")["net"].sum().sort_index()
    return s.diff(win)


def power(sig, main):
    j = pd.concat([sig.rename("sig"), main[["fwd", "past"]]], axis=1, sort=True).dropna()
    if len(j) < 60: return {}
    ry = j["fwd"] - np.polyval(np.polyfit(j["past"], j["fwd"], 1), j["past"])
    rx = j["sig"] - np.polyval(np.polyfit(j["past"], j["sig"], 1), j["past"])
    pr = float(np.corrcoef(ry, rx)[0, 1])
    return {"N": len(j), "corr": j["sig"].corr(j["fwd"]), "partial": pr,
            "t": pr * np.sqrt((len(j) - 3) / max(1e-12, 1 - pr ** 2))}


price = L.load_price(CODE); seat = L.load_seat(CODE)
fwd = L.forward_returns(price, (5, 10, HORIZON)); mc = L.main_contract(price)
df = seat.merge(fwd[["contract", "trade_date", "settle", f"fwd{HORIZON}"]],
                on=["contract", "trade_date"], how="inner")
m = mc.set_index("trade_date")["main"]
px = fwd.set_index(["contract", "trade_date"])
rows = [(d, px.loc[(c, d)]["settle"], px.loc[(c, d)][f"fwd{HORIZON}"])
        for d, c in m.items() if (c, d) in px.index]
main = pd.DataFrame(rows, columns=["trade_date", "settle", "fwd"]).set_index("trade_date")
main["past"] = main["settle"].pct_change(20)
print(f"{CODE} 行情 {price['trade_date'].nunique()} 日 / 席位 {seat['member_key'].nunique()} 家 "
      f"[{df['trade_date'].min():%Y-%m-%d} ~ {df['trade_date'].max():%Y-%m-%d}]")

section("P1 alpha 排行(全样本,仅供参照——真正算数的是后面的样本外)")
full = seat_alpha(df).sort_values("alpha", ascending=False)
print(full.head(10).to_string())
print("\n垫底 5 家(对手盘):")
print(full.tail(5).to_string())

section("P2 alpha 排名跨年稳不稳(逐年留一,14 年样本)")
years = sorted(df["trade_date"].dt.year.unique())
ranks = {"全样本": full["alpha"].rank(ascending=False)}
for y in years:
    a = seat_alpha(df[df["trade_date"].dt.year != y], min_days=MIN_DAYS)
    if not a.empty: ranks[str(y)] = a["alpha"].rank(ascending=False)
rk = pd.DataFrame(ranks).dropna()
cor = rk.corr(method="spearman")["全样本"].drop("全样本")
print(f"共同可比席位 {len(rk)} 家;各留一口径与全样本的排名相关:")
print(f"  最低 {cor.min():.2f}(去掉 {cor.idxmin()})  最高 {cor.max():.2f}  中位 {cor.median():.2f}")
print(f"\n全样本前 5:{'、'.join(full.head(5).index)}")
for y in years[-4:]:
    if str(y) in ranks:
        print(f"  去掉 {y}:{'、'.join(ranks[str(y)].sort_values().head(5).index)}")

section("P3 滚动样本外:只用当下之前的数据选组")
cuts = [pd.Timestamp(f"{y}-01-01") for y in range(2016, 2027, 2)]
print(f"  {'训练截止':12s}{'K':>3s}{'测试N':>7s}{'corr':>8s}{'偏相关':>8s}{'t':>7s}  组")
for k in (3, 5, 8):
    for cut in cuts:
        tr = seat_alpha(df, hi=cut, min_days=120)
        if tr.empty or len(tr) < k: continue
        grp = tr.sort_values("alpha", ascending=False).head(k).index.tolist()
        te = df[df["trade_date"] >= cut]; te_main = main[main.index >= cut]
        r = power(group_signal(te, grp), te_main)
        if r:
            print(f"  <{cut:%Y-%m}   {k:>3d}{r['N']:>7d}{r['corr']:>+8.3f}"
                  f"{r['partial']:>+8.3f}{r['t']:>+7.2f}  {'、'.join(grp[:3])}"
                  + ("…" if k > 3 else ""))

section("P4 选择准则对比(同一段样本外)")
cut = pd.Timestamp("2022-01-01")
tr = seat_alpha(df, hi=cut, min_days=120)
te = df[df["trade_date"] >= cut]; te_main = main[main.index >= cut]
print(f"训练 < {cut:%Y-%m-%d},测试 ≥ 之(N≈{len(te_main)} 日)\n")
print(f"  {'准则':16s}{'K':>3s}{'corr':>9s}{'偏相关':>9s}{'t':>8s}   组")
for label, key in [("alpha 排序", "alpha"), ("总盈亏排序", "pnl"), ("beta 排序(对照)", "beta")]:
    for k in (5,):
        grp = tr.sort_values(key, ascending=False).head(k).index.tolist()
        r = power(group_signal(te, grp), te_main)
        if r: print(f"  {label:16s}{k:>3d}{r['corr']:>+9.3f}{r['partial']:>+9.3f}"
                    f"{r['t']:>+8.2f}   {'、'.join(grp[:3])}…")
r = power(group_signal(te, tr.index.tolist()), te_main)
if r: print(f"  {'全部席位(对照)':16s}{len(tr):>3d}{r['corr']:>+9.3f}{r['partial']:>+9.3f}{r['t']:>+8.2f}")
