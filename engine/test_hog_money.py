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
    # 生猪 2026-08-23 起(DEC-118)做多腿走 unload_bounce,需要 sig 上有 bounce_long 列;
    # 通用夹具是合成的,没有这列。测试基线取「生猪的基础规则 + 默认做多来源」,
    # 品种专属的 long_mode / exit_mode 由各自的测试显式打开(TestBounceLong/TestInstExit/TestRules)。
    H.RULES["long_mode"] = "flow"
    H.RULES["exit_mode"] = "retail"
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


class TestEntrySide:
    """进场方向 —— 运营者 2026-08-21:「触发信号要显示做多或者做空,一触发就显示」。

    **判据只有一份**(`entry_side`),`replay` 与 `build_payload` 共用。
    前端不许自己推 —— DEC-104 就是前端自己推进场判据推错的:页面写着
    「需达 1(现 2.09)」却又显示无持仓,因为它显示的是机构那个数而引擎比的是散户那个。
    """

    def test_做空做多按门槛两侧分(self):
        H.RULES.update(long_enabled=True, long_needs_dip=False, enter=1.0)
        assert H.entry_side(-1.5, 0.0) == (-1, None)
        assert H.entry_side(+1.5, 0.0) == (1, None)
        assert H.entry_side(+1.0, 0.0)[0] == 1, "恰好到门槛算达标"
        assert H.entry_side(-1.0, 0.0)[0] == -1

    def test_没到门槛要说没到而不是含糊(self):
        H.RULES.update(long_enabled=True, long_needs_dip=False)
        side, why = H.entry_side(0.92, 0.0)      # 玻璃 2026-08-20 的真实取值
        assert side == 0 and why == "强度未到门槛"

    def test_做多关着时上穿门槛也不进而且要说清(self):
        """关着做多时 z 上穿只代表「机构在减空」,界面不说清会让人以为信号漏了。"""
        H.RULES.update(long_enabled=False)
        side, why = H.entry_side(+3.0, 0.0)
        assert side == 0 and why == "本品种做多已关"
        assert H.entry_side(-3.0, 0.0)[0] == -1, "做空那一侧不受影响"

    def test_要求回撤时没回撤就不进(self):
        H.RULES.update(long_enabled=True, long_needs_dip=True)
        assert H.entry_side(+3.0, +0.05)[0] == 0
        assert "回撤" in H.entry_side(+3.0, +0.05)[1]
        assert H.entry_side(+3.0, -0.05) == (1, None)
        assert H.entry_side(+3.0, np.nan)[0] == 0, "回撤未知不能当成有回撤"

    def test_信号没值时不猜方向(self):
        assert H.entry_side(np.nan, 0.0) == (0, "信号未就绪")

    def test_replay与entry_side用的是同一套判据(self):
        """抽函数之后 `replay` 必须走它 —— 两处各写一份是 DEC-104 的病根。"""
        src = pathlib.Path(H.__file__).read_text(encoding="utf-8")
        body = src[src.index("def replay("):src.index("def _f(")]
        assert "entry_side(" in body, "replay 没有调用共用判据"
        assert 'RULES["long_enabled"]' not in body, "replay 里不该再自己判做多开关"


class TestPastAcrossRollover:
    """past(回撤判据)不许跨合约相除。

    这是 2026-08-21 的线上事故:纯碱 8/13 主力由 SA2609 换 SA2701,结算价
    990 → 1031(+4.1% 全是合约价差)。past 当时写的是 settle.pct_change(20),
    把跳空当成真涨,于是「近 20 日没有回撤」把 8/20 那个做多信号挡在门外 ——
    而按同合约算,那 20 日其实是跌的(−2.7%)。
    """

    @staticmethod
    def _price():
        # 老合约 A 从 100 缓慢跌;新合约 B 价位高一截(140 起),B 自己也在跌。
        # 持仓量在第 25 天翻转,制造一次换月。
        rows = []
        for i, d in enumerate(pd.bdate_range("2024-01-01", periods=40)):
            oi_a, oi_b = (900, 100) if i < 25 else (100, 900)
            for c, px, oi in (("AA2409", 100.0 - i * 0.4, oi_a),
                              ("AA2501", 140.0 - i * 0.5, oi_b)):
                rows.append({"trade_date": d, "contract": c, "settlement_price": px,
                             "close_price": px, "open_price": px,
                             "open_interest": oi, "volume": 1,
                             "source": "exchange"})
        return H.clean_price(pd.DataFrame(rows))

    def test_换月后不出现假涨(self):
        mkt = H.main_series(self._price())
        assert (mkt["main"] != mkt["main"].shift(1))[1:].any(), "夹具没造出换月,测试无效"
        past = mkt["past"].dropna()
        assert len(past), "past 全是 NaN,夹具太短"
        # 两个合约都在跌,past 任何一天都不该为正。
        assert past.max() < 0.0, f"换月跳空又混进 past 了:最大 {past.max():+.4f}"

    def test_旧写法会踩这个坑(self):
        """反证:确认夹具确实能重现事故,否则上面那条测试是空的。"""
        mkt = H.main_series(self._price())
        naive = mkt["settle"].pct_change(H.RULES["dip_win"]).dropna()
        assert naive.max() > 0.0, "夹具没能重现跨合约假涨,上一条测试形同虚设"


class TestPointInTime:
    """回榜反推 = 未来数据,当天那一格不许用(2026-08-21 修)。

    `reboard_inferred` 行是**用回榜日的增减倒推**的,实测可见滞后**恒为 1 个交易日**。
    第 D 日收盘引擎算信号、第 D+1 日开盘成交,两个时点上 D 日的反推值都还不存在。
    而信号是 `net.diff(sig_win)`,一头正好踩在这一格上。

    修之前的代价(REPORT_PIT_LOOKAHEAD_v1):玻璃 +573%→+70%、夏普 0.65→0.21、
    回撤 −45.9%→−67.9%;纯碱 +295%→+65%。**那些差额全是实盘拿不到的钱。**
    这几条测试是防止它被改回去的唯一屏障 —— 改回去不会报错,只会让回测重新变好看。
    """

    @staticmethod
    def _raw(rows):
        """(trade_date, member, rank_type, quantity, source) → clean_seat 的入参。"""
        return pd.DataFrame([
            {"trade_date": d, "instrument": "LH", "contract": "LH2611",
             "is_variety_total": "f", "rank_type": rt, "member": m,
             "quantity": q, "source": src}
            for d, m, rt, q, src in rows])

    def test_官方腿要留下哪怕另一腿是反推的(self):
        """**逐腿判,不是整行判。**

        一家可能多头榜在(官方)、空头榜掉了(反推)。整行丢掉会连那条实盘看得见的
        多头腿一起扔 —— 实测玻璃 3,131 天里有 1,352 天因此算错,这是我第一版的 bug。
        """
        d = pd.Timestamp("2026-03-02")
        out = H.clean_seat(self._raw([
            (d, "甲", "long", 100, "czce_official"),
            (d, "甲", "short", 40, "reboard_inferred"),
        ]))
        r = out.iloc[0]
        assert r["net"] == 60, "事后完整口径:100 − 40"
        assert r["net_off"] == 100, "当日可见口径:只剩那条官方的多头腿"

    def test_两腿都是反推就给未知而不是零(self):
        """掉榜≠清仓(research/PITFALLS 第 4 条)。给 0 会凭空造出一次清仓。"""
        d = pd.Timestamp("2026-03-02")
        out = H.clean_seat(self._raw([
            (d, "甲", "long", 100, "reboard_inferred"),
            (d, "甲", "short", 40, "reboard_inferred"),
        ]))
        assert out.iloc[0]["net"] == 60
        assert np.isnan(out.iloc[0]["net_off"]), "当天什么都不知道,不能报 0"

    def test_当天的反推值不进信号而五天前的照用(self):
        """滞后恒为 1 天,所以 `T − sig_win` 那一格在 T 日必定已可见。

        构造:第 0 天与第 6 天各有一次掉榜(只有反推行)。
        `sig_win=5`,看第 5 天的信号 —— 它的被减数是第 0 天(该用全量,那天的反推
        在第 1 天就可见了),减数是第 5 天本身(官方,不受影响)。
        """
        H.RULES["sig_win"] = 5
        idx = bdays("2026-03-02", 7)
        rows = []
        for i, d in enumerate(idx):
            src = "reboard_inferred" if i in (0, 6) else "czce_official"
            rows.append((d, "甲", "long", 100 + i * 10, src))
        seat = H.clean_seat(self._raw(rows))
        groups = pd.Series([("甲",)] * len(idx), index=idx)
        sig = H.signal_series(seat, groups)
        # 第 5 天:官方 150;被减数是第 0 天的 100(全量,含反推)→ chg = 50
        assert sig["chg"].iloc[5] == pytest.approx(50.0)
        assert sig["net"].iloc[5] == pytest.approx(150.0)
        # 第 6 天只有反推行 → 当天不可知,信号留空而不是拿反推值凑
        assert np.isnan(sig["net"].iloc[6]), "当天只有反推行,net 必须是未知"
        assert np.isnan(sig["chg"].iloc[6])

    def test_全是官方行时新旧口径应当一模一样(self):
        """没有反推行的品种(如生猪只有 2,520 条)不该被这次改动影响。"""
        idx = bdays("2026-03-02", 8)
        rows = [(d, "甲", "long", 100 + i * 5, "czce_official") for i, d in enumerate(idx)]
        seat = H.clean_seat(self._raw(rows))
        assert (seat["net"] == seat["net_off"]).all()
        groups = pd.Series([("甲",)] * len(idx), index=idx)
        sig = H.signal_series(seat, groups)
        assert sig["net"].dropna().tolist() == [100 + i * 5 for i in range(len(idx))]


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


class TestCostSignal:
    """成本进场信号(DEC-112,鸡蛋)。钉三件事:成本重建的会计规则、
    状态条件各自的挡单原因、以及出场那一路**没有**被换掉。
    这套判据的闸门记录在 REPORT_COST_GATES_v1 —— 改这里的语义等于闸门作废。"""

    @staticmethod
    def _mk(net_vals, px_vals):
        idx = pd.bdate_range("2024-01-01", periods=len(net_vals))
        sig = pd.DataFrame({"net": net_vals}, index=idx, dtype=float)
        mkt = pd.DataFrame({"settle": px_vals}, index=idx, dtype=float)
        groups = pd.Series([("甲", "乙")] * len(net_vals), index=idx)
        return sig, mkt, groups

    def test_加仓按当日结算价加权_减仓成本不动(self):
        sig, mkt, groups = self._mk([10, 20, 20, 10, 10], [100, 110, 120, 130, 140])
        cc = H.inst_cost_series(sig, mkt, groups)
        assert cc["cost"].iloc[0] == 100          # 首日建仓 = 当日结算价
        assert abs(cc["cost"].iloc[1] - 105) < 1e-9   # (10×100+10×110)/20
        assert abs(cc["cost"].iloc[2] - 105) < 1e-9   # 没加仓,不动
        assert abs(cc["cost"].iloc[3] - 105) < 1e-9   # **减仓成本不动**
        assert cc["age"].iloc[3] == 3

    def test_方向翻转重置成本(self):
        sig, mkt, groups = self._mk([10, -5, -5], [100, 90, 80])
        cc = H.inst_cost_series(sig, mkt, groups)
        assert cc["side"].iloc[1] == -1
        assert abs(cc["cost"].iloc[1] - 90) < 1e-9    # 新一轮从翻向日起算
        assert cc["age"].iloc[1] == 0

    def test_掉榜日冻结且不发信号(self):
        sig, mkt, groups = self._mk([10, np.nan, 12], [100, 100, 100])
        cc = H.inst_cost_series(sig, mkt, groups)
        assert not np.isfinite(cc["cost"].iloc[1])    # 冻结:当天不产出
        unload = pd.Series([0.0, 0.0, 0.0], index=sig.index)
        ext = H.cost_entry_frame(cc, sig["net"], mkt["settle"], unload)
        assert ext["cost_z"].iloc[1] == 0
        assert "掉榜" in ext["cost_reason"].iloc[1]

    def test_三个挡单原因各说各的(self):
        sig, mkt, groups = self._mk([10, 10, 10], [100, 100, 106])
        cc = H.inst_cost_series(sig, mkt, groups)
        # 卸仓超三成挡住(第 1 天),价格高于成本挡住(第 2 天:价 106 > 成本 100)
        unload = pd.Series([0.0, 0.5, 0.0], index=sig.index)
        ext = H.cost_entry_frame(cc, sig["net"], mkt["settle"], unload)
        assert ext["cost_z"].iloc[0] != 0             # 三条件全满足,进
        assert ext["cost_z"].iloc[1] == 0
        assert "卸掉" in ext["cost_reason"].iloc[1]
        assert ext["cost_z"].iloc[2] == 0
        assert "高于机构成本" in ext["cost_reason"].iloc[2]

    def test_做空方向对称(self):
        sig, mkt, groups = self._mk([-10, -10], [100, 104])
        cc = H.inst_cost_series(sig, mkt, groups)
        unload = pd.Series([0.0, 0.0], index=sig.index)
        ext = H.cost_entry_frame(cc, sig["net"], mkt["settle"], unload)
        # 空头成本 100,现价 104 ≥ 成本 → 做空可进,信号为负
        assert ext["cost_z"].iloc[1] < 0

    def test_玻璃两条附加条件各自挡单(self):
        """DEC-114:最小轮龄与「还在加仓」。默认关时不得影响鸡蛋纯碱。"""
        sig, mkt, groups = self._mk([10, 10, 10, 10], [100, 100, 100, 100])
        cc = H.inst_cost_series(sig, mkt, groups)
        unload = pd.Series([0.0] * 4, index=sig.index)
        chg = pd.Series([5.0, 5.0, -3.0, 5.0], index=sig.index)   # 第 2 天机构在减
        old = (H.RULES["cost_min_age"], H.RULES["cost_need_adding"])
        try:
            H.RULES["cost_min_age"], H.RULES["cost_need_adding"] = 0, False
            base = H.cost_entry_frame(cc, sig["net"], mkt["settle"], unload, chg)
            assert (base["cost_z"] != 0).all()          # 默认关:四天都进
            H.RULES["cost_min_age"], H.RULES["cost_need_adding"] = 2, True
            ext = H.cost_entry_frame(cc, sig["net"], mkt["settle"], unload, chg)
        finally:
            H.RULES["cost_min_age"], H.RULES["cost_need_adding"] = old
        assert ext["cost_z"].iloc[0] == 0 and "刚翻向" in ext["cost_reason"].iloc[0]
        assert ext["cost_z"].iloc[1] == 0 and "刚翻向" in ext["cost_reason"].iloc[1]   # 轮龄 1 < 2
        assert ext["cost_z"].iloc[2] == 0 and "没在同向加仓" in ext["cost_reason"].iloc[2]
        assert ext["cost_z"].iloc[3] != 0                # 轮龄 3、在加仓、价=成本:进

    def test_出场那一路仍是散户反向(self):
        """cost 模式只换进场。出场换了,五道闸门全部作废。"""
        idx = pd.bdate_range("2024-01-01", periods=3)
        sig = pd.DataFrame({"z": [1.0, 1.0, 1.0],
                            "cost_z": [1.5, 0.0, -1.5]}, index=idx)
        retail = pd.DataFrame({"rz": [0.7, -0.7, 0.2]}, index=idx)
        old = H.RULES["signal_source"]
        H.RULES["signal_source"] = "cost"
        try:
            z_in, z_out = H.entry_exit_signals(sig, retail)
        finally:
            H.RULES["signal_source"] = old
        assert list(z_in) == [1.5, 0.0, -1.5]
        assert list(z_out) == [0.7, -0.7, 0.2]        # 出场 = 散户 rz,原样


class TestInstExit:
    """机构出场模式(DEC-117,焦煤)。钉三件事:触发序列的三态、
    inst 模式下散户翻向与持满确实被关掉、默认模式一个字节没变。"""

    def test_触发序列_翻向与卸仓_掉榜不触发(self):
        idx = pd.bdate_range("2024-01-01", periods=5)
        sig = pd.DataFrame({"net": [10, 10, np.nan, -5, -5]}, index=idx, dtype=float)
        mkt = pd.DataFrame({"settle": [100.0] * 5}, index=idx)
        groups = pd.Series([("甲", "乙")] * 5, index=idx)
        unload = pd.Series([0.0, 0.5, np.nan, 0.0, 0.0], index=idx)
        f = H.inst_exit_flags(sig, mkt, groups, unload)
        assert list(f) == [False, True, False, False, False]
        # 第 1 天卸仓 50% 触发;第 2 天掉榜不触发;第 3 天翻向但前一天掉榜 → 不触发
        # (昨今都可见才算翻向);第 4 天方向延续不触发

    def test_inst模式_关散户翻向与持满_理由记机构出场(self):
        """夹具:做空进场后散户 rz 立刻翻到 +2(四件套会「反向」出场),
        inst 模式必须无视它,直到 inst_exit 为真那天才以「机构出场」平仓。"""
        idx = pd.bdate_range("2024-01-01", periods=12)
        sig = pd.DataFrame({"z": [-1.5] + [0.0] * 11,
                            "inst_exit": [False] * 6 + [True] + [False] * 5}, index=idx)
        retail = pd.DataFrame({"rz": [-1.5] + [2.0] * 11}, index=idx)
        mkt = pd.DataFrame({"main": ["XX2501"] * 12, "settle": 100.0, "open": 100.0,
                            "ret": 0.0, "ret_open": 0.0, "o2c": 0.0, "past": -0.01,
                            "dleft": 200, "close": 100.0}, index=idx)
        op = pd.DataFrame({"XX2501": 100.0}, index=idx)
        st = pd.DataFrame({"XX2501": 100.0}, index=idx)
        old = dict(H.RULES)
        try:
            # 用方案 C:出场那一路是散户 rz。若用 "flow",z_out 会是 sig 的 z(恒 0),
            # 第 1 天就触发「消退」(z 恰为 0 那个老坑,见 QUIET),测不到想测的东西。
            H.RULES["signal_source"] = "resonance"
            H.RULES["long_enabled"] = False
            H.RULES["max_hold"] = 3           # 四件套下第 3 天就会「持满」
            H.RULES["exit_mode"] = "retail"
            tr_r, _, _ = H.replay(sig, mkt, retail, op, st)
            H.RULES["exit_mode"] = "inst"
            tr_i, _, _ = H.replay(sig, mkt, retail, op, st)
        finally:
            H.RULES.clear()
            H.RULES.update(old)
        assert tr_r[0]["exit_reason"] in ("反向", "持满")
        assert tr_i[0]["exit_reason"] == "机构出场"
        assert tr_i[0]["exit_date"] == idx[6].strftime("%Y-%m-%d")

    def test_inst模式缺列要报错不许静默退化(self):
        idx = pd.bdate_range("2024-01-01", periods=3)
        sig = pd.DataFrame({"z": [0.0] * 3}, index=idx)
        retail = pd.DataFrame({"rz": [0.0] * 3}, index=idx)
        mkt = pd.DataFrame({"main": ["XX2501"] * 3, "settle": 100.0, "open": 100.0,
                            "ret": 0.0, "ret_open": 0.0, "o2c": 0.0, "past": 0.0,
                            "dleft": 200, "close": 100.0}, index=idx)
        old = H.RULES["exit_mode"]
        H.RULES["exit_mode"] = "inst"
        try:
            with pytest.raises(ValueError):
                H.replay(sig, mkt, retail)
        finally:
            H.RULES["exit_mode"] = old


class TestBounceLong:
    """卸仓反弹做多(DEC-118,生猪):流量正值压掉、bounce 注入、做空优先、缺列报错。"""

    def test_压掉正值_注入反弹_做空优先(self):
        idx = pd.bdate_range("2024-01-01", periods=4)
        sig = pd.DataFrame({"z": [1.5, 0.0, -1.5, np.nan],
                            "bounce_long": [False, True, True, True]}, index=idx)
        old = dict(H.RULES)
        try:
            H.RULES["long_mode"] = "unload_bounce"
            H.RULES["enter"] = 1.0
            out = H._apply_long_mode(sig["z"], sig)
        finally:
            H.RULES.clear()
            H.RULES.update(old)
        assert out.iloc[0] == 0.0          # 流量 +1.5 被压掉:不许顺带做多
        assert out.iloc[1] == 1.5          # bounce 注入
        assert out.iloc[2] == -1.5         # 做空信号优先
        assert out.iloc[3] == 1.5          # 未就绪(NaN)的日子 bounce 照样能进

    def test_flow模式一字不动(self):
        idx = pd.bdate_range("2024-01-01", periods=2)
        sig = pd.DataFrame({"z": [1.5, -0.3], "bounce_long": [True, True]}, index=idx)
        old = H.RULES["long_mode"]
        H.RULES["long_mode"] = "flow"
        try:
            out = H._apply_long_mode(sig["z"], sig)
        finally:
            H.RULES["long_mode"] = old
        assert list(out) == [1.5, -0.3]

    def test_缺列报错(self):
        idx = pd.bdate_range("2024-01-01", periods=2)
        sig = pd.DataFrame({"z": [0.0, 0.0]}, index=idx)
        old = H.RULES["long_mode"]
        H.RULES["long_mode"] = "unload_bounce"
        try:
            with pytest.raises(ValueError):
                H._apply_long_mode(sig["z"], sig)
        finally:
            H.RULES["long_mode"] = old


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

    def test_鸡蛋走成本进场信号(self):
        """DEC-112,运营者拍板。这颗钉子上一代钉的是 DEC-096 的
        「开做多 + 要 dip」—— 换成本信号时它按设计红了一次,这正是它的工作。
        五道闸门 5/5 见 REPORT_COST_GATES_v1;dip 关掉是因为「价不劣于成本」
        本身就是不追高,再叠 dip 是双重计数。"""
        H.use("JD")
        assert H.RULES["signal_source"] == "cost"
        assert H.RULES["long_enabled"] is True
        assert H.RULES["long_needs_dip"] is False
        # 纯碱同日跟进(DEC-113,闸门 4/5 **知情破例**,不是验证通过)
        H.use("SA")
        assert H.RULES["signal_source"] == "cost"
        assert H.RULES["long_needs_dip"] is False
        # 玻璃(DEC-114):成本 + 两条附加;鸡蛋纯碱那两条必须是默认关
        H.use("FG")
        assert H.RULES["signal_source"] == "cost"
        assert H.RULES["cost_need_adding"] is True and H.RULES["cost_min_age"] == 2
        H.use("JD")
        assert H.RULES["cost_need_adding"] is False and H.RULES["cost_min_age"] == 0
        # 焦煤生猪不许被顺手带成 cost
        for code in ("JM", "LH"):
            H.use(code)
            assert H.RULES["signal_source"] == "resonance", code
        # 焦煤做多已开、不要 dip(DEC-116 知情破例),出场走机构出场(DEC-117);
        # 生猪仍只做空、四件套出场 —— inst 出场在别家全输,不许被顺手带上
        H.use("JM")
        assert H.RULES["long_enabled"] is True and H.RULES["long_needs_dip"] is False
        assert H.RULES["exit_mode"] == "inst"
        for code in ("LH", "FG", "SA", "JD"):
            H.use(code)
            assert H.RULES["exit_mode"] == "retail", code
        # 生猪(DEC-118):做多开,但只由卸仓反弹触发;别的品种 long_mode 必须是 flow
        H.use("LH")
        assert H.RULES["long_enabled"] is True and H.RULES["long_needs_dip"] is False
        assert H.RULES["long_mode"] == "unload_bounce" and H.RULES["long_unload_min"] == 0.30  # DEC-127 由 0.50 降
        for code in ("FG", "SA", "JD", "JM"):
            H.use(code)
            assert H.RULES["long_mode"] == "flow", code


class TestFixedGroup:
    """DEC-122(2026-08-23 运营者拍板):生猪席位组改成固定 5 家,不滚动重选。"""

    def test_生猪配了固定五家其余品种仍滚动(self):
        H.use("LH")
        assert H.RULES["fixed_members"] == ["国泰君安", "东证期货", "东吴期货", "永安期货", "南华期货"]
        # 焦煤 DEC-125 固定过一天又改回滚动(DEC-126):固定名单弱的主因是失去按年重选
        for code in ("FG", "SA", "JD", "JM"):
            H.use(code)
            assert H.RULES["fixed_members"] is None, code

    def test_固定名单逐日同一组且没有重选切点(self):
        """形状必须与 `rolling_groups` 一致,下游 `signal_series`/`build_payload` 不分叉。
        `log` 只一条、日期写拍板日;`cuts` 为空 —— 界面据此不再写「下次重选」。"""
        idx = bdays("2026-03-02", 6)
        seat = pd.DataFrame({"trade_date": list(idx) * 2, "contract": "LH2611",
                             "member_key": ["甲"] * 6 + ["乙"] * 6,
                             "net": [100] * 6 + [-50] * 6, "net_off": [100] * 6 + [-50] * 6,
                             "source": "akshare_v1"})
        price = pd.DataFrame({"trade_date": idx, "contract": "LH2611",
                              "settle": [100.0] * 6, "source": "akshare_v1"})
        ser, log, cuts = H.fixed_groups(["甲", "乙"], seat, price, idx, "2026-03-04")
        assert list(ser.index) == list(idx)
        assert all(g == ("甲", "乙") for g in ser)
        assert cuts == []
        assert len(log) == 1 and log[0]["date"] == "2026-03-04" and log[0]["members"] == ["甲", "乙"]
        assert set(log[0]["alpha"]) == {"甲", "乙"}


class TestRollBounce:
    """DEC-123:换月反弹提示(生猪专用)。主力剩 ≤22 日且近 20 日跌 ≥5% → 提示买次主力 X+2。"""

    def test_次主力合约按月份加二并跨年进位(self):
        assert H.next_main_contract("LH2605") == "LH2607"
        assert H.next_main_contract("LH2611") == "LH2701"
        assert H.next_main_contract("LH2701", 4) == "LH2705"

    def test_只有生猪配了换月反弹其余品种为空(self):
        H.use("LH")
        assert H.RULES["roll_bounce"] == {"since": "2026-01-01", "dleft_max": 22, "drop_min": 0.05}
        for code in ("FG", "SA", "JD", "JM"):
            H.use(code)
            assert H.RULES["roll_bounce"] is None, code

    def test_触发判定与历史只记每个主力首次(self):
        """主力 LH2609 剩 22 日、近 20 日 −11% → 触发;同一主力后面几天不重复记;
        历史从 since 起算;次主力 LH2611 的 20 日涨跌按结算价算。"""
        idx = bdays("2026-07-01", 30)
        st = pd.DataFrame({"LH2609": 11000.0, "LH2611": np.linspace(11985, 12485, 30)}, index=idx)
        mkt = pd.DataFrame({"main": "LH2609", "dleft": np.arange(45, 15, -1),
                            "past": -0.116, "close": 11000.0, "settle": 11000.0}, index=idx)
        cfg = {"since": "2026-01-01", "dleft_max": 22, "drop_min": 0.05}
        out = H.roll_bounce_payload(mkt, st, cfg)
        assert out["active"] is True and out["next"] == "LH2611"
        assert len(out["history"]) == 1
        h = out["history"][0]
        assert h["days_left"] == 22 and h["next"] == "LH2611" and h["drop20"] == -11.6
        assert h["next_ret20"] is not None and h["next_ret20"] > 0
        # 跌得不够 → 不触发、历史为空
        mkt2 = mkt.assign(past=-0.03)
        out2 = H.roll_bounce_payload(mkt2, st, cfg)
        assert out2["active"] is False and out2["history"] == []


class TestLongSince:
    """DEC-124:生猪做多腿只从 2026-01-01 起开,之前的年份做多信号当不存在。"""

    def test_生猪配了起始日其余品种为空(self):
        H.use("LH")
        assert H.RULES["long_since"] == "2026-01-01"
        for code in ("FG", "SA", "JD", "JM"):
            H.use(code)
            assert H.RULES["long_since"] is None, code

    def test_起始日之前做多信号不进场之后照进(self):
        """同一条 z=+3 的做多信号,价格走平:起始日之前不开仓,之后开仓。"""
        idx = bdays("2025-12-22", 10)
        mkt, op, st = frames(idx, "LH2603", [100.0] * 10)
        H.RULES["long_since"] = "2026-01-01"
        z = [QUIET] * 10
        z[1] = 3.0      # 2025-12-23:起始日前
        z[8] = 3.0      # 2026-01-01(起始日当天,次日开盘成交)
        tr = H.replay(signals(idx, z), mkt, op=op, st=st)[0]
        assert all(pd.Timestamp(t["entry_date"]) >= pd.Timestamp("2026-01-01") for t in tr), tr
        assert len(tr) == 1 and tr[0]["side"] == "long"
        H.RULES["long_since"] = None
        tr2 = H.replay(signals(idx, z), mkt, op=op, st=st)[0]
        assert len(tr2) >= 1 and pd.Timestamp(tr2[0]["entry_date"]) < pd.Timestamp("2026-01-01")


class TestGroupOverrides:
    """DEC-129:运营者点名换人,在滚动组之上、只管到下一次重选切点。"""

    def test_玻璃配了华泰换国泰君安其余品种为空(self):
        H.use("FG")
        assert H.RULES["group_overrides"] == [{"since": "2026-08-21", "replace": {"华泰期货": "国泰君安"}}]
        for code in ("LH", "SA", "JD", "JM"):
            H.use(code)
            assert H.RULES["group_overrides"] is None, code

    def test_只在since到下次切点之间替换并写一条手动log(self):
        idx = bdays("2026-08-17", 12)     # 08-17 ~ 09-01
        grp = ("甲", "乙", "丙")
        groups = pd.Series([grp] * len(idx), index=idx, dtype=object)
        cuts = ["2026-05-01", "2026-09-01"]
        seat = pd.DataFrame({"trade_date": list(idx) * 3, "contract": "FG2701",
                             "member_key": ["甲"] * 12 + ["乙"] * 12 + ["丁"] * 12,
                             "net": [100] * 12 + [-50] * 12 + [30] * 12, "net_off": [100] * 12 + [-50] * 12 + [30] * 12,
                             "source": "akshare_v1"})
        price = pd.DataFrame({"trade_date": idx, "contract": "FG2701", "settle": [100.0] * 12, "source": "akshare_v1"})
        g, log = H.apply_group_overrides(groups, [], cuts, [{"since": "2026-08-23", "replace": {"丙": "丁"}}], seat, price)
        assert g[pd.Timestamp("2026-08-21")] == grp, "since 之前不动"
        assert g[pd.Timestamp("2026-08-24")] == ("甲", "乙", "丁"), "since 起换人"
        assert g[pd.Timestamp("2026-09-01")] == grp, "下次切点起照常重选,点名失效"
        assert len(log) == 1 and log[0]["manual"] is True and log[0]["members"] == ["甲", "乙", "丁"]

    def test_新人已在组里时不替换不重复(self):
        idx = bdays("2026-08-24", 3)
        groups = pd.Series([("甲", "丁", "丙")] * 3, index=idx, dtype=object)
        seat = pd.DataFrame({"trade_date": idx, "contract": "FG2701", "member_key": "甲", "net": 1, "net_off": 1, "source": "akshare_v1"})
        price = pd.DataFrame({"trade_date": idx, "contract": "FG2701", "settle": 100.0, "source": "akshare_v1"})
        g, log = H.apply_group_overrides(groups, [], [], [{"since": "2026-08-23", "replace": {"丙": "丁"}}], seat, price)
        assert all(x == ("甲", "丁", "丙") for x in g) and log == []


class TestRearmAfterDelivery:
    """DEC-131:临近交割强平后,同方向信号没断过就不许在新主力续仓;断过一天再出现才算新信号。"""

    @staticmethod
    def _two_contract_world():
        # LH2609 散户窗口止点 2026-08-31:从 8/18 起剩 ≤10 日 → 临近交割。主力 8/18 起换到 LH2611。
        idx = bdays("2026-08-03", 25)   # 08-03 ~ 09-04
        op = pd.DataFrame({"LH2609": 100.0, "LH2611": 110.0}, index=idx)
        st = op.copy()
        main = ["LH2609" if d < pd.Timestamp("2026-08-18") else "LH2611" for d in idx]
        mkt = pd.DataFrame({"main": main, "past": 0.0}, index=idx)
        return idx, mkt, op, st

    def test_强平后信号不断则不续仓(self):
        idx, mkt, op, st = self._two_contract_world()
        tr = H.replay(signals(idx, 3.0), mkt, op=op, st=st)[0]     # 做多信号全程成立
        reasons = [t["exit_reason"] for t in tr]
        assert "临近交割" in reasons, reasons
        k = reasons.index("临近交割")
        assert all(t["side"] != "long" for t in tr[k + 1:]), f"强平后不该在 LH2611 续多: {tr[k+1:]}"

    def test_信号断一天再出现才算新信号(self):
        idx, mkt, op, st = self._two_contract_world()
        z = [3.0] * len(idx)
        gap = list(idx).index(pd.Timestamp("2026-08-24"))
        z[gap] = QUIET                                            # 8/24 信号消失一天
        tr = H.replay(signals(idx, z), mkt, op=op, st=st)[0]
        longs_after = [t for t in tr if t["side"] == "long" and t["contract"] == "LH2611"]
        assert len(longs_after) == 1 and longs_after[0]["entry_date"] >= "2026-08-25", tr

    def test_反方向不受限(self):
        idx, mkt, op, st = self._two_contract_world()
        z = [3.0] * len(idx)
        for k, d in enumerate(idx):
            if d >= pd.Timestamp("2026-08-20"):
                z[k] = -3.0                                       # 强平后转做空信号
        tr = H.replay(signals(idx, z), mkt, op=op, st=st)[0]
        assert any(t["side"] == "short" and t["contract"] == "LH2611" for t in tr), tr

    def test_开关关掉恢复原行为(self):
        idx, mkt, op, st = self._two_contract_world()
        H.RULES["rearm_after_delivery"] = False
        tr = H.replay(signals(idx, 3.0), mkt, op=op, st=st)[0]
        H.RULES["rearm_after_delivery"] = True
        assert any(t["side"] == "long" and t["contract"] == "LH2611" for t in tr), "关掉开关应照旧续仓"
