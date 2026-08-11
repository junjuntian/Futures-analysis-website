# -*- coding: utf-8 -*-
"""第三条腿:金银比配对策略(运营者 2026-08-11 授意,1 月案例验证后)。

比值口径:COMEX 金/银连续(≈伦敦 XAU/XAG,运营者指定国际口径),z=250日。
时差防未来:沪市 T 日收盘决策时 COMEX T 日未收盘,z 取 COMEX T-1(shift 1)。
入场(T+1 沪市开盘,等值配平):
  多金空银:z < -2(银极贵)且 近5日八家 AG「增空事件」>=1(机构扳机)
  多银空金:z > +2(金极贵)且 近5日八家 AU「增空事件」>=1
出场(先触发者):|z| 回归 <0.5(收盘确认 T+1 开盘平)/ 60 交易日超时 / 配对净值 -4% 止损。
执行标的:沪金/沪银主力(复权),配对收益 = 多腿收益 − 空腿收益(等值)。
"""
import sys

import numpy as np
import pandas as pd

import aulib
from v50_goldsilver import member_day_au, neg_events, EXTRA_ALIAS, GROUP8
import v46_silver

pd.set_option("display.width", 250)


def main():
    cont_au = pd.read_pickle(aulib.OUT / "au_continuous.pkl")
    _, _, cont_ag, md_ag, _ = v46_silver.prep("ag")
    md_ag["member"] = md_ag["member"].replace(EXTRA_ALIAS)
    md_au = member_day_au()

    cx = pd.read_csv(aulib.DATA / "comex_gold_silver.csv", index_col=0, parse_dates=True)
    z_raw = (cx["ratio"] - cx["ratio"].rolling(250).mean()) / cx["ratio"].rolling(250).std()
    z = z_raw.shift(1)  # 时差防未来

    sh_ag = neg_events(md_ag, cont_ag, GROUP8)
    sh_au = neg_events(md_au, cont_au, GROUP8)

    common = cont_au.index.intersection(cont_ag.index)
    au = cont_au.loc[common]
    ag = cont_ag.loc[common]
    dates = common
    pos = {d: i for i, d in enumerate(dates)}
    zc = pd.Series([z.asof(d) for d in dates], index=dates)

    def recent_short(evd, d):
        w = evd[(evd["trade_date"] > d - pd.Timedelta(days=8)) & (evd["trade_date"] <= d)]
        return sorted(set(w["member"]))

    au_o, au_c = au["adj_open"].to_numpy() if "adj_open" in au else au["adj_close"].to_numpy(), au["adj_close"].to_numpy()
    ag_o, ag_c = ag["adj_open"].to_numpy() if "adj_open" in ag else ag["adj_close"].to_numpy(), ag["adj_close"].to_numpy()
    # adj_open 兜底
    if "adj_open" not in au:
        au_o = au_c
    if "adj_open" not in ag:
        ag_o = ag_c

    trades, busy = [], -1
    for d in dates:
        i = pos[d]
        if i + 1 >= len(dates) or i < busy:
            continue
        zv = zc[d]
        if not (zv == zv):
            continue
        side = trig = None
        if zv < -2:
            t_ = recent_short(sh_ag, d)
            if t_:
                side, trig = "多金空银", t_
        elif zv > 2:
            t_ = recent_short(sh_au, d)
            if t_:
                side, trig = "多银空金", t_
        if side is None:
            continue
        i0 = i + 1
        la, sa = (au_o[i0], ag_o[i0]) if side == "多金空银" else (ag_o[i0], au_o[i0])
        if np.isnan(la) or np.isnan(sa):
            continue
        long_leg = au_c if side == "多金空银" else ag_c
        short_leg = ag_c if side == "多金空银" else au_c
        exit_i = reason = None
        pnl = 0.0
        for j in range(i0, min(i0 + 60, len(dates))):
            pnl = (long_leg[j] / la - 1) - (short_leg[j] / sa - 1)
            if pnl <= -0.04:
                reason, exit_i = "止损4%", j
                break
            zj = zc[dates[j]]
            if zj == zj and abs(zj) < 0.5:
                if j + 1 < len(dates):
                    jn = j + 1
                    lo_, so_ = (au_o[jn], ag_o[jn]) if side == "多金空银" else (ag_o[jn], au_o[jn])
                    pnl = (lo_ / la - 1) - (so_ / sa - 1)
                    reason, exit_i = "z回归", jn
                else:
                    reason, exit_i = "z回归", j
                break
        if exit_i is None:
            exit_i = min(i0 + 60, len(dates)) - 1
            pnl = (long_leg[exit_i] / la - 1) - (short_leg[exit_i] / sa - 1)
            reason = "60日超时"
        trades.append({"信号日": d.date(), "方向": side, "z": round(zv, 2),
                       "扳机席位": "、".join(trig), "进场日": dates[i0].date(),
                       "出场日": dates[exit_i].date(), "结果": reason,
                       "配对收益%": round((pnl - 0.002) * 100, 2),
                       "持有日": exit_i - i0 + 1})
        busy = exit_i
    tr = pd.DataFrame(trades)
    print(f"== 金银比配对策略(2013-2026,z国际口径250日,机构扳机) ==")
    if len(tr):
        print(f"共 {len(tr)} 笔:胜率 {(tr['配对收益%']>0).mean()*100:.1f}%,"
              f"均 {tr['配对收益%'].mean():+.2f}%,总 {tr['配对收益%'].sum():+.1f}%,"
              f"止损 {int((tr['结果']=='止损4%').sum())} 笔,均持有 {tr['持有日'].mean():.0f} 日")
        t2 = tr.copy()
        t2["年"] = pd.to_datetime(t2["信号日"].astype(str)).dt.year
        print(t2.groupby("年").agg(笔数=("配对收益%", "size"), 均=("配对收益%", "mean"),
                                  合计=("配对收益%", "sum")).round(2).to_string())
        print("\n== 全部逐笔 ==")
        print(tr.to_string(index=False))
    tr.to_pickle(aulib.OUT / "pair_trades.pkl")


if __name__ == "__main__":
    sys.exit(main())
