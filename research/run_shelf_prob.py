"""到达平台位的概率:能不能给出一个可信的频率,而不是一个装饰性的百分比。

运营者 2026-08-20 要的东西:关键平台位 + 到达各平台位的概率(=卖点) + 止损点。
概率这一块必须先验,验不出干净的就不上——宁可只给台阶不给数字。

做法(**首次触达**的经验频率,不是模型):
  对每个 (组合, 日) 算出「从今天到窗口止点,价差最多往下走了多少点」= down_max,
  再除以 σ·√T(σ=近 20 日价差日变动的标准差,T=剩余交易日)。
  这样不同品种、不同波动、不同剩余期的处境才能放在一起数。
  于是 P(能走到距离 D) = P(down_max/(σ√T) ≥ D/(σ√T)) —— 一条经验生存曲线。

三条验收线(任一不过就不上概率):
  ① 单调:距离越远,概率必须越低;
  ② 样本:每个桶要有足够观测,否则那个百分比是装饰;
  ③ 剩余期必须**进分桶**,不能被平均掉——快到期时概率天然低。

第四节**产出 Rust 里的 `REACH_CURVE` 常量**,并与仓库现值逐个对拍。
2026-08-20 审计发现:代码注释写着「重估要跑本脚本」,而本脚本当时只有三段探索性
打印,根本不产出那 4×31 个数——**烤死的常量,来路在仓库里无法复现**。现在跑一次
就知道两件事:新算的曲线长什么样、它跟线上那份差多少。
"""
from __future__ import annotations

import pathlib
import re

import numpy as np
import pandas as pd

DATA = pathlib.Path(__file__).resolve().parent / "data"
NAMES = {"LH": "生猪", "JM": "焦煤", "JD": "鸡蛋", "FG": "玻璃", "SA": "纯碱", "AP": "AP"}


def window_end(contract: str) -> pd.Timestamp:
    """散户窗口止点 = 交割月前月最后一个非周末日(与 DEC-089 / 5A 同口径)。"""
    raw = "".join(ch for ch in str(contract) if ch.isdigit())
    yy, mm = 2000 + int(raw[:2]), int(raw[2:])
    d = pd.Timestamp(year=yy, month=mm, day=1) - pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d


def build() -> pd.DataFrame:
    df = pd.read_csv(DATA / "allspreads.csv.gz", parse_dates=["trade_date"])
    df["spread"] = df["spread"].astype(float)
    rows = []
    for (c1, c2), g in df.groupby(["contract_1", "contract_2"], sort=False):
        g = g.sort_values("trade_date")
        end = min(window_end(c1), window_end(c2))
        g = g[g["trade_date"] <= end]
        if len(g) < 40:
            continue
        v = g["spread"].to_numpy()
        n = len(v)
        # 从**次日**起到止点的最低/最高(不含当日,当日不能算成已到达)
        fmin = np.full(n, np.nan)
        fmax = np.full(n, np.nan)
        mn, mx = np.inf, -np.inf
        for i in range(n - 1, -1, -1):
            fmin[i], fmax[i] = mn, mx
            mn, mx = min(mn, v[i]), max(mx, v[i])
        sigma = pd.Series(v).diff().rolling(20, min_periods=10).std().to_numpy()
        T = np.arange(n - 1, -1, -1, dtype=float)          # 剩余交易日
        rows.append(pd.DataFrame({
            "inst": g["instrument_1"].iloc[0], "combo": f"{c1}-{c2}",
            "date": g["trade_date"].to_numpy(), "T": T, "sigma": sigma,
            "down": v - fmin, "up": fmax - v,
        }))
    a = pd.concat(rows)
    a = a[(a["T"] >= 5) & np.isfinite(a["sigma"]) & (a["sigma"] > 0)]
    a = a[np.isfinite(a["down"]) & np.isfinite(a["up"])]
    a["scale"] = a["sigma"] * np.sqrt(a["T"])
    a["zdown"] = a["down"] / a["scale"]
    a["zup"] = a["up"] / a["scale"]
    return a


def main() -> None:
    a = build()
    print("样本:", f"{len(a):,} 个(组合×日)",
          "| 品种:", "、".join(f"{NAMES.get(k,k)} {v:,}" for k, v in
                              a["inst"].value_counts().items()))

    print("\n" + "=" * 92)
    print("一、单调性与样本量:到达 D 个「σ√T」之外的经验频率")
    print("=" * 92)
    print("  D 是距离,σ√T 是这个处境下价差的自然尺度。两个方向分开数——")
    print("  向下(做空价差的目标)与向上(止损那一侧)不一定对称。\n")
    grid = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5]
    print(f"  {'D(σ√T)':>9s}{'向下到达%':>11s}{'向上到达%':>11s}{'样本':>9s}")
    for d in grid:
        print(f"  {d:>9.2f}{100*(a['zdown']>=d).mean():>11.1f}{100*(a['zup']>=d).mean():>11.1f}{len(a):>9,d}")

    print("\n" + "=" * 92)
    print("二、剩余期必须进分桶:同一个 D,剩余天数不同结果一样吗")
    print("=" * 92)
    a["Tbin"] = pd.cut(a["T"], [4, 20, 40, 80, 160, 10**9],
                       labels=["5~20日", "21~40日", "41~80日", "81~160日", ">160日"])
    piv = []
    for d in (0.5, 1.0, 1.5):
        r = a.groupby("Tbin", observed=True).apply(
            lambda g: 100 * (g["zdown"] >= d).mean(), include_groups=False)
        piv.append(r.rename(f"D={d}"))
    cnt = a.groupby("Tbin", observed=True).size().rename("样本")
    out = pd.concat(piv + [cnt], axis=1)
    print(out.round(1).to_string())
    print("\n  如果各行差不多,说明 σ√T 这个尺度已经把剩余期吸收掉了,不用再分桶;")
    print("  差很多就必须把剩余期也当成一维,否则快到期时会给出虚高的概率。")

    print("\n" + "=" * 92)
    print("三、逐品种(同一套尺度在各品种上是不是都成立)")
    print("=" * 92)
    print(f"  {'品种':6s}{'样本':>9s}" + "".join(f"{'D='+str(d):>9s}" for d in (0.5, 1.0, 1.5, 2.0)))
    for inst, g in a.groupby("inst"):
        row = f"  {NAMES.get(inst, inst):6s}{len(g):>9,d}"
        for d in (0.5, 1.0, 1.5, 2.0):
            row += f"{100*(g['zdown']>=d).mean():>9.1f}"
        print(row)

    emit_curve(a)


# ---- 第四节:产出并核对 Rust 常量 ----------------------------------------

# 与 Rust 的 `reach_bucket` 一一对应。改这里就得改那边,反之亦然。
CURVE_BINS = [4, 20, 40, 80, 10**9]
CURVE_LABELS = ["5~20", "21~40", "41~80", ">80"]
CURVE_GRID = np.round(np.arange(0, 3.01, 0.1), 2)   # z = 0.0 … 3.0,步长 0.1
RUST_FILE = (pathlib.Path(__file__).resolve().parents[1]
             / "rust" / "apps" / "api" / "src" / "spread_analytics.rs")


def curve_table(a: pd.DataFrame) -> tuple[list[list[float]], list[int]]:
    """每个剩余期桶一条经验生存曲线,**两个方向合并**。

    合并方向 = 把 zdown 与 zup 拼起来一起数,所以每行贡献两个观测——Rust 注释里
    的样本数(如 18,848)正是行数的两倍,不是写错了。合并的理由见 Rust 侧注释:
    逐方向的版本样本外崩了,那些差异是行情往哪边走了(=漂移),不是品种特性。
    """
    b = a.assign(B=pd.cut(a["T"], CURVE_BINS, labels=CURVE_LABELS))
    curves, counts = [], []
    for lab in CURVE_LABELS:
        g = b[b["B"] == lab]
        z = np.concatenate([g["zdown"].to_numpy(), g["zup"].to_numpy()])
        curves.append([round(100 * float((z >= d).mean()), 1) for d in CURVE_GRID])
        counts.append(len(z))
    return curves, counts


def read_baked() -> list[list[float]] | None:
    """读出仓库里现有的 REACH_CURVE,用于对拍。读不到返回 None(不当致命错)。"""
    try:
        src = RUST_FILE.read_text(encoding="utf-8")
        blk = src[src.index("const REACH_CURVE"):]
        blk = blk[: blk.index("\n];")]
        blk = blk[blk.index("=") + 1:]            # 去掉 `[&[f64]; 4]` 这段类型标注
        rows = re.findall(r"&\[(.*?)\],", blk, re.S)
        return [[float(x) for x in re.findall(r"\d+\.\d+", r)] for r in rows]
    except (OSError, ValueError):
        return None


def emit_curve(a: pd.DataFrame) -> None:
    print()
    print("=" * 92)
    print("四、产出 Rust 常量 REACH_CURVE,并与仓库现值对拍")
    print("=" * 92)
    curves, counts = curve_table(a)

    baked = read_baked()
    if baked is None or len(baked) != len(curves):
        print("  ⚠ 读不出仓库里的现值,只输出新算的曲线。")
    else:
        worst = 0.0
        for lab, new, old in zip(CURVE_LABELS, curves, baked):
            if len(new) != len(old):
                diff = 99.9
            else:
                diff = max(abs(x - y) for x, y in zip(new, old))
            worst = max(worst, diff)
            verdict = "一致" if diff <= 0.05 else "**不一致**"
            print(f"  剩余 {lab:>6s} 日   最大差 {diff:5.2f} 个百分点   {verdict}")
        print()
        if worst <= 0.05:
            print("  → 仓库里的常量与本次重算一致,不用动代码。")
        else:
            print("  → **有差异。差异不等于该改**:先分清是数据多了几天(那就别动,")
            print("     研究结论不该因为多一天数据就让页面上的数字无声漂移),")
            print("     还是口径真的变了(那要连同 DECISIONS 一起改)。")

    print()
    print("  下面这段可直接替换 spread_analytics.rs 里的 REACH_CURVE:")
    print()
    print("const REACH_CURVE: [&[f64]; 4] = [")
    for lab, row, n in zip(CURVE_LABELS, curves, counts):
        print(f"    // 剩余 {lab} 个交易日,样本 {n:,}")
        print("    &[")
        for i in range(0, len(row), 15):
            print("        " + ", ".join(str(x) for x in row[i:i + 15]) + ",")
        print("    ],")
    print("];")
    print()
    print(f"  (z 网格 {CURVE_GRID[0]}~{CURVE_GRID[-1]},步长 0.1,共 {len(CURVE_GRID)} 点;")
    print("   Rust 侧 reach_pct 按同一步长线性插值,两边的步长必须一样)")


if __name__ == "__main__":
    main()
