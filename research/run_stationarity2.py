"""补充检验:价格−机构成本的协整性 + 前后半表现差异的显著性。
跑法:python research/run_stationarity2.py"""
import sys, pathlib, io
import numpy as np, pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine")); sys.path.insert(0, str(ROOT / "research"))
import hog_money as H
import campaign as C
import statlib as S

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
L = ["补充检验 A:价格 与 机构成本 是否协整(伪回归的关键一问)", ""]
L.append("成本进场规则 = 「价格不劣于机构成本才进」。价格与成本各自都是 I(1)(随机游走),")
L.append("**只有两者协整(差值平稳)时,拿它们比大小才有意义**;否则差值会无限发散,")
L.append("规则等于在拿两条各走各路的曲线做比较 —— 那就是典型的伪回归。")
L.append("")
L.append(f"{'品种':<8}{'序列':<22}{'N':>6}{'ADF':>9}{'KPSS':>8}  判定")
L.append("-" * 72)

rets = {}
for code in ("LH", "JM", "FG", "JD", "SA"):
    price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
    H.use(code)
    rs = pd.Timestamp(H.RULES["replay_start"])
    if code == "JD":
        ok = price.dropna(subset=["open_interest"])["trade_date"].unique()
        price, seat = price[price["trade_date"].isin(ok)], seat[seat["trade_date"].isin(ok)]
    price, seat = price[price["trade_date"] >= rs], seat[seat["trade_date"] >= rs]
    mkt = H.main_series(price); mkt = mkt[mkt.index >= rs]
    op, st = H.contract_prices(price)
    groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        groups, log = H.apply_group_overrides(groups, log, cuts, H.RULES["group_overrides"], seat, price)
    sig = H.signal_series(seat, groups)
    cc = H.inst_cost_series(sig, mkt, groups)
    px, cost = mkt["settle"], cc["cost"].reindex(mkt.index)
    for tag, ser in ((f"主力结算价", px), (f"机构成本", cost), (f"**价格−成本**", (px - cost))):
        x = pd.Series(ser).dropna()
        if len(x) < 60:
            continue
        a, k = S.adf(x.values), S.kpss(x.values)
        L.append(f"{code:<8}{tag:<22}{len(x):>6}{a['stat']:>9.2f}{k['stat']:>8.3f}  {S.verdict_pair(a,k)}")
    L.append("")
    # 顺带收集日收益,给检验 B 用
    if H.RULES.get("strategy") == "campaign":
        rets[f"{code} 主引擎"] = pd.Series(C.run(seat, mkt, op, st, list(groups.dropna().iloc[-1]), H.RULES)["daily"]).dropna()
    else:
        s2 = sig
        if H.RULES.get("signal_source") == "cost":
            s2 = H.attach_cost_signal(sig, seat, mkt, groups)
        if H.RULES.get("exit_mode") == "inst":
            s2 = H.attach_inst_exit(s2, seat, mkt, groups)
        rdf, _ = H.retail_series(seat, mkt.index)
        rets[f"{code} 主引擎"] = pd.Series(H.replay(s2, mkt, rdf, op, st)[2]).dropna()

L.append("")
L.append("补充检验 B:前后半样本的表现差异,是随机波动还是真的变了?")
L.append("(两样本均值差的 t 检验;|t|<2 = 差异在噪音范围内,不能说策略变了)")
L.append("")
L.append(f"{'策略':<20}{'前半夏普':>9}{'后半夏普':>9}{'均值差 t':>10}  结论")
L.append("-" * 66)
for name, r in rets.items():
    h = len(r) // 2
    a_, b_ = r.iloc[:h], r.iloc[h:]
    def sh(x):
        return float(x.mean() / x.std() * np.sqrt(242)) if x.std() > 0 else np.nan
    se = np.sqrt(a_.var(ddof=1) / len(a_) + b_.var(ddof=1) / len(b_))
    t = float((b_.mean() - a_.mean()) / se) if se > 0 else np.nan
    concl = "差异不显著(同一策略)" if abs(t) < 2 else "**差异显著(表现变了)**"
    L.append(f"{name:<20}{sh(a_):>9.2f}{sh(b_):>9.2f}{t:>10.2f}  {concl}")
io.open(OUT / "stationarity2.txt", "w", encoding="utf-8").write("\n".join(L))
