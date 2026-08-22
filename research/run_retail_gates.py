"""散户单路的五道闸门 —— 按 PLAN_RETAIL_ONLY_v1 预注册执行,判据原样照搬。

散户单路 = 把 sig 的 z 换成散户 rz 喂进去:共振恒成立,进出场全是散户 rz。
这不是新代码路径,是共振路径的退化情形,replay 一个字节没改。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402

SEEDS = list(H.RULES["retail_seed"])


def tstat(rets):
    a = np.array(rets, dtype=float)
    return a.mean() / a.std(ddof=1) * np.sqrt(len(a)) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan


def sharpe(d):
    d = d.fillna(0)
    return float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan


def cum(d):
    return float((1 + d.fillna(0)).prod() - 1) * 100


def yearly(d):
    return {y: cum(d[d.index.year == y]) for y in sorted({x.year for x in d.index})}


def retail_only(sig, mkt, rdf, op, st, seat=None, seeds=None, enter=1.0):
    """散户单路回放。seeds 给了就按留二名单重算 rdf;enter 改门槛。"""
    old_enter, old_seed = H.RULES["enter"], list(H.RULES["retail_seed"])
    try:
        H.RULES["enter"] = enter
        if seeds is not None:
            H.RULES["retail_seed"] = list(seeds)
            rdf, _ = H.retail_series(seat, mkt.index)
        fake = sig.copy()
        fake["z"] = rdf["rz"].reindex(sig.index)
        tr, _, daily = H.replay(fake, mkt, rdf, op, st)
    finally:
        H.RULES["enter"], H.RULES["retail_seed"] = old_enter, old_seed
    return [t for t in tr if t["exit_date"]], daily


def gates(code):
    v = H.use(code)
    H.CURRENT = {"code": code, **v}
    low = code.lower()
    price = H.clean_price(pd.read_csv(R.DATA / f"{low}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(R.DATA / f"{low}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, _, _ = H.rolling_groups(seat, price, mkt.index)
    sig = H.signal_series(seat, groups)
    rdf, _ = H.retail_series(seat, mkt.index)
    # 基线 = 该品种生产信号(焦煤生猪 = 共振)
    H.RULES["signal_source"] = "resonance"
    tr_b, _, day_b = H.replay(sig, mkt, rdf, op, st)
    tr_b = [t for t in tr_b if t["exit_date"]]
    tr_c, day_c = retail_only(sig, mkt, rdf, op, st)
    print("=" * 92)
    print(f"[{code}] 基线(共振) {len(tr_b)} 笔/{cum(day_b):+.1f}%/{sharpe(day_b):.2f}   "
          f"散户单路 {len(tr_c)} 笔/{cum(day_c):+.1f}%/{sharpe(day_c):.2f}/回撤 {H._perf(day_c)['max_dd_pct']:+.1f}%")
    print("=" * 92)
    yb, yc = yearly(day_b), yearly(day_c)
    wins = sum(1 for y in yb if yc.get(y, 0) > yb[y])
    pos = sum(1 for y in yc if yc[y] > 0)
    for y in sorted(yb):
        print(f"    {y}: 候选 {yc.get(y, float('nan')):>+7.1f}%  基线 {yb[y]:>+7.1f}%  {'✓' if yc.get(y, 0) > yb[y] else ' '}")
    g1 = wins >= len(yb) / 2 and pos >= len(yc) / 2
    print(f"闸门1 逐年:赢 {wins}/{len(yb)},正 {pos}/{len(yc)}  [{'过' if g1 else '不过'}]")
    years = sorted(yb)
    chain, picks = [], []
    for j, y in enumerate(years):
        if j == 0:
            arm = "基"
        else:
            pr = years[:j]
            sb, sc = sharpe(day_b[day_b.index.year.isin(pr)]), sharpe(day_c[day_c.index.year.isin(pr)])
            arm = "候" if (np.isfinite(sc) and (not np.isfinite(sb) or sc > sb)) else "基"
        picks.append(arm)
        src = day_c if arm == "候" else day_b
        chain.append(src[src.index.year == y])
    chain = pd.concat(chain)
    g2 = cum(chain) >= cum(day_b)
    print(f"闸门2 选臂 walk-forward:链 {cum(chain):+.1f}% vs 一直基线 {cum(day_b):+.1f}%({''.join(picks)})  [{'过' if g2 else '不过'}]")
    s08 = sharpe(retail_only(sig, mkt, rdf, op, st, enter=0.8)[1])
    s12 = sharpe(retail_only(sig, mkt, rdf, op, st, enter=1.2)[1])
    g3 = s08 > sharpe(day_b) and s12 > sharpe(day_b)
    print(f"闸门3 门槛 0.8/1.0/1.2:{s08:.2f}/{sharpe(day_c):.2f}/{s12:.2f} vs 基线 {sharpe(day_b):.2f}  [{'过' if g3 else '不过'}]")
    loo = []
    for drop in SEEDS:
        keep = [x for x in SEEDS if x != drop]
        _, d = retail_only(sig, mkt, rdf, op, st, seat=seat, seeds=keep)
        loo.append((drop, sharpe(d)))
    n_ok = sum(1 for _, x in loo if x > sharpe(day_b))
    g4 = n_ok >= 2
    print("闸门4 名单留二:" + "  ".join(f"去{d}→{x:.2f}" for d, x in loo) + f"  ({n_ok}/3 赢基线)  [{'过' if g4 else '不过'}]")
    tc, tb = tstat([t["ret_pct"] for t in tr_c]), tstat([t["ret_pct"] for t in tr_b])
    g5 = np.isfinite(tc) and tc > 0
    print(f"闸门5 单笔 t:候选 {tc:+.2f}(基线 {tb:+.2f})  [{'过' if g5 else '不过'}]")
    n = sum([g1, g2, g3, g4, g5])
    print(f"  ★ [{code}] 五关通过 {n}/5\n")
    return n


def info_row(code):
    """玻璃/纯碱/鸡蛋:只给一行「散户单路 vs 现行成本」参考数,不设闸门。"""
    sig, mkt, rdf, op, st, groups, unload = R.load(code)   # R.load 钉 resonance,这里只借数据
    v = H.use(code)                                          # 恢复生产 cost 配置
    H.CURRENT = {"code": code, **v}
    seat = H.clean_seat(pd.read_csv(R.DATA / f"{code.lower()}_seat.csv.gz"))
    sig_c = H.attach_cost_signal(sig, seat, mkt, groups)
    _, _, day_cost = H.replay(sig_c, mkt, rdf, op, st)
    H.RULES["signal_source"] = "resonance"
    _, day_r = retail_only(sig, mkt, rdf, op, st)
    print(f"[{code}] 参考:现行成本 {cum(day_cost):+.1f}%/{sharpe(day_cost):.2f}   "
          f"散户单路 {cum(day_r):+.1f}%/{sharpe(day_r):.2f}   (不设闸门,不提改动)")


if __name__ == "__main__":
    res = {c: gates(c) for c in ("JM", "LH")}
    print("—— 参考(刚换成本信号的三个,只看不动) ——")
    for c in ("FG", "SA", "JD"):
        info_row(c)
    print("\n候选通过数:", res)
