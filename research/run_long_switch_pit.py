"""做多开关在**修掉回榜前视之后**重扫(DEC-108 遗留)。

`VARIETIES` 里那组比较 ——

    生猪 关 2.25/t+2.59 · 开+dip 1.93/t+2.40 · 开−dip 1.57/t+2.15   → 关
    焦煤 关 1.11/t+2.53 · 开−dip 0.89/t+1.79 · 开+dip 0.69/t+1.43   → 关
    玻璃 开−dip 0.65/t+2.37 · 开+dip 0.50/t+1.78 · 关 0.36/t+1.33   → 开、不要 dip

**全部是修前视之前算的,已作废** —— 玻璃修后双向只剩 0.21。三个开着做多的品种
(FG/SA/JD)都要在新口径上重来一遍,关着的两个(LH/JM)也顺带确认没翻。

报同一组指标(夏普/t)好跟旧记录对照,另加累计、回撤与**逐年符号** ——
`docs/PITFALLS.md`:玻璃曾全样本 t=+2.96 而逐年 7 正 7 负,只看全样本会选错。

**这是参数选择,纪律照旧**:三档之间差在噪音量级内就维持现状,不为了「更好看的
数」去翻开关。DEC-096 时鸡蛋那次就写明了「这属于样本内选择,别写成实测最优」。
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
CONFIGS = [("关", False, True), ("开−dip", True, False), ("开+dip", True, True)]


def one(code: str, enabled: bool, needs_dip: bool) -> dict:
    v = H.use(code)
    H.CURRENT = {"code": code, **v}
    H.RULES["long_enabled"] = enabled
    H.RULES["long_needs_dip"] = needs_dip
    low = code.lower()
    price = H.clean_price(pd.read_csv(DATA / f"{low}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(DATA / f"{low}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, _, _ = H.rolling_groups(seat, price, mkt.index)
    sig = H.signal_series(seat, groups)
    rdf, _ = H.retail_series(seat, mkt.index)
    trades, _, daily = H.replay(sig, mkt, rdf, op, st)

    closed = [t for t in trades if t["exit_date"]]
    r = np.array([t["ret_pct"] for t in closed]) if closed else np.array([])
    t_stat = (r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))
              if len(r) > 2 and r.std(ddof=1) > 0 else np.nan)
    eq = (1 + daily).cumprod()
    ann = daily.mean() * 250
    sharpe = ann / (daily.std() * np.sqrt(250)) if daily.std() > 0 else np.nan
    yr: dict[int, list[float]] = {}
    for tr in closed:
        yr.setdefault(int(tr["exit_date"][:4]), []).append(tr["ret_pct"])
    ysign = {y: (float(np.prod([1 + x / 100 for x in v])) - 1) for y, v in yr.items()}
    return {"n": len(closed), "sharpe": sharpe, "t": t_stat,
            "cum": (float(eq.iloc[-1]) - 1) * 100,
            "dd": float((eq / eq.cummax() - 1).min() * 100),
            "pos_years": sum(1 for x in ysign.values() if x > 0), "years": len(ysign)}


def main() -> None:
    print(__doc__.split("\n")[0])
    print("\n**旧记录全部作废**(修前视之前算的)。下面是新口径。\n")
    for code in CODES:
        cur = H.VARIETIES[code]
        now = "开−dip" if (cur["long_enabled"] and not cur["long_needs_dip"]) else (
            "开+dip" if cur["long_enabled"] else "关")
        rows = [(name, one(code, en, dip)) for name, en, dip in CONFIGS]
        rows.sort(key=lambda kv: -(kv[1]["sharpe"] if np.isfinite(kv[1]["sharpe"]) else -9))
        best = rows[0][0]
        line = " · ".join(
            f"{n} {r['sharpe']:.2f}/t{r['t']:+.2f}" for n, r in rows)
        flag = "" if best == now else f"   ← 最优变成「{best}」,现行是「{now}」"
        print(f"{cur['name']}  现行:{now}")
        print(f"  {line}{flag}")
        for n, r in rows:
            mark = " ←现行" if n == now else ""
            print(f"    {n:<7s} {r['n']:>4d} 笔  累计 {r['cum']:>+8.1f}%  回撤 {r['dd']:>6.1f}%"
                  f"  逐年为正 {r['pos_years']}/{r['years']}{mark}")
        print()
    print("判定纪律:三档差在噪音量级内就**维持现状**;逐年符号不稳的不采纳。")
    print("翻开关是样本内选择 —— 采纳也要如实写成「运营者知情后拍板」,不是「实测最优」。")


if __name__ == "__main__":
    main()
