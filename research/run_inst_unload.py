"""机构「出货程度」能不能解禁反向进场 —— 运营者四条规则里的第 2 条。

> 如果和机构反向，那就需要机构出货出的差不多了，才能和机构反向。

第一轮(`REPORT_INST_COST_v1.md`)否掉了规则 1(只在玻璃成立)与规则 3(成本位置
是动量的伪装)。**这一条是四条里唯一不与价格机械相关的** —— 它约束的是机构仓位
的轨迹,不是价格位置,所以躲得开上一轮那个"其实在测动量"的判决。

口径先钉死,不许事后再调:

    出货程度 = 1 − |当前净持仓| / |本轮方向内的峰值|

  · 峰值取**滚动最大值,只看到当天**(因果,无前视);
  · **方向翻转时重置** —— "出货"指的是把这一轮建起来的仓卸掉,
    上一轮反方向的峰值与它无关;
  · 0 = 正在峰值上;1 = 卸干净了。

要判的:**出货程度越高,站到机构对面越划算吗?** 也就是 `side × 前瞻收益`
(顺机构方向的收益)是否随出货程度**单调下降** —— 下降才说明反向有戏。

两道闸门(上一轮就是靠第一道否掉规则 3 的):

  ① 动量对照:在「顺机构方向的近 20 日收益」分档之内再看,效应还在不在。
  ② **持仓量安慰剂**:把同一套回落比例算在**全市场持仓量**上。它同样是仓位类
     变量,却不含任何机构信息 —— 如果它能做出一样的效果,那这事跟聪明钱无关。

`reboard_inferred` 一律排除(回榜反推 = 未来数据,见第一轮报告)。
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from run_inst_cost import group_book, load  # noqa: E402

CODES = ["LH", "FG", "SA", "JD", "JM"]
HORIZONS = [5, 10, 20]


def retrace(net: pd.Series) -> pd.Series:
    """本轮方向内、相对滚动峰值的回落比例。0=在峰值,1=卸干净。

    **因果**:峰值只用到当天为止的值。方向翻转或仓位归零就重开一轮 ——
    上一轮反方向的峰值与这一轮的"出货"无关。
    """
    out = np.full(len(net), np.nan)
    peak = np.nan
    cur = 0
    for i, n in enumerate(net.to_numpy()):
        if not np.isfinite(n) or n == 0:
            peak, cur = np.nan, 0
            continue
        s = int(np.sign(n))
        if s != cur:
            cur, peak = s, abs(n)
        else:
            peak = max(peak, abs(n))
        out[i] = 1.0 - abs(n) / peak if peak > 0 else np.nan
    return pd.Series(out, index=net.index)


def market_oi(code: str, dates: pd.DatetimeIndex, main: pd.Series) -> pd.Series:
    """主力合约的全市场持仓量 —— 安慰剂用。不含任何机构信息。"""
    _, price, _ = load(code)
    oi = price.pivot_table(index="trade_date", columns="contract",
                           values="open_interest", aggfunc="last").reindex(dates)
    return pd.Series(
        [oi.at[d, c] if isinstance(c, str) and c in oi.columns else np.nan
         for d, c in zip(dates, main)], index=dates)


def buckets(x: pd.Series, r: pd.Series, k: int = 4) -> pd.DataFrame | None:
    """按 x 分 k 档看 r 的均值。档位分不开就返回 None。"""
    df = pd.DataFrame({"x": x, "r": r}).dropna()
    if len(df) < 120:
        return None
    # **不能同时传显式 labels 与 duplicates="drop"** —— 并档之后标签数对不上,
    # pandas 抛 ValueError,而这里的 except 会把它吞成"样本不足"。
    # 玻璃样本内那半有 25% 的值恰好是 0(在峰值),正好踩中,一度报成没数据。
    try:
        codes = pd.qcut(df["x"], k, labels=False, duplicates="drop")
    except ValueError:
        return None
    if pd.Series(codes).nunique() < 3:
        return None
    q = pd.Series(codes, index=df.index).map(lambda i: f"Q{int(i) + 1}")
    return df.groupby(q, observed=True)["r"].agg(["mean", "count"])


def line(g: pd.DataFrame) -> str:
    cells = "  ".join(f"{k}:{v['mean'] * 100:+6.2f}%" for k, v in g.iterrows())
    lo, hi = g["mean"].iloc[0], g["mean"].iloc[-1]
    trend = "↓单调" if g["mean"].is_monotonic_decreasing else (
        "↑单调" if g["mean"].is_monotonic_increasing else "  不单调")
    return f"{cells}   末−首 {(hi - lo) * 100:+.2f}%  {trend}  每档~{int(g['count'].mean()):,}"


def report(code: str) -> None:
    book = group_book(code)
    px = book["settle"]
    side = np.sign(book["net"])
    unload = retrace(book["net"])
    oi_unload = retrace(market_oi(code, book.index, book["main"]))

    ok = np.isfinite(unload) & np.isfinite(side) & (side != 0)
    n = int(ok.sum())
    print(f"\n{'=' * 92}")
    print(f"{H.VARIETIES[code]['name']}   可判定的天 {n:,}")
    print("=" * 92)
    if n < 200:
        print("  样本不足 200,跳过 —— 这个品种上这条规则本来就大部分日子无法判定。")
        return
    u = unload[ok]
    print(f"  出货程度分布:在峰值(=0)的天占 {(u == 0).mean():.0%};"
          f"中位 {u.median():.0%};卸掉一半以上的天占 {(u > 0.5).mean():.0%}")

    print("\n  ① 主表:顺机构方向的收益 vs 出货程度")
    print("     (**要看到 ↓单调** —— 出货越多、顺势收益越差,反向才有依据)")
    for h in HORIZONS:
        fwd = px.shift(-h) / px - 1
        g = buckets(unload[ok], (side * fwd)[ok])
        print(f"    {h:>2} 日  {line(g) if g is not None else '样本不足'}")

    print("\n  ② 持仓量安慰剂:同一套回落比例,算在全市场持仓量上(不含机构信息)")
    for h in HORIZONS:
        fwd = px.shift(-h) / px - 1
        g = buckets(oi_unload[ok], (side * fwd)[ok])
        print(f"    {h:>2} 日  {line(g) if g is not None else '样本不足'}")

    print("\n  ③ 动量对照:在近 20 日「顺机构方向收益」分 3 档之内,再按出货程度分 3 档")
    fwd = px.shift(-10) / px - 1
    past = side * (px / px.shift(20) - 1)
    df = pd.DataFrame({"u": unload, "past": past, "r": side * fwd})[ok].dropna()
    if len(df) < 200:
        print("    样本不足")
        return
    print(f"    出货程度与近 20 日收益的相关系数 {df['u'].corr(df['past']):+.2f}"
          f"   ← 接近 0 才说明它不是动量的另一种写法")
    try:
        pb = pd.qcut(df["past"], 3, labels=["跌", "平", "涨"], duplicates="drop")
    except ValueError:
        print("    近期收益分不出档")
        return
    print("      动量档 |     出货少     出货中     出货多     多−少")
    for m in ("跌", "平", "涨"):
        sub = df[pb == m]
        if len(sub) < 40:
            continue
        try:
            qb = pd.qcut(sub["u"], 3, labels=["少", "中", "多"], duplicates="drop")
        except ValueError:
            continue
        if qb.nunique() < 3:
            continue
        g = sub.groupby(qb, observed=True)["r"].mean()
        cells = "".join(f"  {g.get(q, np.nan) * 100:+8.2f}%" for q in ("少", "中", "多"))
        print(f"      {m:>6s} |{cells}   {(g.get('多', np.nan) - g.get('少', np.nan)) * 100:+.2f}%")


def oos(code: str) -> None:
    """样本外切分:前半段看形状,后半段验它还在不在。

    **只对数据量够的品种做。** LH/JD/JM 的可判定天数只有 400~500 天,一切两半
    每边两百出头,验不出任何东西 —— 硬做只会得到一个噪音驱动的结论,
    而那种结论最容易被当成"验过了"。

    切点用**时间中位**,不是随机切:随机切会让同一段行情同时落进两边,
    重叠样本下等于没切。
    """
    book = group_book(code)
    px = book["settle"]
    side = np.sign(book["net"])
    unload = retrace(book["net"])
    ok = np.isfinite(unload) & np.isfinite(side) & (side != 0)
    n = int(ok.sum())
    if n < 1200:
        print(f"  {H.VARIETIES[code]['name']}: 可判定 {n:,} 天,**不做样本外** —— "
              f"切两半每边太薄,验不出东西。")
        return

    idx = book.index[ok]
    cut = idx[len(idx) // 2]
    print(f"\n  {H.VARIETIES[code]['name']}  切点 {cut.date()}  "
          f"(前 {int((idx <= cut).sum()):,} 天 / 后 {int((idx > cut).sum()):,} 天)")
    for h in HORIZONS:
        fwd = px.shift(-h) / px - 1
        r = side * fwd
        cells = []
        for name, mask in (("样本内", ok & (book.index <= cut)),
                           ("样本外", ok & (book.index > cut))):
            g = buckets(unload[mask], r[mask])
            if g is None:
                cells.append(f"{name}:样本不足")
                continue
            spread = (g["mean"].iloc[-1] - g["mean"].iloc[0]) * 100
            cells.append(f"{name} 末−首 {spread:+.2f}%")
        print(f"    {h:>2} 日   " + "   |   ".join(cells))


def main() -> None:
    print(__doc__.split("\n")[0])
    print("\n判定标准(事先写死,不许看完数据再改):")
    print("  · ① 三个前瞻窗口都要 ↓单调,且末−首有量级(> 0.5%);")
    print("  · ② 安慰剂表若做出同等效应 → 与机构无关,否决;")
    print("  · ③ 动量三档里「多−少」符号要一致 → 否则是动量伪装,否决。")
    for code in CODES:
        try:
            report(code)
        except Exception as exc:                       # noqa: BLE001
            print(f"\n{code}: 跑不动 —— {type(exc).__name__}: {exc}")
    print(f"\n{'=' * 92}")
    print("提醒:t 值与显著性这一轮不报 —— 重叠样本会系统性高估。")
    print("这一轮只判形状与安慰剂;能过这三关,再做聚类去重叠与样本外切分。")

    print(f"\n{'=' * 92}")
    print("④ 样本外切分(只对数据量够的品种):样本内看到的形状,样本外还在吗?")
    print("=" * 92)
    print("  两边符号一致且量级相当才算数。样本外翻号或塌掉 —— 那就是样本内挑出来的。")
    for code in CODES:
        try:
            oos(code)
        except Exception as exc:                       # noqa: BLE001
            print(f"  {code}: 跑不动 —— {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
