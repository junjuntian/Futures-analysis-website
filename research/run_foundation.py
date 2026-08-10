# -*- coding: utf-8 -*-
"""地基核验:数据健全性、主力序列、AU 大波段、席位序列一致性。"""
import sys

import numpy as np
import pandas as pd

import aulib

pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 100)


def main():
    price = aulib.load_price()
    seat = aulib.load_seat()

    print("== 行情健全性 ==")
    print(f"行数 {len(price)}, 日期 {price.trade_date.min().date()} ~ {price.trade_date.max().date()}, "
          f"合约数 {price.contract.nunique()}, 交易日数 {price.trade_date.nunique()}")
    dup = price.duplicated(["contract", "trade_date"]).sum()
    print(f"合约×日期重复行: {dup}")

    mc = aulib.main_contract(price)
    switches = mc[mc["main"] != mc["main"].shift(1)]
    print(f"\n== 主力合约 == 切换 {len(switches) - 1} 次;近 6 次:")
    print(switches.tail(6).to_string(index=False))

    cont = aulib.continuous_series(price, mc)
    print(f"\n== 连续序列 == 收益缺失 {cont.ret.isna().sum()} 天;"
          f"|r|>7% 天数 {(cont.ret.abs() > 0.07).sum()};最新真实收盘 {cont.close.iloc[-1]:.2f}")

    zz = aulib.zigzag(cont, 0.10)
    zz_show = zz.copy()
    zz_show["from"] = zz_show["from"].dt.date
    zz_show["to"] = zz_show["to"].apply(lambda x: x.date() if pd.notna(x) else "进行中")
    print(f"\n== AU 主力(复权) ±10% 大波段 == 共 {len(zz)} 段")
    print(zz_show.to_string(index=False))

    n_vt = int(seat["is_variety_total"].sum())
    print(f"\n== 汇总行实态 == 库内 SHFE 品种汇总行 {n_vt} 行(设计应自算灌入,尚未实现;研究管线自行聚合,不阻塞)")

    print("\n== 八席位覆盖(别名合并后) ==")
    rows = []
    for m in aulib.FOCUS:
        s = aulib.member_variety_series(seat, m)
        n_evt = (s["dnet"].abs() > 0).sum()
        rows.append({"席位": m, "上榜天数": len(s), "首日": s.index.min().date() if len(s) else None,
                     "末日": s.index.max().date() if len(s) else None,
                     "有增减动作天数": int(n_evt),
                     "净仓中位": float(s["net"].median()) if len(s) else np.nan})
    print(pd.DataFrame(rows).to_string(index=False))

    aulib.OUT.mkdir(exist_ok=True)
    cont.to_pickle(aulib.OUT / "au_continuous.pkl")
    zz.to_pickle(aulib.OUT / "au_zigzag10.pkl")
    mc.to_pickle(aulib.OUT / "au_main.pkl")
    print("\n已写出 out/au_continuous.pkl, au_zigzag10.pkl, au_main.pkl")


if __name__ == "__main__":
    sys.exit(main())
