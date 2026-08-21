"""把「当天那一格」的前视摘掉,回测掉多少?—— 三个口径的对照。

`run_reboard_lookahead.py` 量出「整行排除反推」会让玻璃 −86%、纯碱 −76%。
但那**排过头了**:实测反推行的可见滞后**恒为 1 个交易日,100% 如此**
(玻璃 5.0 万条、纯碱 3.1 万条、鸡蛋 7.5 千条,最长都是 1)。也就是说 ——

  · 第 T 日收盘后引擎算信号时,**当天**掉榜席位的反推值还不存在
    (要等 T+1 收盘、看到他的增减才推得出);
  · 但 T−1 及更早的反推值,在 T 日**确实已经可见**了,用它们不是前视。

所以真正的前视只在**当天那一格**。而引擎的信号是 `net.diff(sig_win)` ——
它一头正好踩在这一格上。

三个口径:
  1 **生产**      全部用 —— 当天那一格也用,含前视(上界)
  2 **PIT 正确**  当天只用官方行,历史用全部 —— 这才是实盘那天能拿到的
  3 全排除        连历史上早已可见的也不用 —— 过于保守(下界),仅作参照

PIT 的实现:`chg(T) = net_当日官方(T) − net_全量(T − sig_win)`。
滞后恒为 1 天,所以 T−sig_win 那一格在 T 日必定已可见,可以放心用全量。
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


def signal_pit(seat_full: pd.DataFrame, seat_off: pd.DataFrame,
               groups: pd.Series) -> pd.DataFrame:
    """与 `signal_series` 同一套分组逻辑,只把「当天那一格」换成官方行。

    **逐字对着 signal_series 写**,只改 chg 的被减数 —— 口径一分叉这个对比就废了。
    换组当天不 diff 的处理也照搬:新旧两组持仓水平不同,那会把「换了一批人」
    当成「机构大幅调仓」。
    """
    net = pd.Series(index=groups.index, dtype=float)
    chg = pd.Series(index=groups.index, dtype=float)
    for grp in {g for g in groups.dropna().unique()}:
        days = groups.index[groups == grp]
        members = list(grp)
        s_full = (seat_full[seat_full["member_key"].isin(members)]
                  .groupby("trade_date")["net"].sum().sort_index())
        s_off = (seat_off[seat_off["member_key"].isin(members)]
                 .groupby("trade_date")["net"].sum().sort_index())
        # 当天用官方口径;被减数(sig_win 天前)用全量 —— 那一格在今天必定已可见。
        cur = s_off.reindex(days)
        lag = s_full.shift(H.RULES["sig_win"]).reindex(days)
        net.loc[days] = cur.values
        chg.loc[days] = (cur - lag).values
    z = chg / chg.rolling(H.RULES["z_win"], min_periods=60).std()
    return pd.DataFrame({"net": net, "chg": chg, "z": z})


def run(code: str, mode: str) -> dict:
    H.use(code)
    low = code.lower()
    price_raw = pd.read_csv(DATA / f"{low}_price.csv.gz")
    seat_raw = pd.read_csv(DATA / f"{low}_seat.csv.gz")
    off_raw = seat_raw[seat_raw["source"] != "reboard_inferred"].copy()

    price = H.clean_price(price_raw)
    seat_full = H.clean_seat(seat_raw)
    seat_off = H.clean_seat(off_raw)
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]

    # **选组一律用全量**:每年一次的重选发生在历史上,那时反推值早已可见。
    # 把选组也换成官方口径,就把「换了一批人」这个无关变量混进来了。
    groups, _, _ = H.rolling_groups(seat_full, price, mkt.index)

    if mode == "prod":
        sig = H.signal_series(seat_full, groups)
        rdf, _ = H.retail_series(seat_full, mkt.index)
    elif mode == "pit":
        sig = signal_pit(seat_full, seat_off, groups)
        rdf, _ = H.retail_series(seat_off, mkt.index)     # 散户那路同样只用当日官方
    else:
        sig = H.signal_series(seat_off, groups)
        rdf, _ = H.retail_series(seat_off, mkt.index)

    trades, _, daily = H.replay(sig, mkt, rdf, op, st)
    closed = [t for t in trades if t["exit_date"]]
    cum = (float(np.prod([1 + t["ret_pct"] / 100 for t in closed])) - 1) * 100 if closed else 0.0
    eq = (1 + daily).cumprod()
    return {"n": len(closed), "cum": cum,
            "win": sum(1 for t in closed if t["ret_pct"] > 0) / max(len(closed), 1) * 100,
            "dd": float((eq / eq.cummax() - 1).min() * 100)}


def main() -> None:
    print(__doc__.split("\n")[0])
    print("\n可见滞后实测恒为 1 个交易日 —— 前视只在「当天那一格」,不是整段历史。\n")
    print(f"{'品种':10s}{'口径':16s}{'笔数':>6s}{'累计':>11s}{'胜率':>8s}{'回撤':>9s}{'相对生产':>11s}")
    print("=" * 76)
    for code in CODES:
        base = run(code, "prod")
        rows = [("1 生产(含前视)", base), ("2 **PIT 正确**", run(code, "pit")),
                ("3 全排除(过保守)", run(code, "drop"))]
        for i, (name, r) in enumerate(rows):
            rel = "" if i == 0 else f"{(r['cum'] - base['cum']) / abs(base['cum']) * 100:>+10.0f}%"
            head = H.VARIETIES[code]["name"] if i == 0 else ""
            print(f"{head:10s}{name:16s}{r['n']:>6d}{r['cum']:>+10.1f}%"
                  f"{r['win']:>7.1f}%{r['dd']:>8.1f}%{rel:>11s}")
        print("-" * 76)
    print("\n第 2 行才是实盘那天真能拿到的口径。它与第 1 行的差 = 这条前视的真实贡献。")


if __name__ == "__main__":
    main()
