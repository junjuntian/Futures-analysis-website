"""出场规则体检 + 「跟随机构一轮」出场实验(预注册,2026-08-23)。

## 起因

焦煤 2026-06 起机构全面转多、8 月主升 +23%;回放发现即便开了做多,现行信号也
只吃到一小口 —— 成本信号 8/4 被 6% 止损打出去后价格跑离成本再没进;流量信号
8/10 进 8/17 就被「临近交割」强平。运营者的判断:**出场规则有问题**。
现行出场四件套(散户翻向 / 止损 6% / 持满 40 / 临近交割)全是短线逻辑,
拿不住一个机构用两个月建起来的仓。

## 第一部分:体检(只量,不改)

对每个品种的现行成交单逐笔问三个问题:
  1. 出场那天机构那一轮还活着吗(方向没翻、卸仓 ≤30%)?
  2. 如果活着,从我们出场到那一轮真正结束(翻向/卸仓>30%/最多再 60 日),
     **沿着我们原方向**还走了多少?—— 这就是「留在桌上的钱」;
  3. 持仓期间最大浮盈(MFE)与实际落袋差多少?
出场原因分布也一起数。

## 第二部分:实验(预注册)

进场**不动**(各品种沿用生产配置),只换出场:
  A 现行:散户翻向 / 止损 6% / 持满 40 / 临近交割
  B 跟随一轮:**机构翻向或卸仓 >30% 就走**;止损 6% 与临近交割保留;
    **关掉散户翻向、关掉 40 日持满**(持满交给交割纪律兜底)
  C 两者都要:A 的四件套 + B 的机构出场,谁先到算谁(= 更早走)

判据(跑之前写死):B 夏普赢 A ≥ 3/5 品种,且逐年 walk-forward 选臂不输一直 A;
最大回撤不许恶化超过 10 个百分点。C 只作参照。
**REPORT_EXIT_UNLOAD_v1 在流量进场上把「卸仓当出场」否了 10 个组合里 9 个;
这次不同的是三个品种进场已换成成本信号(早进、站机构这边),持仓逻辑该重验。**
"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402

UMAX = 0.30


def sharpe(d):
    d = d.fillna(0)
    return float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan


def cum(d):
    return float((1 + d.fillna(0)).prod() - 1) * 100


def yearly(d):
    return {y: cum(d[d.index.year == y]) for y in sorted({x.year for x in d.index})}


def prod_sig(code, sig, mkt, seat, groups):
    """按生产配置把 sig 准备好(cost 品种挂 cost_z)。R.load 钉了 resonance,这里恢复。"""
    v = H.use(code)
    H.CURRENT = {"code": code, **v}
    if H.RULES["signal_source"] == "cost":
        return H.attach_cost_signal(sig, seat, mkt, groups)
    return sig


def campaign_exit_flags(sig, mkt, groups, unload):
    """机构那一轮结束的日子:方向翻转/归零,或卸仓 >UMAX。掉榜日不发信号。"""
    cc = H.inst_cost_series(sig, mkt, groups)
    side = cc["side"].reindex(mkt.index)
    prev = side.shift(1)
    flip = (side.notna() & prev.notna() & (side != prev))
    unl = unload.reindex(mkt.index)
    return (flip | (unl > UMAX)).fillna(False), cc


def checkup(code, trades, cc, unload, mkt):
    """体检:出场时机构一轮是否还活着;留在桌上的钱;MFE 与落袋差。"""
    idx = mkt.index
    pos = {d: i for i, d in enumerate(idx)}
    side_s = cc["side"].reindex(idx)
    unl = unload.reindex(idx)
    settle = mkt["settle"]
    alive_n, left, mfe_gap, reasons = 0, [], [], {}
    for t in trades:
        if not t["exit_date"]:
            continue
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
        d0, d1 = pd.Timestamp(t["entry_date"]), pd.Timestamp(t["exit_date"])
        if d1 not in pos or d0 not in pos:
            continue
        i0, i1 = pos[d0], pos[d1]
        sd = 1 if t["side"] == "long" else -1
        # 出场日机构一轮还活着?
        s_exit = side_s.iloc[i1]
        u_exit = unl.iloc[i1]
        alive = np.isfinite(s_exit) and s_exit == sd and np.isfinite(u_exit) and u_exit <= UMAX
        # MFE:持仓期间(按结算价)沿方向的最大浮盈 vs 实际
        path = settle.iloc[i0 + 1:i1 + 1]
        if len(path) and np.isfinite(settle.iloc[i0]):
            exc = (path / settle.iloc[i0] - 1) * sd * 100
            mfe_gap.append(float(exc.max() - t["ret_pct"]))
        if alive:
            alive_n += 1
            # 从出场到一轮结束(翻向/卸仓>UMAX/最多 60 日)沿原方向的收益
            j_end = min(i1 + 60, len(idx) - 1)
            for j in range(i1 + 1, j_end + 1):
                sj, uj = side_s.iloc[j], unl.iloc[j]
                if (np.isfinite(sj) and sj != sd and sj != 0) or (np.isfinite(uj) and uj > UMAX):
                    j_end = j
                    break
            p1, p2 = settle.iloc[i1], settle.iloc[j_end]
            if np.isfinite(p1) and np.isfinite(p2) and p1 > 0:
                left.append(float((p2 / p1 - 1) * sd * 100))
    n = sum(reasons.values())
    return {
        "n": n, "reasons": reasons,
        "alive_pct": alive_n / n * 100 if n else np.nan,
        "left_mean": float(np.mean(left)) if left else np.nan,
        "left_med": float(np.median(left)) if left else np.nan,
        "left_pos_pct": float(np.mean([x > 0 for x in left]) * 100) if left else np.nan,
        "mfe_gap_med": float(np.median(mfe_gap)) if mfe_gap else np.nan,
    }


def run_variant(sig, mkt, rdf, op, st, variant, flags):
    old = dict(H.RULES)
    try:
        if variant == "B":
            H.RULES["max_hold"] = 10**6
            tr, _, daily = H.replay(sig, mkt, rdf, op, st, extra_exit=flags, disable_reverse=True)
        elif variant == "C":
            tr, _, daily = H.replay(sig, mkt, rdf, op, st, extra_exit=flags)
        else:
            tr, _, daily = H.replay(sig, mkt, rdf, op, st)
    finally:
        H.RULES.clear()
        H.RULES.update(old)
    return [t for t in tr if t["exit_date"]], daily


results = {}
for code in ("FG", "SA", "JD", "JM", "LH"):
    sig0, mkt, rdf, op, st, groups, unload = R.load(code)
    seat = H.clean_seat(pd.read_csv(R.DATA / f"{code.lower()}_seat.csv.gz"))
    sig = prod_sig(code, sig0, mkt, seat, groups)
    flags, cc = campaign_exit_flags(sig0, mkt, groups, unload)

    trA, dA = run_variant(sig, mkt, rdf, op, st, "A", flags)
    ck = checkup(code, trA, cc, unload, mkt)
    print("=" * 96)
    print(f"[{code}] 现行 {ck['n']} 笔  出场原因 {ck['reasons']}")
    print(f"  体检:出场时机构一轮还活着 {ck['alive_pct']:.0f}%;活着的那些笔,出场后到一轮结束"
          f"沿原方向再走 均值 {ck['left_mean']:+.2f}% / 中位 {ck['left_med']:+.2f}% / 为正 {ck['left_pos_pct']:.0f}%;"
          f"  MFE−落袋 中位 {ck['mfe_gap_med']:.2f}pp")
    trB, dB = run_variant(sig, mkt, rdf, op, st, "B", flags)
    trC, dC = run_variant(sig, mkt, rdf, op, st, "C", flags)
    rows = {"A 现行": (trA, dA), "B 跟随一轮": (trB, dB), "C 两者都要": (trC, dC)}
    print(f"  {'出场':<10}{'笔数':>5}{'累计':>9}{'夏普':>7}{'回撤':>9}{'均持有':>7}{'正年份':>8}")
    yr = {}
    for k, (tr, d) in rows.items():
        hold = np.mean([t["hold_days"] for t in tr]) if tr else np.nan
        yr[k] = yearly(d)
        posy = sum(1 for x in yr[k].values() if x > 0)
        print(f"  {k:<10}{len(tr):>5}{cum(d):>+8.1f}%{sharpe(d):>7.2f}{H._perf(d)['max_dd_pct']:>+8.1f}%"
              f"{hold:>6.0f}日{posy:>5}/{len(yr[k])}")
    # 选臂 walk-forward(A vs B)
    ys = sorted(yr["A 现行"])
    chain, picks = [], []
    for j, y in enumerate(ys):
        arm = "A"
        if j:
            pr = ys[:j]
            sa = sharpe(dA[dA.index.year.isin(pr)])
            sb = sharpe(dB[dB.index.year.isin(pr)])
            arm = "B" if (np.isfinite(sb) and (not np.isfinite(sa) or sb > sa)) else "A"
        picks.append(arm)
        src = dB if arm == "B" else dA
        chain.append(src[src.index.year == y])
    chain = pd.concat(chain)
    winB = sharpe(dB) > sharpe(dA)
    ddok = H._perf(dB)["max_dd_pct"] >= H._perf(dA)["max_dd_pct"] - 10
    wf = cum(chain) >= cum(dA)
    print(f"  判据:B 夏普赢 A {winB} | 回撤未恶化>10pp {ddok} | 选臂链 {cum(chain):+.1f}% vs 一直 A {cum(dA):+.1f}% {wf}({''.join(picks)})")
    results[code] = (winB, ddok, wf)
    print()

wins = sum(1 for v in results.values() if v[0])
print(f"★ B 夏普赢 A:{wins}/5 品种(判据 ≥3);逐品种 (赢/回撤OK/选臂OK) = {results}")
