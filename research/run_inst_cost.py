"""机构持仓成本能不能当进场判据 —— 第一步:覆盖率、方向、成本位置。

运营者 2026-08-21 的判断:「核心策略是跟随聪明钱……方向和机构一致;和机构反向就得
等机构出货出得差不多。我的成本跟机构差不多、或者优于机构,才是更好的入场点。
散户只能当参考,只能做辅助判断入场时机,而不是真正的入场时机。」

这与现行方案 C 是根本性的调换:现在散户信号当扳机、机构只用来判共振。

**为什么值得试**:`hog_money.py` 里一个 `cost` 字都没有——机构成本是现行信息集
里完全没用到的一维。而 DEC-052/DEC-084 早就写着「现行规则已达该信息集上限,
空间在品种分散与**新信息维度**」。这正是那个维度。

**不预设它会赢。** 这个项目里六轮系统性寻优全部返回过负结果
(见 research/PITFALLS.md)。这一步只判三件事,任何一件不过就不往下做:

  A 覆盖率 —— 这 5 家的合计成本有多少天算得出来?算不出来的规则不能用。
  B 方向   —— 单按机构**净持仓方向**持有,有没有收益?这是运营者第 1 条的地基。
  C 成本位置 —— 「入场价相对机构成本的位置」与后续收益是不是单调?

**前视陷阱(本脚本的头等纪律)**:`reboard_inferred` 那些行是**用回榜日的增减倒推
出来的**,对被填的那几天是未来数据。生产实例(fix-sanhe-fabricated-changes.sql):
    04-16  822 (+194)   在榜,交易所原始
    04-17  215 (−822)   掉榜。215 由 04-18 倒推
    04-18  824 (+609)   回榜;824 − 609 = 215
现行信号引擎不碰成本所以不受影响,但**成本一旦进信号,这就是活的前视源**。
本脚本一律排除,并把排掉多少行打出来。

**第二个限制,必须如实写在结论里**:我们拿到的是**推算成本**(公开持仓变化 +
结算价推出),不是成交均价——交易所不公布成交明细。「优于机构成本」是跟一个
带误差的估计在比。
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"
CODES = ["LH", "FG", "SA", "JD", "JM"]
# 前瞻窗口。引擎实际持仓中位数在 20 日上下,取三档看形状随窗口稳不稳。
HORIZONS = [5, 10, 20]


def load(code: str) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """读该品种的席位与行情,**排除回榜反推行**,返回排掉的行数。"""
    low = code.lower()
    seat = pd.read_csv(DATA / f"{low}_seat.csv.gz", parse_dates=["trade_date"])
    price = pd.read_csv(DATA / f"{low}_price.csv.gz", parse_dates=["trade_date"])
    dropped = int((seat["source"] == "reboard_inferred").sum())
    seat = seat[seat["source"] != "reboard_inferred"].copy()
    return H.clean_seat(seat), H.clean_price(price), dropped


def cost_of(net: pd.Series, settle: pd.Series) -> pd.Series:
    """逐日推算净持仓成本 —— 与 `domain/seat_cost.rs` 同一套口径。

    加仓按手数加权平均、减仓不改均价、**净头寸翻向或归零时重置**。
    用结算价:收盘价在无成交日是 0(DEC-073),结算价才是当日代表价。

    这是那套算法的第三份实现(Rust 一份、SQL 侧一份)。**不得改口径** —— 一改
    研究结论就与页面上运营者看到的成本不是同一个东西了。函数末尾有对拍断言。
    """
    out = np.full(len(net), np.nan)
    cost = np.nan
    prev = 0.0
    for i, (n, s) in enumerate(zip(net.to_numpy(), settle.to_numpy())):
        if not np.isfinite(n):
            out[i] = cost          # 掉榜:什么都不动,成本原地冻结
            continue
        flipped = (prev > 0 > n) or (prev < 0 < n)
        if flipped or n == 0:
            cost = np.nan
        if n != 0 and np.isfinite(s):
            added = abs(n) - abs(prev) if (prev == 0 or np.sign(n) == np.sign(prev)) else abs(n)
            if not np.isfinite(cost):
                cost = s                              # 新建仓:成本就是当日结算价
            elif added > 0:
                cost = (cost * (abs(n) - added) + s * added) / abs(n)   # 加仓:加权
            # 减仓不改均价 —— 留空是有意的
        out[i] = cost
        prev = n if np.isfinite(n) else prev
    return pd.Series(out, index=net.index)


def group_book(code: str) -> pd.DataFrame:
    """按引擎同一套选组逻辑,算出这 5 家在**主力合约**上的合计仓位与加权成本。

    逐席位逐合约各自算成本再按手数加权 —— 与页面同源。先聚合再算成本会把各家
    自己的建仓节奏抹掉,得出一个不对应任何真实仓位的数。

    `clean_seat` 的输出里**掉榜就是没有那一行**(不是 NaN 行),所以下面 reindex
    到完整交易日之后要显式区分「没行=未知」与「有行且为 0=真平了」。
    """
    H.use(code)
    seat, price, dropped = load(code)
    mkt = H.main_series(price)
    dates = mkt.index
    groups, _, _ = H.rolling_groups(seat, price, dates)

    settle = price.pivot_table(index="trade_date", columns="contract",
                               values="settlement_price", aggfunc="last").reindex(dates)
    # 逐 (席位, 合约) 的净持仓。缺行 = 掉榜 = 未知,保持 NaN。
    net_wide = seat.pivot_table(index="trade_date", columns=["member_key", "contract"],
                                values="net", aggfunc="sum").reindex(dates)

    cost_wide = {}
    for key in net_wide.columns:
        contract = key[1]
        if contract in settle.columns:
            cost_wide[key] = cost_of(net_wide[key], settle[contract])

    rows = []
    main = mkt["main"]
    for d in dates:
        members = groups.get(d)
        c = main.get(d)
        if not members or not isinstance(c, str):
            rows.append({"date": d, "net": np.nan, "cost": np.nan,
                         "known": 0, "n": 0, "cover": np.nan})
            continue
        legs = []
        for m in members:
            key = (m, c)
            if key not in net_wide.columns:
                continue
            n = net_wide.at[d, key]
            if not np.isfinite(n) or n == 0:
                continue                       # 掉榜或真平仓,都不贡献这一天的方向
            legs.append((float(n), float(cost_wide.get(key, pd.Series(dtype=float)).get(d, np.nan))))
        if not legs:
            rows.append({"date": d, "net": np.nan, "cost": np.nan,
                         "known": 0, "n": 0, "cover": np.nan})
            continue
        total = sum(n for n, _ in legs)
        if total == 0:
            rows.append({"date": d, "net": 0.0, "cost": np.nan,
                         "known": 0, "n": len(legs), "cover": np.nan})
            continue
        # 只按**净方向那一侧**的席位加权:运营者问的是「我的成本跟机构比」,
        # 机构指的是站在那个方向上的那些仓,不是把对手盘也平均进来。
        side_legs = [(n, cst) for n, cst in legs if np.sign(n) == np.sign(total)]
        side_lots = sum(abs(n) for n, _ in side_legs)
        priced = sum(abs(n) for n, cst in side_legs if np.isfinite(cst))
        wcost = (sum(abs(n) * cst for n, cst in side_legs if np.isfinite(cst)) / priced
                 if priced > 0 else np.nan)
        rows.append({"date": d, "net": total, "cost": wcost,
                     "known": int(priced > 0), "n": len(legs),
                     "cover": priced / side_lots if side_lots > 0 else np.nan})

    book = pd.DataFrame(rows).set_index("date")
    op, st = H.contract_prices(price)
    book["main"] = main
    book["open"] = [op.at[d, c] if isinstance(c, str) and c in op.columns and d in op.index
                    else np.nan for d, c in zip(book.index, book["main"])]
    book["settle"] = [st.at[d, c] if isinstance(c, str) and c in st.columns and d in st.index
                      else np.nan for d, c in zip(book.index, book["main"])]
    book.attrs["dropped"] = dropped
    return book


def report(code: str) -> dict:
    book = group_book(code)
    n_all = len(book)
    known = int(book["known"].sum())
    has_pos = int(np.isfinite(book["net"]).sum())

    print(f"\n{'=' * 88}")
    print(f"{H.VARIETIES[code]['name']}  交易日 {n_all:,}  "
          f"(排除 reboard_inferred {book.attrs['dropped']:,} 行)")
    print("=" * 88)
    print(f"  A 覆盖率:有机构净持仓的天 {has_pos:,} ({has_pos / n_all:.0%});"
          f"其中成本算得出的 {known:,} ({known / max(has_pos, 1):.0%})")
    if known:
        cov = book.loc[book["known"] == 1, "cover"]
        print(f"    成本覆盖的手数占该侧的比例:中位 {cov.median():.0%}、"
              f"最低十分位 {cov.quantile(0.1):.0%}")

    # ---- B 方向:单按机构净持仓方向持有 ----
    px = book["settle"]
    side = np.sign(book["net"])
    print("  B 方向(单按机构净持仓方向持有,次日开盘换仓,不含成本):")
    for h in HORIZONS:
        fwd = px.shift(-h) / px - 1
        r = (side * fwd).dropna()
        if len(r) < 30:
            print(f"    {h:>2} 日: 样本不足")
            continue
        t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if r.std(ddof=1) > 0 else np.nan
        print(f"    {h:>2} 日: 均值 {r.mean() * 100:+.2f}%  胜率 {(r > 0).mean():.1%}  "
              f"样本 {len(r):,}  t={t:+.2f}  "
              f"(**重叠样本,t 被高估,只看符号与量级**)")

    # ---- C 成本位置 ----
    sigma = px.diff().rolling(20, min_periods=10).std()
    entry = book["open"].shift(-1)          # 次日开盘成交(DEC-090)
    # 优于机构 = 做多买得更便宜 / 做空卖得更贵。用 σ 归一,跨品种可比。
    edge = side * (book["cost"] - entry) / sigma
    print("  C 成本位置(edge = 顺机构方向下,我比机构的成本好多少个 σ):")
    ok = np.isfinite(edge) & (book["known"] == 1)
    if ok.sum() < 100:
        print("    样本不足,跳过")
        return {"code": code}
    for h in HORIZONS:
        fwd = px.shift(-h) / px - 1
        r = (side * fwd)
        df = pd.DataFrame({"edge": edge[ok], "r": r[ok]}).dropna()
        if len(df) < 100:
            continue
        q = pd.qcut(df["edge"], 5, labels=[f"Q{i}" for i in range(1, 6)], duplicates="drop")
        g = df.groupby(q, observed=True)["r"].agg(["mean", "count"])
        cells = "  ".join(f"{k}:{v['mean'] * 100:+.2f}%" for k, v in g.iterrows())
        mono = g["mean"].is_monotonic_increasing
        print(f"    {h:>2} 日  {cells}   单调递增:{'是' if mono else '否'}  "
              f"每桶 ~{int(g['count'].mean()):,}")
    return {"code": code, "known": known, "has_pos": has_pos, "n": n_all}


def control(code: str) -> None:
    """把动量这个混淆项排掉:在「近期涨跌」分档之内,edge 还有没有效?

    **为什么必须做这一步**:`edge = side × (机构成本 − 入场价) / σ` 与近期价格走势
    机械相关 —— 价格刚跌过,做多的 edge 就自动变高。不控制的话,上面那张表可能
    只是在测短期动量,和机构一点关系都没有,那它就不是「新的信息维度」,
    只是动量换了个说法。

    做法:先按「顺机构方向的近 20 日收益」分 3 档,每档内部再按 edge 分 3 档,
    看 edge 的高低差在**每一档动量之内**是否还在。

    对照组:同样的 3×3,但把 edge 换成**纯价格位置**(现价相对近 60 日均价),
    它不含任何机构信息。如果两张表长得一样,那 edge 里就没有机构的东西。
    """
    book = group_book(code)
    px = book["settle"]
    side = np.sign(book["net"])
    sigma = px.diff().rolling(20, min_periods=10).std()
    entry = book["open"].shift(-1)

    edge = side * (book["cost"] - entry) / sigma
    past = side * (px / px.shift(20) - 1)                 # 顺机构方向的近 20 日收益
    # 对照:不含机构信息的纯价格位置(现价离 60 日均价多少个 σ,同样带方向)
    plain = side * (px.rolling(60, min_periods=30).mean() - entry) / sigma
    fwd = px.shift(-10) / px - 1
    r = side * fwd

    df = pd.DataFrame({"edge": edge, "plain": plain, "past": past, "r": r})
    df = df[book["known"] == 1].dropna()
    if len(df) < 300:
        print(f"  {code}: 样本 {len(df)},不足以做二维排序")
        return

    print(f"\n  {H.VARIETIES[code]['name']}  样本 {len(df):,}  (10 日前瞻)")
    print(f"    edge 与近 20 日收益的相关系数: {df['edge'].corr(df['past']):+.2f}"
          f"   ← 越接近 −1 说明 edge 几乎就是动量的反面")
    for name, col in (("edge(含机构成本)", "edge"), ("对照:纯价格位置", "plain")):
        try:
            pb = pd.qcut(df["past"], 3, labels=["跌", "平", "涨"], duplicates="drop")
        except ValueError:
            print("    近期收益分不出档,跳过")
            return
        print(f"    {name}")
        header = "      动量档 |" + "".join(f"  {q:>8s}" for q in ("低", "中", "高")) + "   高−低"
        print(header)
        for m in ("跌", "平", "涨"):
            sub = df[pb == m]
            if len(sub) < 30:
                continue
            try:
                qb = pd.qcut(sub[col], 3, labels=["低", "中", "高"], duplicates="drop")
            except ValueError:
                continue
            g = sub.groupby(qb, observed=True)["r"].mean()
            cells = "".join(f"  {g.get(q, np.nan) * 100:+7.2f}%" for q in ("低", "中", "高"))
            spread = (g.get("高", np.nan) - g.get("低", np.nan)) * 100
            print(f"      {m:>6s} |{cells}   {spread:+.2f}%")


def main() -> None:
    print(__doc__.split("\n")[0])
    print("\n**所有结论都建立在推算成本上,不是成交均价。**")
    for code in CODES:
        try:
            report(code)
        except Exception as exc:                      # noqa: BLE001
            print(f"\n{code}: 跑不动 —— {type(exc).__name__}: {exc}")
    print(f"\n{'=' * 88}")
    print("怎么读这张表:")
    print("  · A 覆盖率低 → 规则大部分日子无法判定,后面不用做了。")
    print("  · B 均值为负 → 「跟随机构持仓方向」这个地基本身不成立。")
    print("  · C 不单调  → 成本位置没有预测力,「优于机构成本才进场」没有依据。")
    print("  t 值全部基于**重叠样本**,系统性高估;这一步只看方向与量级,")
    print("  真要下结论得做聚类去重叠 + 样本外切分,那是下一步的事。")

    print(f"\n{'=' * 88}")
    print("D 动量对照:在「近期涨跌」分档之内,edge 还剩多少?")
    print("=" * 88)
    print("  「高−低」那一列若在三个动量档里都接近 0,说明 edge 只是动量的另一种写法,")
    print("  机构成本没有带来新信息;若它稳定为负(高 edge = 机构浮亏 → 后续更差),")
    print("  且明显强于右边那张纯价格对照表,机构成本才算真的多说了一句话。")
    for code in CODES:
        try:
            control(code)
        except Exception as exc:                      # noqa: BLE001
            print(f"  {code}: 跑不动 —— {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
