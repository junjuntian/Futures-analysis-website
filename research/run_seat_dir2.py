"""第二把尺:把「大且赚钱」组灌进现行引擎全流程回放(PLAN_JM_SEAT_DIR_v1)。
跑法:仓库根目录 python research/run_seat_dir2.py JM
对照 run_fixed_groups.py 同口径;B=滚动大盈组(与 alpha 组同切点扩窗)。"""
import sys, pathlib
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H

code = sys.argv[1] if len(sys.argv) > 1 else "JM"
D = pathlib.Path(__file__).resolve().parent / "data"
price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
v = H.use(code); H.CURRENT = {"code": code, **v}
H.RULES["fixed_members"] = None
mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
rdf, _ = H.retail_series(seat, mkt.index)
roll_a, _, cuts = H.rolling_groups(seat, price, mkt.index)
cuts_ts = [pd.Timestamp(c) for c in cuts if pd.Timestamp(c) <= mkt.index[-1]]


def big_pnl_top(hi, k):
    d = seat[seat["trade_date"] < hi].merge(
        price[["contract", "trade_date", "settle"]], on=["contract", "trade_date"], how="inner")
    d = d.sort_values(["member_key", "contract", "trade_date"])
    g = d.groupby(["member_key", "contract"])
    d["prev_net"] = g["net"].shift(); d["prev_settle"] = g["settle"].shift()
    gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
    d = d[d["prev_net"].notna() & (gap <= 5)].copy()
    d["dpx"] = (d["settle"] - d["prev_settle"]) * H.RULES["multiplier"]
    grp = d.groupby("member_key")
    t = pd.DataFrame({"pnl": grp.apply(lambda s: (s["dpx"] * s["prev_net"]).sum(), include_groups=False),
                      "avg_abs": grp["prev_net"].apply(lambda s: s.abs().mean()),
                      "days": grp["trade_date"].nunique()})
    t = t[t["days"] >= H.RULES["member_min_days"]]
    if len(t) < k:
        return None
    big = t[t["avg_abs"] >= t["avg_abs"].median()]
    return tuple(big.sort_values("pnl", ascending=False).head(k).index) if len(big) >= k else None


picks = {c: big_pnl_top(c, H.RULES["group_k"]) for c in cuts_ts}
roll_b = pd.Series(index=mkt.index, dtype=object)
for d in mkt.index:
    valid = [c for c in cuts_ts if c <= d and picks[c]]
    roll_b[d] = picks[valid[-1]] if valid else None
fixed_2408 = big_pnl_top(pd.Timestamp("2024-08-01"), H.RULES["group_k"])


def run(groups):
    sig = H.signal_series(seat, groups)
    if H.RULES["signal_source"] == "cost":
        sig = H.attach_cost_signal(sig, seat, mkt, groups)
    if H.RULES["exit_mode"] == "inst":
        sig = H.attach_inst_exit(sig, seat, mkt, groups)
    if H.RULES["long_mode"] == "unload_bounce":
        sig = H.attach_bounce_long(sig, seat, mkt, groups)
    trades, pos, daily = H.replay(sig, mkt, rdf, op, st)
    out = {}
    for name, lo in (("全样本", None), ("近一年", mkt.index[-1] - pd.Timedelta(days=365)),
                     ("2026", pd.Timestamp("2026-01-01"))):
        dd = daily if lo is None else daily[daily.index >= lo]
        tr = [t for t in trades if lo is None or (t["exit_date"] and pd.Timestamp(t["exit_date"]) >= lo)]
        p = H._perf(dd)
        w = [t for t in tr if t["ret_pct"] > 0]
        out[name] = (p["cum_pct"], p["sharpe"], p["max_dd_pct"], len(tr),
                     round(len(w) / len(tr) * 100) if tr else None)
    return out


res = {"A 现行滚动alpha组": run(roll_a), "B 滚动大盈组": run(roll_b)}
if fixed_2408:
    res["C 固定大盈@2024-08"] = run(pd.Series([fixed_2408] * len(mkt.index), index=mkt.index, dtype=object))
print(f"{v['name']} 现行策略回放  signal_source={H.RULES['signal_source']} long={H.RULES['long_enabled']} exit={H.RULES['exit_mode']}")
print("B 当前名单:", "、".join(roll_b.dropna().iloc[-1]))
if fixed_2408:
    print("C 名单:", "、".join(fixed_2408))
for per in ("近一年", "2026", "全样本"):
    print(f"\n=== {per} ===")
    for k, r in res.items():
        c, s, d, n, w = r[per]
        print(f"  {k:20s} 累计{c:>+8.1f}%  夏普{(s if s is not None else float('nan')):>6.2f}  回撤{d:>7.1f}%  笔{n:>4d}  胜{(w if w is not None else float('nan')):>4.0f}%")
