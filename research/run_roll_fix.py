"""换月怎么处理:现在是「免费滚过去」,运营者指出散户做不到。

运营者 2026-08-20 指着 FG 那一行:「fg2609 触发了进场信号,最后时间不够了需要平仓,
如果 2701 的位置好,直接平完仓到 2701 开仓」。

现状的洞:DEC-089 的交割纪律查的是「**今天主力合约**还剩几天」,不是「**持仓所在
合约**还剩几天」。主力一换,检查对象就变成远月,老仓免费滚过去——FG2609 窗口止点
08-31,08-14 时只剩 11 天(差一天撞线),08-17 主力换到 FG2701,于是那笔仓从没被平掉。

三种口径,同一套信号,只改换月怎么处理:
  A 现状     —— 跟着主力走,换月即免费续命(既不付成本也不重新判断)
  B 跟合约   —— 持仓留在自己的合约里,到**它自己的**交割线才强制平
  C 换月即平 —— 主力一换就平仓,再进场是一次全新的判断(运营者描述的那种)

先复刻 A(与线上对得上)再谈别的。
"""
from __future__ import annotations

import os
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "engine"))
os.environ.setdefault("ENGINE_SOURCE", "csv")
import hog_money as H  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"
NAMES = {"LH": "生猪", "JD": "鸡蛋", "JM": "焦煤", "FG": "玻璃", "SA": "纯碱"}
COST = H.COST


def prep(code):
    H.use(code)
    price, seat = H.load_from_csv(code, DATA)
    price, seat = H.clean_price(price), H.clean_seat(seat)
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    g, _, _ = H.rolling_groups(seat, price, mkt.index)
    sig = H.signal_series(seat, g)
    rdf, _ = H.retail_series(seat, mkt.index)
    # 逐合约的开盘/结算,B/C 口径要按**持仓所在合约**计价
    px = price.assign(_o=price["open_price"].replace(0, np.nan))
    op = px.set_index(["contract", "trade_date"])["_o"].sort_index()
    st = px.set_index(["contract", "trade_date"])["settle"].sort_index()
    return mkt, sig, rdf, op, st


def replay_variant(code, mkt, sig, rdf, op, st, mode):
    """mode: 'follow_main'(=A,直接调引擎) / 'hold_contract'(B) / 'roll_exit'(C)。"""
    H.use(code)
    if mode == "follow_main":
        tr, _, daily = H.replay(sig, mkt, rdf)
        return [t for t in tr if t.get("exit_date")], daily

    idx = mkt.index
    z_in, z_out = H.entry_exit_signals(sig, rdf)
    main = mkt["main"]
    trades, side, entry_i, entry_c = [], 0, None, None
    daily = pd.Series(0.0, index=idx)

    def price_at(c, i, which):
        s = op if which == "open" else st
        v = s.get((c, idx[i]), np.nan)
        return float(v) if np.isfinite(v) else np.nan

    for i, d in enumerate(idx):
        z = z_out.get(d, np.nan)
        reason = None
        if side != 0 and i > entry_i:
            # **按持仓所在合约**算交割剩余,不是今天的主力
            dleft = H.days_to_window_end(entry_c, d)
            # 止损判据也必须连乘,且带上当天的开→结算那一截 —— 与引擎 compound()
            # 同构。用简单收益会让止损在不同的日子触发,笔数一样但记账对不上。
            hd = [k for k in range(entry_i + 1, i + 1)
                  if np.isfinite(price_at(entry_c, k, "open"))]
            vv = 1.0
            for a_i, k in zip(hd, hd[1:]):
                a, b = price_at(entry_c, a_i, "open"), price_at(entry_c, k, "open")
                if a > 0:
                    vv *= 1 + side * (b / a - 1)
            lo_ = price_at(entry_c, hd[-1], "open") if hd else np.nan
            p_now = price_at(entry_c, i, "settle")
            cum = (vv * (1 + side * (p_now / lo_ - 1)) - 1
                   if np.isfinite(lo_) and np.isfinite(p_now) and lo_ > 0 else 0.0)
            p_in = price_at(entry_c, entry_i + 1, "open")
            if dleft <= H.RULES["exit_before_delivery"]:
                reason = "临近交割"
            elif mode == "roll_exit" and main.iloc[i] != entry_c:
                reason = "换月平仓"
            elif cum <= -H.RULES["stop"]:
                reason = "止损"
            elif i - entry_i >= H.RULES["max_hold"]:
                reason = "持满"
            elif np.isfinite(z) and side * z <= -H.RULES["enter"]:
                reason = "反向"
            # 老合约没价了,只能按最后有价那天平
            if not np.isfinite(price_at(entry_c, min(i + 1, len(idx) - 1), "open")):
                reason = reason or "无成交价"
        if reason:
            p_in = price_at(entry_c, entry_i + 1, "open")
            j = i + 1
            while j < len(idx) and not np.isfinite(price_at(entry_c, j, "open")):
                j += 1
            p_out = price_at(entry_c, j, "open") if j < len(idx) else np.nan
            if np.isfinite(p_in) and np.isfinite(p_out) and p_in > 0:
                # **连乘,不用简单收益**:做空时两者不相等,而引擎其余部分全是
                # 逐日连乘口径。这条与 engine/hog_money.py 的 compound() 同构。
                dd = [k for k in range(entry_i + 1, j + 1)
                      if np.isfinite(price_at(entry_c, k, "open"))]
                vv = 1.0
                for a_i, k in zip(dd, dd[1:]):
                    a, b = price_at(entry_c, a_i, "open"), price_at(entry_c, k, "open")
                    if a > 0:
                        vv *= 1 + side * (b / a - 1)
                booked = vv - 1 - 2 * COST
                trades.append({"side": "short" if side < 0 else "long",
                               "entry_date": idx[entry_i].strftime("%Y-%m-%d"),
                               "exit_date": d.strftime("%Y-%m-%d"),
                               "contract": entry_c, "ret_pct": round(booked * 100, 2),
                               "hold_days": i - entry_i, "exit_reason": reason})
                # 日净值必须**只走这个合约真正有价的那些天**,并且用「上一个有价日」
                # 作分母 —— 中间有没成交的空档时,按自然日连乘链条会断,而记账是跨过
                # 空档的,两条就对不上。2026-08-20 第一版就是这么错的:玻璃逐笔 +442%
                # 而逐日 +2856%,差 2413 个点。恒等式对不上就是有 bug,不许往下解读。
                days = [k for k in range(entry_i + 1, min(j, len(idx) - 1) + 1)
                        if np.isfinite(price_at(entry_c, k, "open"))]
                for a_idx, k in zip(days, days[1:]):
                    a, b = price_at(entry_c, a_idx, "open"), price_at(entry_c, k, "open")
                    if a > 0:
                        daily.iloc[k] = side * (b / a - 1)
                if days:
                    daily.iloc[days[0]] -= COST
                    daily.iloc[days[-1]] -= COST
            side = 0
        ze = z_in.get(d, np.nan)
        c_now = main.iloc[i]
        if side == 0 and H.days_to_window_end(c_now, d) <= H.RULES["exit_before_delivery"]:
            continue
        if side == 0 and np.isfinite(ze) and np.isfinite(price_at(c_now, min(i + 1, len(idx) - 1), "open")):
            want = 0
            if ze <= -H.RULES["enter"]:
                want = -1
            elif ze >= H.RULES["enter"] and H.RULES["long_enabled"]:
                p = mkt["past"].get(d, np.nan)
                if (not H.RULES["long_needs_dip"]) or (np.isfinite(p) and p < 0):
                    want = 1
            if want != 0:
                side, entry_i, entry_c = want, i, c_now
    return trades, daily


def stats(tr, daily):
    if not tr:
        return None
    r = np.array([t["ret_pct"] for t in tr])
    eq = (1 + daily).cumprod()
    return {"n": len(r), "cum": (float(eq.iloc[-1]) - 1) * 100,
            "dd": float((eq / eq.cummax() - 1).min()) * 100,
            "sh": float(daily.mean() / daily.std() * np.sqrt(242)) if daily.std() > 0 else np.nan,
            "win": (r > 0).mean() * 100, "avg": r.mean(),
            "t": r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 2 else np.nan}


def main():
    print("=" * 96)
    print("换月三种口径(信号完全一样,只改换月怎么处理)")
    print("=" * 96)
    print(f"  {'品种':6s}{'口径':14s}{'笔数':>6s}{'累计%':>10s}{'回撤%':>8s}{'夏普':>7s}{'胜率%':>7s}{'单笔%':>7s}{'t值':>7s}")
    for code in ("LH", "JD", "JM", "FG", "SA"):
        mkt, sig, rdf, op, st = prep(code)
        for mode, label in (("follow_main", "A 现状(免费滚)"), ("hold_contract", "B 跟合约到交割线"),
                            ("roll_exit", "C 换月即平仓")):
            tr, daily = replay_variant(code, mkt, sig, rdf, op, st, mode)
            o = stats(tr, daily)
            head = f"  {NAMES[code]:6s}" if mode == "follow_main" else " " * 8
            if not o:
                print(f"{head}{label:14s}  无交易"); continue
            print(f"{head}{label:14s}{o['n']:>6d}{o['cum']:>+10.1f}{o['dd']:>+8.1f}"
                  f"{o['sh']:>7.2f}{o['win']:>7.1f}{o['avg']:>+7.2f}{o['t']:>+7.2f}")
        print()


if __name__ == "__main__":
    main()
