# -*- coding: utf-8 -*-
"""第二层:卖点搜索。对 walk-forward OOS 的信号(同一批进场),并列对比五种出场:

E1 固定目标: +10% / 止损3% / 超时120        (基准,运营者举例的"一轮")
E2 追踪5%:  从进场后最高价回撤5%出;初始止损3%
E3 结构出场: 收盘跌破MA20出;硬止损3%
E4 保本推进: 浮盈达+5%后止损上移至成本;目标+10%
E5 利润奔跑: 无目标,追踪6%;硬止损3%
同日冲突一律先按不利处理(先止损)。进场价与 OOS 交易一致(复用其进场日/价)。
"""
import sys

import numpy as np
import pandas as pd

import aulib

pd.set_option("display.width", 220)
MAXHOLD = 250  # 放宽,让 E5 有空间


def replay(cont, entries, mode):
    dates = cont.index.to_list()
    pos = {d: i for i, d in enumerate(dates)}
    hi, lo, cl = cont["adj_high"].to_numpy(), cont["adj_low"].to_numpy(), cont["adj_close"].to_numpy()
    ma20 = cont["adj_close"].rolling(20).mean().to_numpy()
    out = []
    for _, e in entries.iterrows():
        i0 = pos.get(pd.Timestamp(e["进场日"]))
        if i0 is None:
            continue
        p0 = e["进场价"]
        stop_px = p0 * 0.97
        tgt_px = p0 * 1.10
        peak = p0
        exit_px = reason = None
        for i in range(i0, min(i0 + MAXHOLD, len(dates))):
            l_, h_, c_ = lo[i], hi[i], cl[i]
            if np.isnan(l_) or np.isnan(h_):
                continue
            # 先不利:止损/追踪线
            trail = None
            if mode == "E2":
                trail = peak * 0.95
            elif mode == "E5":
                trail = peak * 0.94
            eff_stop = max(stop_px, trail) if trail is not None else stop_px
            if l_ <= eff_stop:
                exit_px, reason = eff_stop, ("止损" if eff_stop == stop_px else "追踪")
                break
            if mode in ("E1", "E4") and h_ >= tgt_px:
                exit_px, reason = tgt_px, "目标"
                break
            if mode == "E3" and not np.isnan(ma20[i]) and c_ < ma20[i]:
                exit_px, reason = c_, "破MA20"
                break
            peak = max(peak, h_)
            if mode == "E4" and peak >= p0 * 1.05:
                stop_px = max(stop_px, p0)  # 保本
        if exit_px is None:
            i = min(i0 + MAXHOLD, len(dates)) - 1
            exit_px, reason = cl[i], "超时"
        out.append({"进场日": e["进场日"], "收益%": (exit_px / p0 - 1 - 0.001) * 100, "出场": reason})
    return pd.DataFrame(out)


def main():
    cont = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    oos = pd.read_pickle(aulib.OUT / "oos_trades.pkl")
    if oos.empty:
        print("无 OOS 交易")
        return 1
    print(f"OOS 进场 {len(oos)} 笔,五种出场并列(同一批进场,只有卖法不同):\n")
    rows = []
    detail = {}
    for mode, name in [("E1", "固定+10%/止损3%"), ("E2", "追踪5%"), ("E3", "破MA20"),
                       ("E4", "保本推进+目标10%"), ("E5", "利润奔跑(追踪6%)")]:
        tr = replay(cont, oos, mode)
        detail[mode] = tr
        rows.append({"出场": f"{mode} {name}", "笔数": len(tr),
                     "均收益%": round(tr["收益%"].mean(), 2), "中位%": round(tr["收益%"].median(), 2),
                     "总收益%": round(tr["收益%"].sum(), 1),
                     "胜率%": round((tr["收益%"] > 0).mean() * 100, 1),
                     "最大单笔%": round(tr["收益%"].max(), 1), "最差单笔%": round(tr["收益%"].min(), 1)})
    print(pd.DataFrame(rows).to_string(index=False))
    pd.to_pickle(detail, aulib.OUT / "exit_compare.pkl")
    print("\n已写出 out/exit_compare.pkl")


if __name__ == "__main__":
    sys.exit(main())
