"""纯碱主引擎「后半样本衰减」的诊断:逐年拆开 + 成本尺子有没有在漂。

跑法:`python research/run_sa_decay.py`(产出 `research/out/sa_decay.txt`)

缘起:`REPORT_STATIONARITY_v1` 量出纯碱主引擎前半夏普 1.08 → 后半 −0.16
(t=−1.64,不显著),列为「衰减观察,下次复盘先看」。运营者 2026-09-03 问
「这个要怎么改」。**改之前先答四问**:

1. 后半到底几笔几天?t 不显著是「没衰减」还是「样本不够」?
2. 是不是市况?—— 同期恒定满仓做空、同期流量臂(旧信号)各是多少;
3. 是不是 DEC-113 上线时就写明的那件事(2023/2024 成本臂让利)换个切法?
4. 成本这把尺子本身在不在漂?「价格−成本」逐年的平稳性与位置。

**注意**:这里比的是「成本臂 vs 流量臂」两条**都在同一份样本上**的曲线,
不是在挑最优(research/PITFALLS 第 5 条)—— 结论只用来定位衰减的来源,
任何参数改动都要另立预注册。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "research"))
import hog_money as H  # noqa: E402
import statlib as S  # noqa: E402

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)
YR = 242
L: list[str] = []


def load(code: str):
    price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
    rs = pd.Timestamp(H.RULES["replay_start"])
    price, seat = price[price["trade_date"] >= rs], seat[seat["trade_date"] >= rs]
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= rs]
    return price, seat, mkt


def arm(code: str, source: str):
    """跑一条臂。source='cost' 现行 / 'flow' 旧信号。"""
    H.use(code)
    # `RULES` 是全局的,而 `entry_exit_signals` 按 signal_source 取不同的列 ——
    # 不显式改这一行,流量臂会去读 cost_z 直接 KeyError
    # (research/PITFALLS 第 2 条「席位组键名」的同款:全局状态要显式设)。
    H.RULES["signal_source"] = source
    price, seat, mkt = load(code)
    op, st = H.contract_prices(price)
    groups, log, cuts = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        groups, log = H.apply_group_overrides(groups, log, cuts,
                                              H.RULES["group_overrides"], seat, price)
    sig = H.signal_series(seat, groups)
    rdf, _ = H.retail_series(seat, mkt.index)
    if source == "cost":
        sig = H.attach_cost_signal(sig, seat, mkt, groups)
    closed, _open, daily = H.replay(sig, mkt, rdf, op, st)[:3]
    return pd.Series(daily).dropna(), closed, mkt


def sharpe(x):
    return float(x.mean() / x.std() * np.sqrt(YR)) if len(x) > 2 and x.std() > 0 else np.nan


def cum(x):
    return float((1 + x).prod() - 1) * 100


CODE = "SA"
cost_d, cost_t, mkt = arm(CODE, "cost")
flow_d, flow_t, _ = arm(CODE, "flow")
bench_d = -mkt["ret_open"].fillna(0.0).reindex(cost_d.index).fillna(0.0)

L += [f"# 纯碱主引擎衰减诊断(数据至 {cost_d.index[-1]:%Y-%m-%d})", "",
      f"样本 {cost_d.index[0]:%Y-%m-%d} ~ {cost_d.index[-1]:%Y-%m-%d},"
      f"{len(cost_d)} 个交易日;成本臂 {len(cost_t)} 笔 / 流量臂 {len(flow_t)} 笔", "",
      "## 一、逐年(成本臂=现行,流量臂=DEC-113 之前的旧信号,基准=恒定满仓做空)", "",
      f"{'年':<6}{'成本%':>9}{'成本夏普':>9}{'笔':>5}{'流量%':>9}{'流量夏普':>9}{'笔':>5}{'基准%':>9}"]
for y in sorted({d.year for d in cost_d.index}):
    cg = cost_d[cost_d.index.year == y]
    fg = flow_d[flow_d.index.year == y]
    bg = bench_d[bench_d.index.year == y]
    if len(cg) < 40:
        continue
    nc = sum(1 for t in cost_t if pd.Timestamp(t["entry_date"]).year == y)
    nf = sum(1 for t in flow_t if pd.Timestamp(t["entry_date"]).year == y)
    L.append(f"{y:<6}{cum(cg):>9.1f}{sharpe(cg):>9.2f}{nc:>5}"
             f"{cum(fg):>9.1f}{sharpe(fg):>9.2f}{nf:>5}{cum(bg):>9.1f}")

h = len(cost_d) // 2
L += ["", f"## 二、前后半(切点 {cost_d.index[h]:%Y-%m-%d},与 run_stationarity2 同法)", ""]
for name, d, tr in (("成本臂", cost_d, cost_t), ("流量臂", flow_d, flow_t),
                    ("基准", bench_d, None)):
    a_, b_ = d.iloc[:h], d.iloc[h:]
    se = np.sqrt(a_.var(ddof=1) / len(a_) + b_.var(ddof=1) / len(b_))
    t = float((b_.mean() - a_.mean()) / se) if se > 0 else np.nan
    na = sum(1 for x in (tr or []) if pd.Timestamp(x["entry_date"]) < d.index[h])
    nb = (len(tr) - na) if tr else 0
    L.append(f"{name:<6} 前半夏普 {sharpe(a_):+5.2f}(笔 {na:>3}) → 后半 {sharpe(b_):+5.2f}"
             f"(笔 {nb:>3})  日收益均值差 t={t:+.2f}"
             f"  累计 {cum(a_):+7.1f}% → {cum(b_):+7.1f}%")

L += ["", "## 三、逐笔口径的前后半差(比日收益更贴「这条规则」)", ""]
cut = cost_d.index[h]
for name, tr in (("成本臂", cost_t), ("流量臂", flow_t)):
    a = np.array([x["ret_pct"] for x in tr if pd.Timestamp(x["entry_date"]) < cut])
    b = np.array([x["ret_pct"] for x in tr if pd.Timestamp(x["entry_date"]) >= cut])
    if len(a) < 2 or len(b) < 2:
        continue
    se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    t = (b.mean() - a.mean()) / se if se > 0 else np.nan
    L.append(f"{name:<6} 前半 {len(a):>3} 笔 均值 {a.mean():+.2f}%/胜 {(a > 0).mean() * 100:4.1f}%"
             f" → 后半 {len(b):>3} 笔 均值 {b.mean():+.2f}%/胜 {(b > 0).mean() * 100:4.1f}%"
             f"  均值差 t={t:+.2f}")

L += ["", "## 四、成本这把尺子在不在漂:「价格−成本」的平稳性与位置", "",
      "(成本进场是**拿两个水平量比大小**,只有协整=差值平稳时才有意义;",
      " REPORT_STATIONARITY_v1 给平台立的规矩就是这一条)", "",
      f"{'品种/年':<10}{'N':>6}{'ADF':>8}{'KPSS':>8}{'价−成本均值%':>13}{'中位%':>8}{'≥0占比':>8}"]
for code in ("SA", "JD"):          # JD 是协整成立的对照组
    H.use(code)
    price, seat, m2 = load(code)
    g2, _l, _c = H.rolling_groups(seat, price, m2.index)
    cc = H.inst_cost_series(H.signal_series(seat, g2), m2, g2)
    px = m2["settle"]
    gap = (px - cc["cost"].reindex(m2.index)).dropna()
    rel = (gap / px.reindex(gap.index) * 100).dropna()
    a, k = S.adf(gap.values), S.kpss(gap.values)
    L.append(f"{code + ' 全样本':<10}{len(gap):>6}{a['stat']:>8.2f}{k['stat']:>8.3f}"
             f"{rel.mean():>13.2f}{rel.median():>8.2f}{(rel >= 0).mean() * 100:>7.0f}%")
    for y, g in gap.groupby(gap.index.year):
        if len(g) < 60:
            continue
        r = rel[rel.index.year == y]
        a, k = S.adf(g.values), S.kpss(g.values)
        L.append(f"{'  ' + str(y):<10}{len(g):>6}{a['stat']:>8.2f}{k['stat']:>8.3f}"
                 f"{r.mean():>13.2f}{r.median():>8.2f}{(r >= 0).mean() * 100:>7.0f}%")
    L.append("")

(OUT / "sa_decay.txt").write_text("\n".join(L), encoding="utf-8")
print("\n".join(L))
