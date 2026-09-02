# -*- coding: utf-8 -*-
"""铁矿石专属进出场研究(2026-09-02,运营者第 1 问答「立项」)。

**预注册见 `PLAN_I_ENGINE_v1.md`** —— 格子、判据、判定规则都在跑之前写死了。
本脚本照单执行,不挑格子、不调阈值。

八个格子 = 进场 {E1 现状 / E2 换散户腿 / E3 成本 / E4 成本+加仓} × 出场 {散户 / 机构}。
零假设用**最大统计量置换**(8 个格子里取最好的那个),不是单格 p。

**整条回放走引擎自己的 `replay()`**,不另写一份 —— 出场口径一分叉,研究结论
就与线上跑的不是同一套东西了(engine 里那句注释说的正是这件事)。

跑法:python research/run_i_engine.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

SIMS = 500
COST = 0.0005                     # 单边,与跟随线同规格
MIN_TRADES = 20                   # 预注册的样本门槛
I_RETAIL = ["新湖期货", "瑞达期货", "宝城期货"]   # REPORT_I_RETAIL_v1 选出的三家
rng = np.random.default_rng(20260902)

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "i_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "i_seat.csv.gz"))
H.use("I")
H.RULES["long_enabled"] = True        # 运营者已拍板,不作为变量
H.RULES["long_needs_dip"] = False
DEFAULT_SEED = list(H.RULES["retail_seed"])

mkt = H.main_series(price)
op, st = H.contract_prices(price)
mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
idx = mkt.index
groups, _log, _cuts = H.rolling_groups(seat, price, idx)
BASE_SIG = H.signal_series(seat, groups)

ENTRIES = [
    ("E1 共振·现行散户", "resonance", DEFAULT_SEED, False),
    ("E2 共振·铁矿散户", "resonance", I_RETAIL, False),
    ("E3 成本进场", "cost", DEFAULT_SEED, False),
    ("E4 成本+仍加仓", "cost", DEFAULT_SEED, True),
]
EXITS = [("散户出场", "retail"), ("机构出场", "inst")]


def build_sig(source: str, need_adding: bool, exit_mode: str) -> pd.DataFrame:
    """按格子的配置造一份信号表。RULES 在这里临时改,用完由调用方复位。"""
    H.RULES["signal_source"] = source
    H.RULES["cost_need_adding"] = need_adding
    H.RULES["exit_mode"] = exit_mode
    sig = BASE_SIG.copy()
    if source == "cost":
        sig = H.attach_cost_signal(sig, seat, mkt, groups)
    if exit_mode == "inst":
        sig = H.attach_inst_exit(sig, seat, mkt, groups)
    return sig


def run_cell(sig: pd.DataFrame, seed: list[str], exit_mode: str,
             cost: float = COST) -> dict:
    """跑一个格子,返回统计。走引擎自己的 replay,不另写一份。"""
    H.RULES["retail_seed"] = list(seed)
    H.RULES["exit_mode"] = exit_mode
    rdf, _have = H.retail_series(seat, idx)
    trades, _pos, daily = H.replay(sig, mkt, rdf, op, st)
    H.RULES["retail_seed"] = DEFAULT_SEED
    closed = [t for t in trades if t.get("exit_date")]
    if not len(daily) or daily.std() == 0:
        return {"trades": len(closed), "cum": np.nan, "sharpe": np.nan,
                "dd": np.nan, "win": np.nan, "daily": daily}
    # 成本:每笔进出各一次单边
    net = daily.copy()
    eq = (1 + net).cumprod()
    gross_cum = (float(eq.iloc[-1]) - 1) * 100
    cum = gross_cum - len(closed) * cost * 2 * 100
    rets = [t["ret_pct"] for t in closed if t.get("ret_pct") is not None]
    return {
        "trades": len(closed),
        "cum": cum,
        "sharpe": float(net.mean() / net.std() * np.sqrt(242)),
        "dd": float((eq / eq.cummax() - 1).min()) * 100,
        "win": (sum(1 for r in rets if r > 0) / len(rets) * 100) if rets else np.nan,
        "daily": net,
    }


# --------------------------------------------------------------- 基准
bench_daily = mkt["ret_open"].fillna(0.0)
bench_eq = (1 + bench_daily).cumprod()
bench = {
    "cum": (float(bench_eq.iloc[-1]) - 1) * 100,
    "sharpe": float(bench_daily.mean() / bench_daily.std() * np.sqrt(242)),
}

L = [f"铁矿石专属进出场研究(样本 {idx[0].date()} ~ {idx[-1].date()},{len(idx)} 个交易日)", ""]
L.append("预注册 `PLAN_I_ENGINE_v1.md`,格子/判据/判定规则跑前写死。")
L.append(f"基准(一律做多):累计 {bench['cum']:+.1f}%、夏普 {bench['sharpe']:.2f}。")
L.append("")
L.append("## 一、八个格子(全部报告,不挑)")
L.append("")
L.append(f"{'格子':<26}{'笔数':>5}{'累计%':>9}{'夏普':>7}{'回撤%':>8}{'胜率%':>7}")
L.append("-" * 64)

CELLS = {}
for ename, source, seed, adding in ENTRIES:
    for xname, xmode in EXITS:
        sig = build_sig(source, adding, xmode)
        r = run_cell(sig, seed, xmode)
        key = f"{ename} × {xname}"
        CELLS[key] = {**r, "source": source, "seed": seed, "adding": adding, "xmode": xmode}
        thin = "  ← 笔数不足" if r["trades"] < MIN_TRADES else ""
        L.append(f"{key:<26}{r['trades']:>5}{r['cum']:>+9.1f}{r['sharpe']:>7.2f}"
                 f"{r['dd']:>+8.1f}{r['win']:>7.1f}{thin}")
L.append("")

best_key = max(CELLS, key=lambda k: CELLS[k]["sharpe"] if np.isfinite(CELLS[k]["sharpe"]) else -9)
best = CELLS[best_key]
L.append(f"**八个里最好的是「{best_key}」**(夏普 {best['sharpe']:.2f})。")
L.append("下面所有检验都对它 —— 但检验的零假设是「**八选一**」,不是「这一格」。")
L.append("")

# --------------------------------------------------------------- 最大统计量置换
L.append("## 二、零假设:最大统计量置换(500 次循环移位)")
L.append("")
L.append("把信号表整体循环移位:进出场的形状与频率全部保留,只打乱它与行情的对齐。")
L.append("每次移位把八个格子都跑一遍,**只取当次最好的那个夏普** —— 这才是")
L.append("「我从八个里挑了最好的」这件事本身的零分布。")
L.append("")
shifts = rng.integers(1, len(idx), size=SIMS)
max_sims = np.full(SIMS, -np.inf)
for ename, source, seed, adding in ENTRIES:
    for xname, xmode in EXITS:
        sig = build_sig(source, adding, xmode)
        # **逐列 roll,不要 `np.roll(sig.values)`** —— attach_cost_signal /
        # attach_inst_exit 会加进布尔与字符串列,整表取 .values 会退化成 object 数组,
        # replay 里的比较当场炸(TypeError: unorderable types)。逐列保留 dtype。
        cols = {c: sig[c].values for c in sig.columns}
        for k, sh in enumerate(shifts):
            alt = pd.DataFrame({c: np.roll(v, sh) for c, v in cols.items()},
                               index=sig.index)
            s = run_cell(alt, seed, xmode)["sharpe"]
            if np.isfinite(s) and s > max_sims[k]:
                max_sims[k] = s
max_sims = max_sims[np.isfinite(max_sims)]
p_max = float((np.sum(max_sims >= best["sharpe"]) + 1) / (len(max_sims) + 1))
L.append(f"  移位后「八格里最好者」夏普:中位 {np.median(max_sims):.2f}、"
         f"95 分位 {np.percentile(max_sims, 95):.2f}、最大 {np.max(max_sims):.2f}")
L.append(f"  实测 {best['sharpe']:.2f} → **p_max = {p_max:.4f}**"
         f"  {'过' if p_max < 0.05 else '**不过**'}")
L.append("")

# --------------------------------------------------------------- 走前检验
L.append("## 三、走前检验(**两折,信息量极低,报出来不是当证据**)")
L.append("")
years = sorted({d.year for d in idx})
wf_rows, wf_daily = [], []
for y in years[1:]:
    train_end = pd.Timestamp(f"{y}-01-01")
    pick, pick_sh = None, -np.inf
    for k, c in CELLS.items():
        d_ = c["daily"]
        d_ = d_[d_.index < train_end]
        if len(d_) < 120 or d_.std() == 0:
            continue
        sh = float(d_.mean() / d_.std() * np.sqrt(242))
        if sh > pick_sh:
            pick, pick_sh = k, sh
    if pick is None:
        continue
    test = CELLS[pick]["daily"]
    test = test[(test.index >= train_end) & (test.index < pd.Timestamp(f"{y+1}-01-01"))]
    if not len(test):
        continue
    wf_rows.append((y, pick, pick_sh, (float(np.prod(1 + test)) - 1) * 100))
    wf_daily.append(test)
if wf_rows:
    L.append(f"{'年':<6}{'挑中':<26}{'训练夏普':>9}{'当年实得%':>11}")
    L.append("-" * 54)
    for y, pk, sh, r in wf_rows:
        L.append(f"{y:<6}{pk:<26}{sh:>9.2f}{r:>+11.1f}")
    wf = pd.concat(wf_daily).sort_index()
    wf_cum = (float(np.prod(1 + wf)) - 1) * 100
    L.append("-" * 54)
    L.append(f"  走前合计 {wf_cum:+.1f}%,{len(wf_rows)} 折,"
             f"{'两折都为正' if all(r > 0 for *_x, r in wf_rows) else '**并非两折都正**'}")
    wf_all_positive = all(r > 0 for *_x, r in wf_rows)
else:
    L.append("  排不出走前窗口。")
    wf_all_positive = False
L.append("")

# --------------------------------------------------------------- 稳健性
L.append("## 四、最好那格的稳健性")
L.append("")
d_best = best["daily"]
yearly = {y: (float(np.prod(1 + g)) - 1) * 100 for y, g in d_best.groupby(d_best.index.year)}
L.append("  逐年:" + "  ".join(f"{y} {v:+.1f}%" for y, v in sorted(yearly.items())))
half = d_best.index[len(d_best) // 2]
for lab, seg in (("前半", d_best[d_best.index <= half]), ("后半", d_best[d_best.index > half])):
    if len(seg) > 20 and seg.std() > 0:
        eq = (1 + seg).cumprod()
        L.append(f"  {lab}:累计 {(float(eq.iloc[-1])-1)*100:+.1f}%、"
                 f"夏普 {float(seg.mean()/seg.std()*np.sqrt(242)):.2f}")
L.append("")

# --------------------------------------------------------------- 判定
L.append("## 五、按预注册的判定规则")
L.append("")
c_a = p_max < 0.05
c_b = wf_all_positive
c_c = best["trades"] >= MIN_TRADES
L.append(f"  (a) p_max < 0.05        {'✓' if c_a else '✗'}  (p_max = {p_max:.4f})")
L.append(f"  (b) 走前两折都为正      {'✓' if c_b else '✗'}")
L.append(f"  (c) 笔数 >= {MIN_TRADES}          {'✓' if c_c else '✗'}  ({best['trades']} 笔)")
L.append("")
if c_a and c_b and c_c:
    L.append(f"**三条全过 → 建议把铁矿石主引擎换成「{best_key}」。**")
else:
    L.append("**没有全过 → 结论是「没有可换的」。** 现状保留,页面继续挂风险警示。")
    L.append("预注册里写死了不设「差一点就过」的通融口子:三年样本 + 八个格子,")
    L.append("松一寸就是给过拟合发许可证。")
io.open(OUT / "i_engine.txt", "w", encoding="utf-8").write("\n".join(L))
print("done ->", OUT / "i_engine.txt")
