# -*- coding: utf-8 -*-
"""燃油 FU 建引擎 + 两个可换位置的席位侦察(预注册 PLAN_FU_ENGINE_v1)。

运营者点名:聪明钱 东证/中金财富/国金/东吴,散户 东方财富/中银;
东证与中金**钉死不换**,另两家要扫更好的候选给他判断。

**只跑事前写死的两格**(默认共振进场 / 成本进场),不搜参数组合。
扫描完必须算「纯噪音下取最大值的期望」——榜首低于它就是整张榜没信息。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

D = Path("research/data")
SMART = ["东证期货", "中金财富", "国金期货", "东吴期货"]
RETAIL = ["东方财富", "中银期货"]
FIXED = ("东证期货", "中金财富")          # 运营者钉死,不参与扫描
SWAPPABLE = ("国金期货", "东吴期货")
# 中金财富 2022-10-26 才首次出现在 FU —— 之前「四家」其实是三家。
FOUR_SEATS_FROM = "2022-11-01"
MIN_DAYS = 300                            # 侦察池的样本下限
rng = np.random.default_rng(20260907)

BASE = {
    "name": "燃油 FU", "unit": "元/吨", "multiplier": 10.0,
    "replay_start": "2018-08-01",
    "long_enabled": True, "long_needs_dip": False,
    "fixed_members": list(SMART),
    "retail_seed": list(RETAIL), "retail_panel": list(RETAIL),
    "out": "fu_signals.json", "backtest": "(待定)",
}


def build():
    price = H.clean_price(pd.read_csv(D / "fu_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / "fu_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    idx = mkt.index
    g, log, cuts = H.fixed_groups(seat, idx) if hasattr(H, "fixed_groups") else (None, None, None)
    return price, seat, mkt, op, st, idx


def arm(tag: str, cfg: dict):
    """跑一格。回 (trades, daily, groups, idx, mkt)。"""
    H.VARIETIES["FU"] = dict(BASE, **cfg)
    H.use("FU")
    price = H.clean_price(pd.read_csv(D / "fu_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / "fu_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    idx = mkt.index
    g, log, cuts = H.rolling_groups(seat, price, idx)
    rdf, _ = H.retail_series(seat, idx)
    raw = H.signal_series(seat, g)
    sig = raw
    if H.RULES["signal_source"] == "cost":
        sig = H.attach_cost_signal(raw, seat, mkt, g)
    if H.RULES.get("exit_mode") == "inst":
        sig = H.attach_inst_exit(sig, seat, mkt, g)
    tr, pos, dl = H.replay(sig, mkt, rdf, op, st)
    return tr, dl, g, idx, mkt


def perf(tr, dl, lo=None):
    d = dl if lo is None else dl[dl.index >= pd.Timestamp(lo)]
    d = d.dropna()
    t = tr if lo is None else [x for x in tr if x["entry_date"] >= lo]
    if len(d) < 5:
        return None
    eq = (1 + d).cumprod()
    r = np.array([x["ret_pct"] for x in t], dtype=float) if t else np.array([0.0])
    return {"n": len(t), "cum": (float(eq.iloc[-1]) - 1) * 100,
            "sharpe": float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else float("nan"),
            "dd": float((eq / eq.cummax() - 1).min()) * 100,
            "win": float((r > 0).mean()) * 100 if t else float("nan"),
            "avg": float(r.mean()) if t else float("nan")}


def line(tag, p):
    if p is None:
        print(f"  {tag:<26}样本不足")
        return
    print(f"  {tag:<26}{p['n']:>4} 笔{p['cum']:>+9.1f}%{p['sharpe']:>7.2f}"
          f"{p['dd']:>8.1f}%{p['win']:>7.1f}%{p['avg']:>+8.2f}%")


print("=" * 96)
print("燃油 FU 建引擎(PLAN_FU_ENGINE_v1)")

# ---------------- 引擎两格 ----------------
print("\n[引擎] 运营者点名的四家 + 两家散户,只跑事前写死的两格")
print(f"  {'格':<26}{'笔数':>6}{'累计':>10}{'夏普':>7}{'回撤':>9}{'胜率':>7}{'单笔均值':>9}")
arms = {}
for tag, cfg in (("A 默认(共振进场·代表格)", {}),
                 ("B 成本进场(玻纯同款)", {"signal_source": "cost"})):
    tr, dl, g, idx, mkt = arm(tag, cfg)
    arms[tag] = (tr, dl)
    line(tag, perf(tr, dl))
print(f"\n  —— 只看四家都在的区间({FOUR_SEATS_FROM} 起,中金财富 2022-10 才首见)——")
for tag, (tr, dl) in arms.items():
    line(tag, perf(tr, dl, FOUR_SEATS_FROM))

# 基准:主力买入持有
_, _, g, idx, mkt = arm("A", {})
bh = (1 + mkt["ret_open"].fillna(0)).cumprod()
print(f"\n  基准(主力买入持有)累计 {(float(bh.iloc[-1]) - 1) * 100:+.1f}%")

# 逐年(代表格)
tr_a, dl_a = arms["A 默认(共振进场·代表格)"]
ya = ((1 + dl_a.fillna(0)).groupby(dl_a.index.year).prod() - 1) * 100
print("\n  代表格 A 逐年:", "  ".join(f"{int(y)}:{v:+.0f}%" for y, v in ya.items()))

# ---------------- 席位侦察 ----------------
print("\n" + "=" * 96)
print("[侦察] 只扫可换的两个位置。东证与中金财富按运营者要求钉死,不参与。")
seat = H.clean_seat(pd.read_csv(D / "fu_seat.csv.gz"))
price = H.clean_price(pd.read_csv(D / "fu_price.csv.gz"))
settle = price.set_index(["contract", "trade_date"])["settle"].sort_index()
MULT = 10.0

d = seat.merge(price[["contract", "trade_date", "settle"]], on=["contract", "trade_date"], how="inner")
d = d.sort_values(["member_key", "contract", "trade_date"])
gb = d.groupby(["member_key", "contract"], sort=False)
d["prev_net"] = gb["net"].shift()
d["prev_settle"] = gb["settle"].shift()
gap = (d["trade_date"] - gb["trade_date"].shift()).dt.days
d = d[d["prev_net"].notna() & (gap <= 5)].copy()
d["pnl"] = (d["settle"] - d["prev_settle"]) * d["prev_net"] * MULT
d["dpx"] = (d["settle"] - d["prev_settle"]) * MULT
last = d["trade_date"].max()


def alpha_of(frame: pd.DataFrame) -> pd.DataFrame:
    """择时收益 = 实际盈亏 − 恒定仓位能赚到的钱(与 run_lh_phase1.seat_alpha 同法)。"""
    out = frame.groupby("member_key").agg(pnl=("pnl", "sum"), days=("trade_date", "nunique"))
    out["beta"] = frame.groupby("member_key").apply(
        lambda s: float((s["dpx"] * s["prev_net"].mean()).sum()), include_groups=False)
    out["alpha"] = out["pnl"] - out["beta"]
    return out


full = alpha_of(d)
y1 = alpha_of(d[d["trade_date"] >= last - pd.Timedelta(days=365)])
y2 = alpha_of(d[d["trade_date"] >= last - pd.Timedelta(days=730)])
med = d.groupby("member_key")["net"].apply(lambda s: float(s.abs().median())).rename("中位净持仓")
alldays = seat["trade_date"].nunique()
onb = seat.groupby("member_key")["trade_date"].nunique().rename("上榜天数")
# 最赚 5 天占比:形态判据(DEC-195 用它否掉过招商/中信)
top5 = d.groupby("member_key").apply(
    lambda s: (float(np.sort(s.groupby("trade_date")["pnl"].sum().values)[-5:].sum())
               / float(s["pnl"].sum()) * 100) if abs(float(s["pnl"].sum())) > 1e6 else np.nan,
    include_groups=False).rename("最赚5天占比")

t = full[["alpha", "pnl"]].join(onb).join(med).join(top5)
t["近一年alpha"] = y1["alpha"]
t["近两年alpha"] = y2["alpha"]
pool = t[t["上榜天数"] >= MIN_DAYS].copy()
pool = pool.drop(index=[m for m in FIXED if m in pool.index], errors="ignore")
print(f"  池子:上榜 ≥{MIN_DAYS} 天的 {len(pool)} 家(全部 {len(t)} 家;已剔除钉死的 {list(FIXED)})")

pool = pool.sort_values("近两年alpha", ascending=False)
print("\n  按**近两年择时收益**排序的前 12 家:")
print(f"    {'席位':<12}{'近两年α':>10}{'近一年α':>10}{'全样本α':>10}"
      f"{'上榜率':>8}{'中位持仓':>10}{'最赚5天':>9}")
for m, r in pool.head(12).iterrows():
    print(f"    {m:<12}{r['近两年alpha'] / 1e8:>+9.2f}亿{r['近一年alpha'] / 1e8:>+9.2f}亿"
          f"{r['alpha'] / 1e8:>+9.2f}亿{r['上榜天数'] / alldays:>8.0%}"
          f"{r['中位净持仓']:>10,.0f}{r['最赚5天占比']:>8.0f}%")

print(f"\n  运营者点名的这两家现在排第几(共 {len(pool)} 家):")
order = list(pool.index)
for m in SWAPPABLE:
    if m in pool.index:
        r = pool.loc[m]
        print(f"    {m:<12}第 {order.index(m) + 1} 名  近两年 {r['近两年alpha'] / 1e8:+.2f}亿"
              f"  近一年 {r['近一年alpha'] / 1e8:+.2f}亿  上榜率 {r['上榜天数'] / alldays:.0%}"
              f"  中位持仓 {r['中位净持仓']:,.0f}  最赚5天 {r['最赚5天占比']:.0f}%")
    else:
        r = t.loc[m] if m in t.index else None
        print(f"    {m:<12}**不在池子里**(上榜 {int(r['上榜天数']) if r is not None else 0} 天 "
              f"< {MIN_DAYS});近两年 α "
              f"{(r['近两年alpha'] / 1e8) if r is not None and pd.notna(r['近两年alpha']) else float('nan'):+.2f}亿")
        if m in seat["member_key"].values:
            print(f"      末次上榜 {seat[seat['member_key'] == m]['trade_date'].max().date()}")

# ---------------- 噪音判据 ----------------
print("\n[噪音判据] 这张榜到底有没有信息")
a = pool["近两年alpha"].dropna().values / 1e8
mu, sd = float(a.mean()), float(a.std(ddof=1))
sims = rng.normal(mu, sd, size=(20000, len(a))).max(axis=1)
print(f"  {len(a)} 家近两年 α:均值 {mu:+.2f}亿  标准差 {sd:.2f}  实际最高 {a.max():+.2f}亿")
print(f"  若全是同分布噪音,取最大值的期望 = {sims.mean():+.2f}亿"
      f"(90% 区间 {np.percentile(sims, 5):+.2f} ~ {np.percentile(sims, 95):+.2f})")
pct = float((sims < a.max()).mean() * 100)
print(f"  实际最高落在零分布第 {pct:.0f} 百分位 → "
      f"{'**榜首连噪音都不如,整张榜没有信息**' if pct < 50 else '榜首高于噪音期望,值得看'}")
print(f"  在 {len(a)} 家里挑最高,单家要压住多重比较需要 p < {0.05 / len(a):.5f}")
