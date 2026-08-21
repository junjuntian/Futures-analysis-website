"""跟随机构建仓:三个前提逐个验 —— 上一轮测错了对象,这一轮重来。

运营者 2026-08-21 指出我上一轮的检验答非所问:

> 前期机构进场，这些机构是赚钱的机构，前期进场跟机构成本差不多，后续机构补仓，
> 我们也补仓，保持成本优于机构……因为机构进场不是一天能进完的，出场也不是一天
> 能出完的。所以才给我们小资金跟随的机会。

**他是对的。** `REPORT_INST_COST_v1` 测的是「任意一天,我的入场价相对机构成本
的位置」—— 一个**横截面的静态比较**。那个 edge 在随机某天上主要捕捉「价格已经
走了多远」,所以动量一控制就没了。它从来没测到「建仓早期跟入、随他补仓」这个
**动态过程**。两者是不同的假说。

这一轮拆成三个前提,任何一个不成立,整条思路就断在那里:

  A **机构建仓/出货不是一天完成的** —— 没有跨度就没有跟随的窗口。
  B **这些机构确实赚钱** —— 跟一个不赚钱的人,成本再优也没用。
  C **跟随真能保持成本优势** —— 这条我事先就存疑:**我们永远晚一天**。
     席位数据盘后才出,最早只能次日开盘成交(DEC-090)。建仓期价格若顺着他们走,
     我们每一笔补仓都比他贵,成本只会**劣于**他而不是优于。这一条必须用数据回答,
     不能靠想当然 —— 两个方向我都可能猜错。

口径:主力合约、`rolling_groups` 选出的那 5 家、排除 `reboard_inferred`。
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
from run_inst_cost import group_book  # noqa: E402

CODES = ["LH", "FG", "SA", "JD", "JM"]


def regimes(net: pd.Series) -> list[tuple[int, int]]:
    """把净持仓切成一段段「同方向」的区间,返回 (起, 止) 的位置下标(闭区间)。

    掉榜(NaN)不切断 —— 那是「不知道」不是「这一轮结束了」。
    只有真归零或方向翻转才开新一轮。
    """
    out = []
    cur = 0
    start = None
    arr = net.to_numpy()
    for i, n in enumerate(arr):
        if not np.isfinite(n):
            continue
        s = 0 if n == 0 else int(np.sign(n))
        if s != cur:
            if start is not None and cur != 0:
                out.append((start, i - 1))
            cur, start = s, (i if s != 0 else None)
    if start is not None and cur != 0:
        out.append((start, len(arr) - 1))
    return out


def follow(book: pd.DataFrame, a: int, b: int) -> dict | None:
    """在一轮建仓里跟着补,算出「我们的成本」与「他们的成本」。

    规则(尽量贴运营者描述,且**不许用未来数据**):
      · 他每加一次仓,我们同比例加一次;他减仓我们不动均价(与成本引擎同口径);
      · **我们晚一天**:今天盘后看到他加仓,最早明天开盘成交(DEC-090);
      · 他的成本按当日结算价推(与页面、与 seat_cost 同口径)。
    """
    net = book["net"].to_numpy()[a:b + 1]
    op = book["open"].to_numpy()[a:b + 1]
    st = book["settle"].to_numpy()[a:b + 1]
    sign = 1.0 if net[0] > 0 else -1.0

    our_lots = our_cost = 0.0
    their_lots = their_cost = 0.0
    prev = 0.0
    for i, n in enumerate(net):
        if not np.isfinite(n):
            continue
        add = abs(n) - abs(prev)
        prev = n
        if add <= 0:
            continue                     # 减仓不改均价
        # 他们:当日结算价
        if np.isfinite(st[i]):
            their_cost = (their_cost * their_lots + st[i] * add) / (their_lots + add)
            their_lots += add
        # 我们:次日开盘(晚一天)。最后一天没有次日,跟不上
        if i + 1 < len(op) and np.isfinite(op[i + 1]):
            our_cost = (our_cost * our_lots + op[i + 1] * add) / (our_lots + add)
            our_lots += add
    if our_lots == 0 or their_lots == 0:
        return None
    # **出场也要次日开盘**,不能拿当日结算价。进场被罚了一天而出场不罚,
    # 等于给结果白送一段;DEC-090 那条纪律两头都得守。
    # 这一轮的最后一天之后若没有开盘价(数据末尾),这一轮不计。
    nxt = b + 1
    ex = book["open"].to_numpy()
    exit_px = ex[nxt] if nxt < len(ex) and np.isfinite(ex[nxt]) else np.nan
    their_exit = st[-1] if np.isfinite(st[-1]) else np.nan
    if not np.isfinite(exit_px) or not np.isfinite(their_exit):
        return None
    return {
        "days": b - a + 1,
        "their_cost": their_cost,
        "our_cost": our_cost,
        # 成本优势:做多=我们更便宜为正;做空=我们卖得更贵为正
        "edge_pct": sign * (their_cost - our_cost) / their_cost * 100,
        # 他们按自己的口径(结算价)结算,我们按能成交的次日开盘 —— 各算各的。
        "their_ret_pct": sign * (their_exit - their_cost) / their_cost * 100,
        "our_ret_pct": sign * (exit_px - our_cost) / our_cost * 100,
    }


def report(code: str) -> None:
    book = group_book(code)
    net = book["net"]
    segs = regimes(net)
    print(f"\n{'=' * 92}")
    print(f"{H.VARIETIES[code]['name']}   完整仓位轮次 {len(segs)} 段")
    print("=" * 92)
    if not segs:
        print("  没有可用轮次")
        return

    # ---- A 建仓/出货有没有跨度 ----
    builds, unwinds = [], []
    for a, b in segs:
        seg = net.to_numpy()[a:b + 1]
        fin = np.where(np.isfinite(seg))[0]
        if len(fin) < 3:
            continue
        pk = fin[np.argmax(np.abs(seg[fin]))]
        builds.append(pk + 1)
        unwinds.append(len(seg) - pk)
    if builds:
        print(f"  A 建仓天数 中位 {int(np.median(builds))}、四分位 "
              f"[{int(np.percentile(builds, 25))}, {int(np.percentile(builds, 75))}];"
              f" 出货天数 中位 {int(np.median(unwinds))}、四分位 "
              f"[{int(np.percentile(unwinds, 25))}, {int(np.percentile(unwinds, 75))}]")

    # ---- B 他们自己赚不赚钱 ----
    rows = [r for a, b in segs if (r := follow(book, a, b))]
    if not rows:
        print("  B/C 没有可算的轮次")
        return
    df = pd.DataFrame(rows)
    print(f"  B 他们自己:{len(df)} 轮  平均 {df['their_ret_pct'].mean():+.2f}%  "
          f"中位 {df['their_ret_pct'].median():+.2f}%  "
          f"赚钱轮次 {(df['their_ret_pct'] > 0).mean():.0%}")

    # ---- C 跟随能不能保持成本优势 ----
    print(f"  C 我们跟随(晚一天):成本优势平均 {df['edge_pct'].mean():+.3f}%  "
          f"中位 {df['edge_pct'].median():+.3f}%  "
          f"**优于他的轮次 {(df['edge_pct'] > 0).mean():.0%}**")
    # 扣手续费:一轮进出两次,单边 COST(引擎同一个常量)。不扣的话结论偏乐观,
    # 而这几个数的量级本来就在半个百分点上下,手续费不是可以忽略的零头。
    fee = 2 * H.COST * 100
    net_ret = df["our_ret_pct"] - fee
    print(f"    我们的收益(**已扣 {fee:.1f}% 往返手续费**) 平均 {net_ret.mean():+.2f}%  "
          f"中位 {net_ret.median():+.2f}%  赚钱轮次 {(net_ret > 0).mean():.0%}")
    keep = net_ret.mean() / df["their_ret_pct"].mean() if df["their_ret_pct"].mean() else np.nan
    print(f"    → 吃到他们利润的 {keep:.0%}"
          f"(他们 {df['their_ret_pct'].mean():+.2f}% → 我们净 {net_ret.mean():+.2f}%)")


def main() -> None:
    print(__doc__.split("\n")[0])
    print("\n三个前提,任何一个不成立整条思路就断在那里:")
    print("  A 建仓/出货要有跨度(否则没有跟随窗口)")
    print("  B 这些机构确实赚钱(跟一个不赚钱的人,成本再优也没用)")
    print("  C 跟随真能保持成本优势(**我们永远晚一天**,这条我事先存疑)")
    for code in CODES:
        try:
            report(code)
        except Exception as exc:                       # noqa: BLE001
            print(f"\n{code}: 跑不动 —— {type(exc).__name__}: {exc}")
    print(f"\n{'=' * 92}")
    print("注意:这里算的是**每一轮仓位的整体收益**,不是逐日前瞻收益 —— 与前两轮")
    print("不同类,不能直接比。轮次数少,均值受个别大轮次影响大,要一起看中位与胜率。")


if __name__ == "__main__":
    main()
