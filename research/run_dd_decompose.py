"""回撤为什么从 −19% 变成 −43%:把两个原因拆开。

运营者 2026-08-19 问:「之前测出散户席位是很好的反向指标,这次为什么回撤这么大?」

两个原因混在一起了,必须分开算:
  ①**旧的回撤数字本来就是错的** —— 旧版逐日净值在 build_payload 里用
    `pos.shift(1) × 结算价收益` 另算一条,与逐笔记账不是同一条曲线(DEC-090)。
  ②**成交口径从「信号日结算价」改成「次日开盘」**,收益真的少了。

做法:跑旧引擎(git 48ae390)拿旧成交口径的逐笔,再用**正确的**日净值构造法
重算一遍它的回撤 —— 中间这一档就把两个原因分开了。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "engine"))
os.environ.setdefault("ENGINE_SOURCE", "csv")
import hog_money as H  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"
SC = pathlib.Path(os.environ["SCRATCH"])
COST = 0.0005
NAMES = {"LH": "生猪", "FG": "玻璃", "SA": "纯碱"}
FILES = {"LH": "hog_signals.json", "FG": "fg_signals.json", "SA": "sa_signals.json"}


def mkt_of(code: str) -> pd.DataFrame:
    H.use(code)
    price, seat = H.load_from_csv(code, DATA)
    price, seat = H.clean_price(price), H.clean_seat(seat)
    m = H.main_series(price)
    return m[m.index >= pd.Timestamp(H.RULES["replay_start"])]


def curve(tr: list[dict], mkt: pd.DataFrame, clock: str) -> dict:
    """按持仓区间铺日收益。clock='settle' 走结→结(旧成交口径),'open' 走开→开。"""
    idx = mkt.index
    loc = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(idx)}
    r = mkt["ret" if clock == "settle" else "ret_open"].fillna(0.0).to_numpy()
    daily = pd.Series(0.0, index=idx)
    for t in tr:
        if not t.get("exit_date"):
            continue
        i0, j0 = loc[t["entry_date"]], loc[t["exit_date"]]
        sd = 1.0 if t["side"] == "long" else -1.0
        # 旧口径:信号日结算价成交 → 吃 ret[i0+1 .. j0]
        # 新口径:次日开盘成交     → 吃 ret_open[i0+2 .. j0+1]
        lo, hi = (i0 + 1, j0) if clock == "settle" else (i0 + 2, j0 + 1)
        for k in range(lo, min(hi, len(idx) - 1) + 1):
            daily.iloc[k] = sd * r[k]
        for k in (lo, min(hi, len(idx) - 1)):
            daily.iloc[k] -= COST
    eq = (1 + daily).cumprod()
    return {"cum": (float(eq.iloc[-1]) - 1) * 100,
            "dd": float((eq / eq.cummax() - 1).min()) * 100,
            "sharpe": float(daily.mean() / daily.std() * np.sqrt(242)) if daily.std() > 0 else float("nan"),
            "expo": float((daily != 0).mean()) * 100}


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    print("=" * 96)
    print("回撤为什么变大:两个原因拆开算")
    print("=" * 96)
    print(f"  {'品种':6s}{'口径':34s}{'累计%':>10s}{'最大回撤%':>11s}{'夏普':>7s}{'在场%':>7s}")
    for c in ("LH", "FG", "SA"):
        mkt = mkt_of(c)
        old = load(SC / "oldout" / FILES[c])
        new = load(SC / "flowout" / FILES[c])
        rows = [
            ("① 旧口径 + 旧(错的)日净值 = 上线过的数字",
             {"cum": old["compare"]["strategy"]["cum_pct"],
              "dd": old["compare"]["strategy"]["max_dd_pct"],
              "sharpe": old["compare"]["strategy"]["sharpe"], "expo": float("nan")}),
            ("② 旧口径 + 正确日净值", curve(old["history"], mkt, "settle")),
            ("③ 新口径(次日开盘)+ 正确日净值", curve(new["history"], mkt, "open")),
        ]
        for i, (tag, v) in enumerate(rows):
            head = f"  {NAMES[c]:6s}" if i == 0 else " " * 8
            ex = "     —" if not np.isfinite(v["expo"]) else f"{v['expo']:>7.0f}"
            print(f"{head}{tag:34s}{v['cum']:>+10.1f}{v['dd']:>+11.1f}{v['sharpe']:>7.2f}{ex}")
        print()


if __name__ == "__main__":
    main()
