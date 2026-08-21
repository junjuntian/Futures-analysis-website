"""拿「机构出货」当出场信号,跟现有的「散户反向」做对照。

**为什么试这个**:量过引擎的出场理由,几乎全是「反向」—— 生猪 15/15、
玻璃 194/235、纯碱 105/129。引擎的行为高度集中在这一条判据上。而这条线索最扎实
的产物恰好是:**机构的出货是一个独立、可观测的过程**(出货期中位 4~5 天,
`unload_series` 已经在算)。

**这是第二次跑。** 第一次(2026-08-21 上午)跑到一半停了 —— 当时发现基准本身
建立在**含回榜前视**的口径上(DEC-108),基准错了对比就没意义。现在引擎已经修成
PIT 口径、纯碱也改成要 dip(DEC-110),基准是干净的,重跑。

**先复现生产,对不上就中止。** 上一次正是靠这条发现前视的:我的基准跑出玻璃
−12.4%/235 笔而文档记 +442%/214 笔,差得离谱 → 一查才知道漏了生产会做的事。
这次一开始就断言。

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
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"

# 生产基准:笔数与逐笔复利累计(不扣费,与 payload 的 stats.cum_pct 同口径)。
# **对不上就中止** —— 基准错了后面全白做。
# **2026-08-21 二次更新**:DEC-111 修掉 past 的跨合约污染、纯碱回退不要 dip
# 之后,五个品种的基准重算(本地 research/data 与生产日志逐项吻合)。
# 旧值 SA (93, 58.7) / JD (26, 34.3) 是 DEC-110 那一版,**已作废**。
EXPECT = {"LH": (18, 90.8), "FG": (228, 69.8), "SA": (111, 64.6),
          "JD": (26, 24.5), "JM": (21, 67.7)}

CODES = ["LH", "FG", "SA", "JD", "JM"]
GRID = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]      # 卸到多少算「出货差不多了」


def prep(code: str):
    """**逐字照抄 `run_one` 的序列** —— 口径一分叉,这个对比就没有意义了。

    注意 `clean_seat` 吃的是**全量**席位数据(含 `reboard_inferred`):
    PIT 口径在引擎内部处理(DEC-108 的 `net_off`),研究这边不该再自己排一遍,
    那样排出来的不是生产在跑的东西。第一次跑这个实验就是这么错的。
    """
    v = H.use(code)
    H.CURRENT = {"code": code, **v}
    low = code.lower()
    price = H.clean_price(pd.read_csv(DATA / f"{low}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(DATA / f"{low}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, _, _ = H.rolling_groups(seat, price, mkt.index)
    sig = H.signal_series(seat, groups)
    rdf, _ = H.retail_series(seat, mkt.index)
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
        exp_n, exp_cum = EXPECT[code]
        if base_n != exp_n or abs(base_cum - exp_cum) > 0.3:
            print(f"\n**中止**:{code} 的基准没复现生产 —— "
                  f"跑出 {base_n} 笔/{base_cum:+.1f}%,生产是 {exp_n} 笔/{exp_cum:+.1f}%。"
                  f"基准错了后面全白做。")
            continue
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
