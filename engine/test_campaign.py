"""campaign(逐合约战役策略,DEC-133)的回归测试。

只钉那些**错了不会报错、只会安静给出一个看着合理的数**的地方:

  · 进场只认可见口径 net_off —— 反推行(net 有、net_off 无)不许造出加仓日(DEC-108)
  · 价格高于批次成本不进;<= 才进,成交在**次日开盘**
  · 聪明钱份额资格:历史战役亏钱的那一侧,区间再漂亮也不进
  · 卸仓 30%(自进场峰值)次日开盘出场;峰值随后续加仓上移
  · 交割纪律:窗口止点前 10 交易日强平,且窗口边缘不开新仓
  · 一个区间只许进一笔:出场后同一区间不回头;新区间照进
  · 多仓并行:两个合约同时持仓,各是各的流

夹具手工构造,期望值能用纸笔算出来。**注意资格判据的现实**:人格盈亏含当前
战役自己建仓期的浮亏(左侧买入天然先亏),所以做多的夹具要先给一段足量的
盈利史(`HIST`),否则几天浮亏就把人格判负 —— 真实数据三年体量大,不存在此虑。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import campaign as C

HIST = pd.bdate_range("2025-06-01", periods=6)
HIST_PX = [900, 920, 940, 960, 980, 1000]   # 甲持 +5000 一路涨:多头人格 +5,000,000


def bdays(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def rules() -> dict:
    return {
        "campaign": {"add_min": 100.0, "confirm": 500.0, "gap": 3, "tail": 10,
                     "unload": 0.30, "share": 0.25, "min_days": 5},
        "exit_before_delivery": 10,
        "multiplier": 10.0,
        "turn_cost": 0.0005,
    }


def seat_rows(member: str, contract: str, dates, nets, off=None):
    off = list(nets) if off is None else off
    return pd.DataFrame({
        "member_key": member, "contract": contract, "trade_date": dates,
        "net": list(nets), "net_off": off,
    })


def with_hist(idx, contract, settles, seat_now):
    """铺上盈利史:甲在 LH8801 上 +5000 一路涨。返回 (seat, mkt, op, st)。"""
    seat = pd.concat([seat_rows("甲", "LH8801", HIST, [5000] * 6), seat_now])
    st = pd.concat([
        pd.DataFrame({contract: [np.nan] * 6, "LH8801": HIST_PX}, index=HIST),
        pd.DataFrame({contract: list(settles), "LH8801": [np.nan] * len(idx)}, index=idx),
    ])
    op = st.copy()
    mkt = pd.DataFrame({"main": [contract] * len(idx)}, index=idx)
    return seat, mkt, op, st


FAR = "LH9911"   # 2099-11 交割:测试日期都在 2026,dleft 恒大


def test_反推行不可见_不许造出加仓日():
    idx = bdays("2026-01-05", 12)
    nets = [0, 0, 300, 600, 900, 1200, 1200, 1200, 1200, 1200, 1200, 1200]
    seat_now = seat_rows("甲", FAR, idx, nets, off=[np.nan] * 12)
    seat, mkt, op, st = with_hist(idx, FAR, list(range(1200, 1200 - 12 * 10, -10)), seat_now)
    out = C.run(seat, mkt, op, st, ["甲"], rules())
    assert out["trades"] == []


def test_价格高于批次成本不进_回到成本次日开盘进():
    idx = bdays("2026-01-05", 14)
    # 确认线在第6天(600手)才到,而那天价 976 已高于批次成本 975.33 ——
    # 当天不进、区间也不烧;第8天 970 回到成本下才触发,次日开盘成交。
    settles = [1000, 990, 980, 970, 970, 976, 977, 970, 968, 966, 966, 966, 966, 966]
    nets = [0, 0, 200, 400, 400, 600, 600, 600, 600, 600, 600, 600, 600, 600]
    seat, mkt, op, st = with_hist(idx, FAR, settles, seat_rows("甲", FAR, idx, nets))
    out = C.run(seat, mkt, op, st, ["甲"], rules())
    trades = out["trades"]
    assert len(trades) == 1
    t = trades[0]
    # 批次成本 = (200x980+200x970+200x976)/600 = 975.33
    assert t["batch_cost"] == 975
    assert t["entry_date"] == idx[7].strftime("%Y-%m-%d")
    assert t["entry_px"] == 968
    assert t["side"] == "long"


def test_份额资格_历史亏钱那侧不进():
    hist = bdays("2025-06-02", 6)
    idx = bdays("2026-01-05", 12)
    # 历史合约上:甲做多一路亏,乙做空一路赚 —— 多头人格为负,空头为正。
    hist_seat = pd.concat([
        seat_rows("甲", "LH8801", hist, [500] * 6),
        seat_rows("乙", "LH8801", hist, [-500] * 6),
    ])
    settles = [1000, 990, 980, 970, 960, 950, 940, 940, 940, 940, 940, 940]
    nets = [0, 0, 200, 400, 600, 800, 800, 800, 800, 800, 800, 800]
    seat = pd.concat([hist_seat, seat_rows("甲", FAR, idx, nets)])
    st = pd.concat([
        pd.DataFrame({FAR: [np.nan] * 6, "LH8801": [1000, 980, 960, 940, 920, 900]}, index=hist),
        pd.DataFrame({FAR: settles, "LH8801": [np.nan] * 12}, index=idx),
    ])
    op = st.copy()
    mkt = pd.DataFrame({"main": [FAR] * 12}, index=idx)
    out = C.run(seat, mkt, op, st, ["甲", "乙"], rules())
    assert all(t["side"] != "long" for t in out["trades"])
    watch_long = [w for w in out["watch"] if w["side"] == "long"]
    assert watch_long and watch_long[0]["qualified"] is False


def test_卸仓30出场_峰值随加仓上移():
    idx = bdays("2026-01-05", 16)
    settles = [1000, 990, 980, 970, 960, 950, 960, 970, 980, 990, 1000, 1010, 1020, 1030, 1040, 1050]
    # 进场后继续加到 1000(峰值),然后砍到 650(< 700 = 1000x0.7)。
    nets = [0, 0, 200, 400, 600, 800, 800, 1000, 1000, 1000, 650, 650, 650, 650, 650, 650]
    seat, mkt, op, st = with_hist(idx, FAR, settles, seat_rows("甲", FAR, idx, nets))
    out = C.run(seat, mkt, op, st, ["甲"], rules())
    closed = [t for t in out["trades"] if t["exit_date"] is not None]
    assert len(closed) == 1
    t = closed[0]
    # 进场:确认线 600 手在第5天已到、960<=成本970 -> 第5天触发、第6天开盘 950 成交。
    # 峰值 1000(第8天加上去的),第11天 650 < 700 触发,次日开盘 1010 出场。
    assert t["exit_reason"].startswith("机构卸仓")
    assert t["exit_date"] == idx[10].strftime("%Y-%m-%d")
    assert t["entry_date"] == idx[4].strftime("%Y-%m-%d")
    assert t["entry_px"] == 950
    assert t["exit_px"] == 1010
    assert abs(t["ret_pct"] - (1010 / 950 - 1) * 100) < 0.01


def test_交割纪律_强平且窗口内不开仓():
    # 2026-03 交割:窗口止点 = 2026-02-27(周五)。02-05 信号日 dleft=16 刚好能进,
    # 02-13 dleft=10 强平,之后窗口内不许再开。
    c = "LH2603"
    idx = bdays("2026-02-02", 19)   # 02-02 ~ 02-26
    settles = [1000, 990, 980, 970] + [960] * 15
    nets = [0, 0, 300, 600, 600, 600] + [600] * 13
    seat, mkt, op, st = with_hist(idx, c, settles, seat_rows("甲", c, idx, nets))
    out = C.run(seat, mkt, op, st, ["甲"], rules())
    closed = [t for t in out["trades"] if t["exit_date"] is not None]
    assert len(out["trades"]) == 1 and len(closed) == 1
    assert closed[0]["exit_reason"] == "临近交割"
    assert closed[0]["entry_date"] == idx[3].strftime("%Y-%m-%d")   # 02-05
    assert closed[0]["exit_date"] == idx[9].strftime("%Y-%m-%d")    # 02-13(dleft=10)


def test_一个区间只进一笔_新区间照进():
    idx = bdays("2026-01-05", 30)
    settles = ([1000, 990, 980, 970, 960, 950, 940, 930, 920, 910]
               + [900] * 6
               + [890, 880, 870, 860, 850, 840, 830, 820, 810, 800]
               + [800] * 4)
    nets = ([0, 0, 200, 400, 600, 800, 800, 800, 800, 800]
            + [500, 500, 500, 500, 500, 500]                          # 砍到 500(<560)-> 出场
            + [500, 500, 500, 500, 500, 1200, 1400, 1600, 1600, 1600]  # 新区间:第22天起
            + [1600] * 4)
    seat, mkt, op, st = with_hist(idx, FAR, settles, seat_rows("甲", FAR, idx, nets))
    out = C.run(seat, mkt, op, st, ["甲"], rules())
    assert len(out["trades"]) == 2
    assert out["trades"][1]["entry_date"] >= idx[21].strftime("%Y-%m-%d")


def test_多仓并行_两个合约各是各的流():
    idx = bdays("2026-01-05", 14)
    settles = [1000, 990, 980, 970, 960, 950, 940, 930, 930, 930, 930, 930, 930, 930]
    nets = [0, 0, 200, 400, 600, 800, 800, 800, 800, 800, 800, 800, 800, 800]
    c2 = "LH9909"
    seat_now = pd.concat([seat_rows("甲", FAR, idx, nets), seat_rows("甲", c2, idx, nets)])
    seat = pd.concat([seat_rows("甲", "LH8801", HIST, [5000] * 6), seat_now])
    st = pd.concat([
        pd.DataFrame({FAR: [np.nan] * 6, c2: [np.nan] * 6, "LH8801": HIST_PX}, index=HIST),
        pd.DataFrame({FAR: settles, c2: settles, "LH8801": [np.nan] * 14}, index=idx),
    ])
    op = st.copy()
    mkt = pd.DataFrame({"main": [FAR] * 7 + [c2] * 7}, index=idx)
    out = C.run(seat, mkt, op, st, ["甲"], rules())
    open_trades = [t for t in out["trades"] if t["exit_date"] is None]
    assert {t["contract"] for t in open_trades} == {FAR, c2}
    assert int(out["pos_count"].max()) == 2


def test_contracts_panel_到期滑出_近月排序_未上榜(monkeypatch=None):
    """DEC-134:一排合约小窗 —— 到期的不出现,近月在前,恒最多 5 个;
    某家当日无行 = 未上榜,不是 0 手。"""
    import hog_money as H
    d = pd.Timestamp("2026-08-24")
    prev = pd.Timestamp("2026-08-17")
    rows = []
    # 六个活跃合约 + 一个已到期的(2607 止点在 2026-06,应被剔除)
    for c in ("LH2607", "LH2609", "LH2611", "LH2701", "LH2703", "LH2705", "LH2707"):
        rows.append({"member_key": "甲", "contract": c, "trade_date": d,
                     "net": -100.0, "net_off": -100.0})
        rows.append({"member_key": "甲", "contract": c, "trade_date": prev,
                     "net": -60.0, "net_off": -60.0})
    # 乙只在 2611 上有行,且只有今天
    rows.append({"member_key": "乙", "contract": "LH2611", "trade_date": d,
                 "net": 50.0, "net_off": 50.0})
    seat = pd.DataFrame(rows)
    panel = H.contracts_panel(seat, ["甲", "乙"], d, prev)
    names = [p["contract"] for p in panel]
    assert names == ["LH2609", "LH2611", "LH2701", "LH2703", "LH2705"]  # 2607 到期剔除,2707 超出 5 个
    p2611 = next(p for p in panel if p["contract"] == "LH2611")
    jia = next(m for m in p2611["members"] if m["member"] == "甲")
    yi = next(m for m in p2611["members"] if m["member"] == "乙")
    assert jia["net"] == -100 and jia["change"] == -40 and jia["on_board"]
    assert yi["net"] == 50 and yi["on_board"]
    p2701 = next(p for p in panel if p["contract"] == "LH2701")
    yi2 = next(m for m in p2701["members"] if m["member"] == "乙")
    assert yi2["on_board"] is False
