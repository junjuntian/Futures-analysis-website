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
"""
from __future__ import annotations

import pathlib

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


if __name__ == "__main__":
    main()
