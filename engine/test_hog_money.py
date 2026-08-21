"""引擎的回归测试。

**为什么现在才有**(2026-08-20 审计):`hog_money.py` + `smart_money.py` 共 2594 行,
决定进场/出场信号,此前的安全网只有 `replay` 末尾那一句断言,CI 一行都不跑。
对比之下 Rust 有 147 个测试、前端 113 个、collector 有 pytest+ruff。**唯独直接
产出交易信号的那部分裸奔。**

这里不追覆盖率,只钉住那些**错了不会报错、只会安静给出一个看着合理的数**的地方——
每一条都对应一次真实踩过的坑,坑的编号写在各自的 docstring 里:

  · 交割纪律查的是**持仓合约**不是当日主力(DEC-096,免费续命)
  · 成交在**次日开盘**不是信号日结算(DEC-090,+5247% → +436%)
  · 做空必须**逐日连乘**不能用简单收益(2026-08-20,+79.6% vs +88.3%)
  · 出场没成交价时**不许下一笔提前开进去**(持仓日重叠)
  · 逐笔记账与逐日净值**必须相等**(玻璃 +442% vs +2856%)

夹具全部手工构造:日期少、价格是整数、期望值能用纸笔算出来。用真实数据当夹具
等于把今天的历史冻进测试,数据一变测试就红,而红的原因与代码无关。
"""
from __future__ import annotations

import pathlib

import numpy as np
import pandas as pd
import pytest

import hog_money as H


# ---- 夹具 ---------------------------------------------------------------


def bdays(start: str, n: int) -> pd.DatetimeIndex:
    """n 个连续工作日(不查节假日,与引擎口径一致)。"""
    return pd.bdate_range(start, periods=n)


def frames(idx, contract: str, opens, settles=None, main=None, past=0.0):
    """把一列价格铺成 replay 要的 (mkt, op, st)。

    opens/settles 长度必须与 idx 齐;`np.nan` 表示那天没成交价。
    """
    op = pd.DataFrame({contract: list(opens)}, index=idx)
    st = pd.DataFrame({contract: list(settles if settles is not None else opens)}, index=idx)
    mkt = pd.DataFrame(
        {"main": [main or contract] * len(idx), "past": [past] * len(idx)}, index=idx
    )
    return mkt, op, st


# 「无信号」的填充值。**不能用 0.0**:`exit_z` 默认就是 0.0,而判据是
# `abs(z) <= exit_z and side*z <= 0`——z 恰好为 0 会当场触发「消退出场」。
# 生产里 z 是连续量,永远不会正好是 0,所以这条规则实测三年一次没触发过;
# 但手工夹具很容易正好写成 0,于是测试在一个自己都没意识到的出场上通过。
QUIET = 0.5


def signals(idx, z):
    """一列 z 值当信号。标量表示整段都是同一个值。"""
    if np.isscalar(z):
        z = [z] * len(idx)
    return pd.DataFrame({"z": list(z)}, index=idx)


@pytest.fixture(autouse=True)
def _plain_rules():
    """每个用例都从**生猪的规则**起步,并关掉共振,免得受品种选择影响。

    `RULES` 是模块级全局,`use()` 会就地改它——2026-08-19 的审计脚本就是忘了
    切回来,拿纯碱的规则跑了生猪,18 笔变 36 笔。测试之间必须复位。
    """
    saved = dict(H.RULES)
    H.use("LH")
    H.RULES["signal_source"] = "plain"
    yield
    H.RULES.clear()
    H.RULES.update(saved)


# ---- 交割窗口 -----------------------------------------------------------


class TestWindowEnd:
    """散户窗口止点 = 交割月**前月**最后一个非周末日。

    这是交割纪律的地基:运营者的原话是「我是散户,玻璃 2609 合约 8.31 日之前
    需要离场」,再由 `exit_before_delivery` 往前推 10 个交易日。
    """

    def test_玻璃2609的止点是八月最后一天(self):
        # 运营者亲口举的例子,直接钉死。2026-08-31 是周一。
        assert H.window_end("FG2609") == pd.Timestamp("2026-08-31")

    def test_止点落在周末时往前退(self):
        # 2026-02 的最后一天是 28 号周六 → 退到 27 号周五。
        assert H.window_end("SA2603") == pd.Timestamp("2026-02-27")
        # 2026-05-31 是周日 → 退到 29 号周五。
        assert H.window_end("LH2606") == pd.Timestamp("2026-05-29")

    def test_一月合约要跨到上一年十二月(self):
        assert H.window_end("JM2601") == pd.Timestamp("2025-12-31")

    def test_合约代码里的字母不影响解析(self):
        assert H.window_end("lh2611") == H.window_end("LH2611")

    def test_剩余天数从次日起算(self):
        """信号是盘后出的:今天判「剩 N 天」,平仓动作发生在明天。"""
        end = H.window_end("FG2609")            # 2026-08-31 周一
        assert H.days_to_window_end("FG2609", end) == 0          # 当天不算
        assert H.days_to_window_end("FG2609", end - pd.Timedelta(days=1)) == 1
        # 8-24 周一 → 25/26/27/28/31 共 5 个工作日
        assert H.days_to_window_end("FG2609", pd.Timestamp("2026-08-24")) == 5

    def test_过了止点一律给零而不是负数(self):
        assert H.days_to_window_end("FG2609", pd.Timestamp("2026-09-10")) == 0

    def test_与套利监控同口径(self):
        """Rust 侧 `days_to_window_end` 是同一个概念的第二份实现。

        两边对「散户还能拿多久」必须给同一个答案,否则机构资金说该走了、
        套利监控还显示能拿——两个页面自相矛盾,而谁都不会报错。
        """
        # Rust 侧 deadline() 取 min(两腿),Python 侧逐合约算;单腿情形必须相同。
        for code, expect in [("LH2611", "2026-10-30"), ("JD2701", "2026-12-31"),
                             ("JM2609", "2026-08-31")]:
            assert H.window_end(code) == pd.Timestamp(expect), code


# ---- 交割纪律(DEC-096:免费续命) ----------------------------------------


class TestDeliveryDiscipline:
    def test_持仓合约到期就强制平仓(self):
        """剩余天数按**持仓所在合约**算,不是当日主力。

        DEC-096 的原始 bug:原来查当日主力,主力一换检查对象就变成远月,老仓
        免费续命。页面上出现「进场 966·FG2609 / 现价 912·FG2701」这种读不懂的
        东西。实测被免费滚过去的那些笔收益普遍高好几倍(鸡蛋 +7.38% vs −1.83%)
        ——它们拿到了一次不用付钱也不用重新判断的续命。
        """
        # 逼近 FG2609 的窗口止点(2026-08-31)。8-11 起,剩余天数一路减到 10。
        idx = bdays("2026-08-03", 20)
        mkt, op, st = frames(idx, "FG2609", np.arange(1000.0, 1000.0 + len(idx)))
        # 全程强做空信号,除非被纪律赶出去否则不会自己走。
        trades, pos, _ = H.replay(signals(idx, -3.0), mkt, op=op, st=st)

        closed = [t for t in trades if t["exit_date"]]
        assert closed, "应当有一笔被交割纪律平掉"
        assert closed[0]["exit_reason"] == "临近交割"
        # 平仓日当天剩余天数必须已经 ≤10,不能拖过线。
        left = H.days_to_window_end("FG2609", pd.Timestamp(closed[0]["exit_date"]))
        assert left <= H.RULES["exit_before_delivery"], f"平得太晚,还剩 {left} 天"

    def test_主力换月不能给老仓续命(self):
        """老仓在 FG2609 上,主力已换到 FG2701——检查的仍须是 FG2609。

        这是 DEC-096 的**反向守卫**:如果哪天有人把判据改回当日主力,这条会红。
        """
        idx = bdays("2026-08-03", 20)
        near, far = "FG2609", "FG2701"
        op = pd.DataFrame(
            {near: np.arange(1000.0, 1000.0 + len(idx)),
             far: np.arange(1200.0, 1200.0 + len(idx))}, index=idx)
        st = op.copy()
        # 前 5 天主力是近月(仓开在近月),之后主力换到远月。
        main = [near] * 5 + [far] * (len(idx) - 5)
        mkt = pd.DataFrame({"main": main, "past": [0.0] * len(idx)}, index=idx)

        trades, _, _ = H.replay(signals(idx, -3.0), mkt, op=op, st=st)
        first = trades[0]
        assert first["contract"] == near, "仓应当开在开仓当日的主力上"
        assert first["exit_date"] is not None, "换月不该让近月的仓无限续命"
        assert first["exit_reason"] == "临近交割"

    def test_窗口内不再开新仓(self):
        """交割窗口内**只挡不进**。信号再强也不开。"""
        idx = bdays("2026-08-24", 6)            # 全程剩余 ≤5 天
        mkt, op, st = frames(idx, "FG2609", np.arange(1000.0, 1000.0 + len(idx)))
        trades, pos, _ = H.replay(signals(idx, -3.0), mkt, op=op, st=st)
        assert trades == []
        assert (pos == 0).all()

    def test_换月之后可以在新合约上开(self):
        """「2701 位置好就到 2701 开仓」——挡的是近月,不是这个信号。"""
        idx = bdays("2026-08-24", 6)
        far = "FG2701"
        mkt, op, st = frames(idx, far, np.arange(1200.0, 1200.0 + len(idx)))
        trades, _, _ = H.replay(signals(idx, -3.0), mkt, op=op, st=st)
        assert trades and trades[0]["contract"] == far


# ---- 成交口径(DEC-090:次日开盘) ---------------------------------------


class TestFillTiming:
    def test_进场价是次日开盘不是信号日结算(self):
        """席位排名收盘后才公布,按信号日结算价成交做不到。

        实测这一条把玻璃从 +5247% 打到 +436% —— 收益几乎全在信号后第一天,
        而那天拿不到。这不是保守,是**那笔钱本来就不存在**。
        """
        idx = bdays("2026-03-02", 8)
        # 开盘价与结算价故意错开,好分辨引擎取了哪一个。
        opens = [100.0, 200.0, 210.0, 220.0, 230.0, 240.0, 250.0, 260.0]
        settles = [999.0] * len(idx)
        mkt, op, st = frames(idx, "LH2611", opens, settles)
        # 只有第 0 天有进场信号。
        z = [-3.0] + [QUIET] * (len(idx) - 1)
        trades, _, _ = H.replay(signals(idx, z), mkt, op=op, st=st)
        assert trades, "第 0 天的信号应当在第 1 天开盘成交"
        assert trades[0]["entry_px"] == 200.0, "取的必须是次日开盘 200,不是当日结算 999"
        assert trades[0]["entry_date"] == idx[0].strftime("%Y-%m-%d"), "记的仍是信号日"

    def test_次日没有开盘价就不进场(self):
        idx = bdays("2026-03-02", 5)
        opens = [100.0, np.nan, 210.0, 220.0, 230.0]
        mkt, op, st = frames(idx, "LH2611", opens)
        z = [-3.0] + [QUIET] * 4
        trades, _, _ = H.replay(signals(idx, z), mkt, op=op, st=st)
        assert trades == [], "次日无成交价,这一笔根本下不出去"


# ---- 记账口径 -----------------------------------------------------------


class TestBookkeeping:
    def test_做空用逐日连乘不是简单收益(self):
        """做空时 `-(p_out/p_in-1)` 与逐日连乘**不相等**。

        2026-08-20 第一版记账用简单收益、日净值用连乘,生猪 +79.6% vs +88.3%,
        被 replay 末尾的断言当场抓住。引擎其余部分(基准、夏普、回撤)全是
        逐日连乘口径,记账必须跟上,否则两套数永远差一点而没人说得清差在哪。
        """
        idx = bdays("2026-03-02", 4)
        # 次日开盘 100 → 120 → 100。做空:
        #   简单收益 = -(100/100 - 1) = 0%
        #   逐日连乘 = (1 - 0.2) × (1 + 20/120) - 1 = -6.67%
        # 两者差 6.67 个百分点,而**简单收益那个数看起来完全合理**——这正是
        # 这类错误的可怕之处:不报错,只是账少了一块。
        opens = [90.0, 100.0, 120.0, 100.0]
        mkt, op, st = frames(idx, "LH2611", opens)
        # 全程强做空信号:不触发任何出场,末了留作「持有中」按结算价估值。
        trades, _, _ = H.replay(signals(idx, -3.0), mkt, op=op, st=st)
        t = trades[0]
        simple = -(100.0 / 100.0 - 1) * 100          # = 0.0
        compound = ((1 - 0.2) * (1 + 20.0 / 120.0) - 1) * 100
        assert abs(t["ret_pct"] - simple) > 1.0, "不能是简单收益"
        assert t["ret_pct"] == pytest.approx(compound, abs=0.05)

    def test_持有中那笔的浮盈也要连乘(self):
        """未平仓那笔单独走一条估值分支,**它和闭合记账一样容易写错**。

        这条是变异测试逼出来的:把持有中的估值改成 `side*(p_now/p_in-1)`,
        原有 21 个用例**全部照过**——那条分支当时根本没人覆盖。
        闭合那笔的连乘由 `test_做空用逐日连乘` 守着(改坏它会红),这里守另一半。

        构造上要避开止损:做空 6% 就出局,所以让价格先跌后回,幅度都压在线内。
        """
        idx = bdays("2026-03-02", 4)
        # 次日开盘 100 → 96 → 100。做空:
        #   简单收益 = -(100/100 - 1) = 0%
        #   逐日连乘 = (1 + 4/100) × (1 - 4/96) - 1 = -0.333%
        opens = [110.0, 100.0, 96.0, 100.0]
        mkt, op, st = frames(idx, "LH2611", opens)
        trades, _, _ = H.replay(signals(idx, -3.0), mkt, op=op, st=st)

        held = [t for t in trades if t["exit_date"] is None]
        assert held, "全程强信号且没撞止损,末了应当留一笔持有中"
        t = held[0]
        assert t["exit_reason"] is None and t["exit_px"] is None
        expect = ((1 + 4 / 100) * (1 - 4 / 96) - 1) * 100
        assert t["ret_pct"] == pytest.approx(expect, abs=0.02), "持有中的浮盈也必须是连乘"
        assert abs(t["ret_pct"] - 0.0) > 0.1, "简单收益会给 0%,那是错的"

    def test_逐笔与逐日必须相等(self):
        """`replay` 末尾那句断言的正面用例。

        断言本身钉的是「两套账对得上」;这里再从外面确认一次,免得哪天有人
        为了让断言过而把它放宽。
        """
        idx = bdays("2026-03-02", 12)
        rng = np.random.default_rng(20260820)
        opens = 1000 + np.cumsum(rng.normal(0, 8, len(idx)))
        mkt, op, st = frames(idx, "LH2611", opens)
        z = [-3.0, QUIET, QUIET, QUIET, 3.0, QUIET, QUIET, -3.0, QUIET, QUIET, QUIET, QUIET]
        trades, _, daily = H.replay(signals(idx, z), mkt, op=op, st=st)
        closed = [t for t in trades if t["exit_date"]]
        assert closed, "这组信号应当至少平掉一笔"
        by_trade = float(np.prod([1 + t["ret_pct"] / 100 for t in closed]))
        # daily 里含手续费,by_trade 不含;放宽到费用量级即可,重点是不能差一个数量级。
        by_day = float((1 + daily).prod())
        assert abs(by_trade - by_day) < 0.05, f"两套账差太多:{by_trade} vs {by_day}"

    def test_空档日不会把净值链条弄断(self):
        """中间几天没有成交价时,记账跨过空档,逐日也必须跨过。

        2026-08-20 研究脚本的第一版就是这么错的:玻璃逐笔 +442% 而逐日 +2856%。
        """
        idx = bdays("2026-03-02", 8)
        opens = [100.0, 100.0, np.nan, np.nan, 110.0, 110.0, 110.0, 110.0]
        mkt, op, st = frames(idx, "LH2611", opens)
        z = [3.0, 0, 0, 0, 0, 0, -3.0, 0]
        H.RULES["long_enabled"] = True
        H.RULES["long_needs_dip"] = False
        trades, _, daily = H.replay(signals(idx, z), mkt, op=op, st=st)
        assert trades, "做多开关已开,应当有仓"
        assert np.isfinite(daily).all(), "空档不该在日净值里留下 nan"
        # 100 → 110 做多 = +10%,空档只是没有报价,不是价格归零。
        assert trades[0]["ret_pct"] == pytest.approx(10.0, abs=0.1)


# ---- 持仓不重叠 ---------------------------------------------------------


class TestNoOverlap:
    def test_老仓没真正平掉之前不许开新仓(self):
        """出场只能在次日开盘成交;那天没价就挂着等。

        2026-08-20 第一版一边说「平了」一边让下一笔在老仓真正平掉之前就开进去,
        两笔的持仓日重叠——净值凭空多算一段。断言当场抓住。
        """
        idx = bdays("2026-03-02", 10)
        # 第 4 天起连续两天没有开盘价:平仓指令发出后要等到第 6 天才成交。
        opens = [100.0, 100.0, 102.0, 104.0, np.nan, np.nan, 106.0, 108.0, 110.0, 112.0]
        mkt, op, st = frames(idx, "LH2611", opens)
        z = [-3.0, QUIET, QUIET, 3.0, 3.0, 3.0, -3.0, -3.0, QUIET, QUIET]
        trades, pos, _ = H.replay(signals(idx, z), mkt, op=op, st=st)

        # 任意两笔的持仓区间不得相交。
        spans = [(t["entry_date"], t["exit_date"]) for t in trades]
        for (a_in, a_out), (b_in, b_out) in zip(spans, spans[1:]):
            assert a_out is not None, "前一笔必须先平掉"
            assert a_out <= b_in, f"持仓重叠:{a_in}~{a_out} 与 {b_in}~{b_out}"
        assert set(pos.unique()) <= {-1.0, 0.0, 1.0}, "同一时刻只能有一手方向"


# ---- 规则本身 -----------------------------------------------------------


class TestResearchHooks:
    """`replay` 的两个研究参数 —— **它们存在的唯一前提是不影响生产路径**。"""

    def test_两个研究参数默认关闭时与不传完全一致(self):
        """加参数最怕的就是「顺手改了默认行为」。这条逐笔比对。

        加在 replay 里而不是另写一份研究版:今天已经栽过好几次「同一件事两处
        实现,一处过期」。代价就是必须有这条测试守着。
        """
        idx = bdays("2026-03-02", 12)
        rng = np.random.default_rng(20260821)
        opens = 1000 + np.cumsum(rng.normal(0, 8, len(idx)))
        mkt, op, st = frames(idx, "LH2611", opens)
        z = [-3.0, QUIET, QUIET, 3.0, QUIET, -3.0, QUIET, QUIET, 3.0, QUIET, QUIET, QUIET]
        base = H.replay(signals(idx, z), mkt, op=op, st=st)[0]
        same = H.replay(signals(idx, z), mkt, op=op, st=st,
                        extra_exit=None, disable_reverse=False)[0]
        assert base == same

    def test_外部出场能把仓位提前赶走(self):
        idx = bdays("2026-03-02", 10)
        mkt, op, st = frames(idx, "LH2611", [100.0] * 10)
        # 全程强做空信号:不加干预会一直持有到末尾。
        held = H.replay(signals(idx, -3.0), mkt, op=op, st=st)[0]
        assert held[0]["exit_date"] is None, "基准应当是持有中"

        kick = pd.Series(False, index=idx)
        kick.iloc[4] = True
        out = H.replay(signals(idx, -3.0), mkt, op=op, st=st, extra_exit=kick)[0]
        assert out[0]["exit_reason"] == "外部"
        assert out[0]["exit_date"] == idx[4].strftime("%Y-%m-%d")

    def test_同一天两个条件都成立时止损优先于外部(self):
        """交割纪律与止损是「不能再拿了」,不是择时判断,外部信号不容替换。

        **优先级只在同一天多个条件同时成立时才有意义** —— 第一版把 extra_exit
        全设成真,结果第一个持仓日就走「外部」,根本没轮到止损,那测的不是优先级。
        这里让止损与外部**落在同一天**。
        """
        idx = bdays("2026-03-02", 6)
        # 次日开盘 100 建空;第 2 天涨到 110 → 浮亏 10%,越过 6% 止损线。
        mkt, op, st = frames(idx, "LH2611", [90.0, 100.0, 110.0, 110.0, 110.0, 110.0])
        kick = pd.Series(False, index=idx)
        kick.iloc[2] = True                     # 与止损同一天
        trades = H.replay(signals(idx, -3.0), mkt, op=op, st=st, extra_exit=kick)[0]
        closed = [t for t in trades if t["exit_date"]]
        assert closed, "应当有平仓"
        assert closed[0]["exit_reason"] == "止损", closed[0]["exit_reason"]
        assert closed[0]["exit_date"] == idx[2].strftime("%Y-%m-%d")

    def test_关掉反向之后那条理由不再出现(self):
        idx = bdays("2026-03-02", 8)
        mkt, op, st = frames(idx, "LH2611", [100.0] * 8)
        z = [-3.0, QUIET, 3.0, 3.0, QUIET, QUIET, QUIET, QUIET]
        on = H.replay(signals(idx, z), mkt, op=op, st=st)[0]
        assert any(t["exit_reason"] == "反向" for t in on), "基准里应当有反向出场"
        off = H.replay(signals(idx, z), mkt, op=op, st=st, disable_reverse=True)[0]
        assert not any(t["exit_reason"] == "反向" for t in off)


class TestUnloadState:
    """机构卸了多少 —— **只作展示的维度**,三处重置错了都不会报错。

    它不进任何进出场判据(`replay` 一个字都不读它),所以错了不会体现为一笔坏交易,
    只会在页面上显示一个看着合理的百分比。这类东西必须靠测试钉住。
    """

    @staticmethod
    def _frames(dates, nets, members=("A", "B"), group=None):
        """把一串合计净持仓铺成 unload_state 要的三个入参。

        席位表按 members 平摊持仓 —— `unload_state` 只用它数「在榜几家」,
        具体怎么摊不影响结果。
        """
        sig = pd.DataFrame({"net": list(nets)}, index=dates)
        rows = []
        for d, n in zip(dates, nets):
            if not np.isfinite(n):
                continue                       # 掉榜:那天一行都没有
            for m in members:
                rows.append({"trade_date": d, "member_key": m,
                             "net": float(n) / len(members)})
        seat = pd.DataFrame(rows, columns=["trade_date", "member_key", "net"])
        grp = group if group is not None else [tuple(members)] * len(dates)
        return sig, seat, pd.Series(grp, index=dates)

    def test_从峰值卸掉一半就报一半(self):
        idx = bdays("2026-03-02", 4)
        sig, seat, groups = self._frames(idx, [-100.0, -200.0, -150.0, -100.0])
        out = H.unload_state(sig, seat, groups)
        assert out["pct"] == pytest.approx(0.5)
        assert out["peak_net"] == -200
        assert out["peak_date"] == idx[1].strftime("%Y-%m-%d")

    def test_换组当天必须重来一轮(self):
        """新旧两组持仓水平不同,不重置会把「换了一批人」读成「机构大幅出货」。

        `signal_series` 算 chg 时为同一个理由分组算过一遍 —— 这里不能漏。
        """
        idx = bdays("2026-03-02", 4)
        # 前两天是 A/B 组、持仓 −1000;后两天换成 C/D 组、持仓只有 −100。
        sig = pd.DataFrame({"net": [-1000.0, -1000.0, -100.0, -100.0]}, index=idx)
        rows = []
        for d, n, ms in zip(idx, [-1000, -1000, -100, -100],
                            [("A", "B"), ("A", "B"), ("C", "D"), ("C", "D")]):
            for m in ms:
                rows.append({"trade_date": d, "member_key": m, "net": n / 2})
        seat = pd.DataFrame(rows)
        groups = pd.Series([("A", "B"), ("A", "B"), ("C", "D"), ("C", "D")], index=idx)

        out = H.unload_state(sig, seat, groups)
        assert out["pct"] == 0.0, "换组之后要从新组自己的峰值起算,不是报「卸了 90%」"
        assert out["peak_net"] == -100

    def test_方向翻转重开一轮(self):
        idx = bdays("2026-03-02", 4)
        sig, seat, groups = self._frames(idx, [-200.0, -100.0, 50.0, 40.0])
        out = H.unload_state(sig, seat, groups)
        # 翻多之后峰值是 +50,今天 40 → 卸掉 20%。与之前那轮空头的 200 无关。
        assert out["peak_net"] == 50
        assert out["pct"] == pytest.approx(0.2)

    def test_掉榜那天冻结而不是重置(self):
        """掉榜是「不知道」不是「卸完了」(research/PITFALLS 第 4 条)。

        研究脚本第一版在这里把整轮重置掉,于是掉榜一天,峰值就从下一个观测值
        重新起算,出货程度掉回 0 —— 哪怕他实际已卸掉八成。
        """
        idx = bdays("2026-03-02", 4)
        sig, seat, groups = self._frames(idx, [-200.0, np.nan, -60.0, -50.0])
        out = H.unload_state(sig, seat, groups)
        assert out["peak_net"] == -200, "掉榜不能让峰值从 −60 重新起算"
        assert out["pct"] == pytest.approx(0.75)

    def test_带出在榜家数好让页面说清掉榜混淆(self):
        """五家掉两家会让合计净持仓下降,而人家可能一手没动。

        实测这个混淆专门吃掉长窗口(纯碱 20 日的表观效应几乎全由它贡献)。
        分不清的时候页面必须说出来,所以两个家数都要带出去。
        """
        idx = bdays("2026-03-02", 3)
        sig = pd.DataFrame({"net": [-300.0, -300.0, -100.0]}, index=idx)
        rows = []
        for d, ms in zip(idx, [("A", "B", "C"), ("A", "B", "C"), ("A",)]):
            for m in ms:
                rows.append({"trade_date": d, "member_key": m, "net": -100.0})
        seat = pd.DataFrame(rows)
        groups = pd.Series([("A", "B", "C")] * 3, index=idx)

        out = H.unload_state(sig, seat, groups)
        assert out["pct"] == pytest.approx(2 / 3, abs=0.001)   # payload 里已 round 到千分位
        assert out["legs_at_peak"] == 3
        assert out["legs_now"] == 1, "页面据此提示:这个降幅分不清是出货还是掉榜"

    def test_它不进任何进出场判据(self):
        """**这条钉的是边界。**

        这个量作为进场判据只在纯碱 5 日窗口上通过检验,横截面上没有支持
        (玻璃样本外翻号、焦煤否)。现在它只该出现在 payload 里给人看;
        哪天有人把它接进 replay,这条会红,逼他先去看
        `REPORT_SA_UNLOAD_DEEP_v1.md` 再决定。
        """
        src = pathlib.Path(H.__file__).read_text(encoding="utf-8")
        body = src[src.index("def replay("):src.index("def _f(")]
        for word in ("unload", "peak_net", "legs_at_peak"):
            assert word not in body, f"replay 里出现了 {word} —— 它只该用于显示"


class TestRules:
    def test_use切换品种会就地改全局规则(self):
        """`RULES` 是模块级全局,`use()` 就地改它。

        这不是缺陷但**极容易踩**:2026-08-19 的审计脚本忘了在每次回放前切品种,
        拿纯碱的规则跑了生猪,18 笔变 36 笔而没有任何报错。把这个行为钉在测试里,
        免得下一个人以为 `use()` 返回的是独立副本。
        """
        H.use("LH")
        lh = dict(H.RULES)
        H.use("SA")
        assert H.RULES is not lh
        assert dict(H.RULES) != lh, "不同品种的规则应当有差别"
        H.use("LH")
        assert dict(H.RULES) == lh, "切回来必须完全还原"

    def test_五个品种都配了交割纪律(self):
        """运营者拍板:贵金属不加,生猪/玻璃/纯碱/鸡蛋/焦煤都要。"""
        for code in ("LH", "FG", "SA", "JD", "JM"):
            H.use(code)
            assert H.RULES["exit_before_delivery"] == 10, code

    def test_exit_z为零时z恰好为零会真的触发消退出场(self):
        """`exit_z = 0.0` 不是「关掉」,是「要求 z 恰好为 0」。

        RULES 里写着这条实测三年 36 笔一次没触发过——**那是因为真实 z 是连续量,
        不会正好落在 0 上**,不是因为它被禁用了。写夹具时很容易把「无信号」填成
        0.0,于是测试在一个自己都没意识到的出场上通过(本测试文件第一版就是)。
        钉在这里,免得下次又有人把 0.0 当成中性值。
        """
        # **价格必须走平**:只留「消退」这一个变量。第一版用了递增价,做空
        # 立刻浮亏 10% 撞上 6% 止损线,于是两条断言测的都不是自己以为的东西。
        idx = bdays("2026-03-02", 5)
        mkt, op, st = frames(idx, "LH2611", [100.0] * 5)
        assert H.RULES["exit_z"] == 0.0

        z_zero = H.replay(signals(idx, [-3.0, 0.0, 0.0, 0.0, 0.0]), mkt, op=op, st=st)[0]
        assert z_zero[0]["exit_reason"] == "消退", "z=0 会当场触发消退"

        z_quiet = H.replay(signals(idx, [-3.0] + [QUIET] * 4), mkt, op=op, st=st)[0]
        assert z_quiet[0]["exit_date"] is None, f"z={QUIET} 不该触发任何出场"

    def test_鸡蛋做多开关是开的且要求回撤(self):
        """DEC-096 补记,运营者拍板。**这是样本内的选择**,写死在测试里是为了
        以后有人无意改掉时能看见,不是说这个选择被样本外验证过。"""
        H.use("JD")
        assert H.RULES["long_enabled"] is True
        assert H.RULES["long_needs_dip"] is True
