"""拿「机构出货」当出场信号,跟现有的「散户反向」做对照。

**为什么试这个**(`REPORT_PICK_ROUND_v1` 之后):量过引擎的出场理由,几乎全是
「反向」—— 生猪 15/15、玻璃 194/235、纯碱 105/129。引擎的行为高度集中在这一条
判据上。而这条线索最扎实的产物恰好是:**机构的出货是一个独立、可观测的过程**
(出货期中位 4~5 天,`unload_series` 已经在算)。

**先说清楚这条路上已经有过的负结果**,免得重复劳动:
  · 六轮系统性寻优含 **645 格出场搜索**,全部返回负结果(`research/PITFALLS.md` 5);
  · **「触发席位翻空即离场」已被否决**(同上 6.1):126 笔里 61 笔出现过,
    按它离场 +109.5% vs 实际 +277.3%,差 −167.9。
  但那测的是**翻空**(方向反转),不是**出货程度**(仓位回落比例) —— 两者不是一回事。

三个变体(进场完全不动,只换出场):
  A 现状        反向出场
  B 只用出货    卸到 X% 就走(关掉反向)
  C 两个都要    谁先到算谁

**判据事先写死,不许看完数据再改**:
  · 结论只看**逐年 walk-forward**:用**之前年份**选出最好的 X,应用到下一年,
    把各年结果接起来。全样本最优只当参照 ——「全样本挑最优 = 挑运气」,
    纯随机世界里同一套机器平均能造出 +4 个百分点/笔的假优势;
  · 参数曲面**不单调就是噪音**,不挑那个峰值;
  · 要赢得过基准 A,而且**逐年符号要多数一致**。
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
from run_inst_cost import load  # noqa: E402

CODES = ["LH", "FG", "SA", "JD", "JM"]
GRID = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]      # 卸到多少算「出货差不多了」


def prep(code: str):
    H.use(code)
    seat, price, _ = load(code)
    mkt = H.main_series(price)
    groups, _, _ = H.rolling_groups(seat, price, mkt.index)
    sig = H.signal_series(seat, groups)
    rdf, _ = H.retail_series(seat, mkt.index)
    op, st = H.contract_prices(price)
    unload = H.unload_series(sig, seat, groups)["pct"]
    return sig, mkt, rdf, op, st, unload


def run(bundle, mode: str, x: float | None) -> list[dict]:
    sig, mkt, rdf, op, st, unload = bundle
    if mode == "A":
        return H.replay(sig, mkt, rdf, op, st)[0]
    kick = unload.notna() & (unload >= x)
    return H.replay(sig, mkt, rdf, op, st, extra_exit=kick,
                    disable_reverse=(mode == "B"))[0]


def perf(trades: list[dict]) -> tuple[float, int]:
    """已平仓那些笔的**复利**累计收益(百分点)与笔数。"""
    closed = [t for t in trades if t["exit_date"]]
    if not closed:
        return 0.0, 0
    return (float(np.prod([1 + t["ret_pct"] / 100 for t in closed])) - 1) * 100, len(closed)


def by_year(trades: list[dict]) -> dict[int, float]:
    out: dict[int, list[float]] = {}
    for t in trades:
        if not t["exit_date"]:
            continue
        out.setdefault(int(t["exit_date"][:4]), []).append(t["ret_pct"])
    return {y: (float(np.prod([1 + r / 100 for r in v])) - 1) * 100 for y, v in out.items()}


def main() -> None:
    print(__doc__.split("\n")[0])
    print("\n判据事先写死:只看逐年 walk-forward;参数曲面不单调=噪音;"
          "要赢基准且逐年符号多数一致。")

    for code in CODES:
        bundle = prep(code)
        base = run(bundle, "A", None)
        base_cum, base_n = perf(base)
        base_yr = by_year(base)
        print(f"\n{'=' * 92}")
        print(f"{H.VARIETIES[code]['name']}   基准A(反向出场) 累计 {base_cum:+.1f}% / {base_n} 笔")
        print("=" * 92)

        # ---- 全样本参数曲面(只当参照) ----
        for mode, name in (("B", "B 只用出货"), ("C", "C 两个都要")):
            cells, yrs = [], {}
            for x in GRID:
                tr = run(bundle, mode, x)
                cum, n = perf(tr)
                cells.append(f"{x:.0%}:{cum:>+7.1f}%/{n}")
                yrs[x] = by_year(tr)
            print(f"  {name}(全样本,仅参照)  " + "  ".join(cells))

            # ---- 逐年 walk-forward ----
            years = sorted(set(base_yr) | {y for v in yrs.values() for y in v})
            picked, chain, signs = [], 1.0, []
            for i, y in enumerate(years):
                if i == 0:
                    continue                      # 第一年没有历史可选参
                past = years[:i]
                best_x = max(GRID, key=lambda x: np.prod(
                    [1 + yrs[x].get(p, 0.0) / 100 for p in past]))
                r = yrs[best_x].get(y, 0.0)
                b = base_yr.get(y, 0.0)
                chain *= 1 + r / 100
                signs.append(r - b)
                picked.append(f"{y}:{best_x:.0%}({r:+.1f}% vs 基准{b:+.1f}%)")
            wf = (chain - 1) * 100
            base_chain = (float(np.prod([1 + base_yr.get(y, 0.0) / 100
                                         for y in years[1:]])) - 1) * 100
            win = sum(1 for d in signs if d > 0)
            print(f"    walk-forward 累计 {wf:+.1f}%  vs 同期基准 {base_chain:+.1f}%  "
                  f"**赢的年份 {win}/{len(signs)}**")
            print("      " + "  ".join(picked[:6]) + ("  …" if len(picked) > 6 else ""))


if __name__ == "__main__":
    main()
