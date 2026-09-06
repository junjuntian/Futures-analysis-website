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


class Test压力判据:
    """⚡ 压力进场判据(DEC-137 生猪 / DEC-145 鸡蛋)。此前**一个引擎测试都没有**。

    钉住的是**现行**行为:⚡ 在展示窗口(`剩 ≤ window`)内且散户剩仓处历届高位时亮,
    展示级品种(`criterion: False`)恒不亮。

    **同时钉住一个已知的口径缺口**(2026-09-03 首亮前核对查明,未处置,等运营者
    拍板):判据用的是主力的 `剩 ≤ window`(生猪 30/鸡蛋 25),历届分位却取在
    `anchor`(20)上、而且是在合约**自己的**序列上找 —— 那时它已经不是主力了。
    生猪 16 届实测,进窗口那天的散户剩仓比锚点那天中位多 +1,556 手(比值 1.20,
    13/16 届更大),照生产口径重放 PIT 触发的届会换人(LH2501 → LH2503)。
    **别顺手把 window 收成 anchor 去"对齐"**:主力在 dleft 掉到 20 之前就换月了
    (生猪主力剩 ≤20 的天数只有 54/1370),收窄等于把 ⚡ 基本关掉 ——
    `test_不许把判据窗口收成锚点` 就是拦这个的。
    """

    IDX = bdays("2026-03-02", 175)
    CONTRACTS = ["LH2605", "LH2607", "LH2609", "LH2611"]
    # LH2701 只挂行情与成交量,**不进主力序列** —— 当前这一届还没有「继任」,
    # 次主力得按量能选出来,引擎里那条 fallback 走的就是这条路。
    QUOTED = CONTRACTS + ["LH2701"]

    @classmethod
    def _st(cls):
        # 每个合约每天都有结算价,锚点按合约自己的序列找(与引擎口径一致)。
        return pd.DataFrame({c: [1000.0 + i for i in range(len(cls.IDX))]
                             for c in cls.QUOTED}, index=cls.IDX)

    @classmethod
    def _mkt(cls, today: pd.Timestamp):
        """主力序列按 CONTRACTS 顺序推进,最后一届是 LH2611(当前届)。"""
        idx = cls.IDX[cls.IDX <= today]
        cut = max(1, len(idx) // 4)
        mains = []
        for i in range(len(idx)):
            mains.append(cls.CONTRACTS[min(i // cut, len(cls.CONTRACTS) - 1)])
        mains[-1] = "LH2611"
        return pd.DataFrame(
            {"main": mains,
             "dleft": [H.days_to_window_end(m, d) for m, d in zip(mains, idx)]},
            index=idx)

    @classmethod
    def _seat(cls, cur_net: float):
        """三家散户:历届各 1000/2000/3000 手,当前届按参数给。"""
        per = {"LH2605": 1000.0, "LH2607": 2000.0, "LH2609": 3000.0, "LH2611": cur_net}
        rows = []
        for c, tot in per.items():
            for m in H.RULES["retail_seed"]:
                for d in cls.IDX:
                    rows.append({"member_key": m, "contract": c, "trade_date": d,
                                 "net_off": tot / len(H.RULES["retail_seed"])})
        return pd.DataFrame(rows)

    @classmethod
    def _vols(cls):
        # 次主力按 20 日均量选:给更远的合约更大的量。
        return pd.DataFrame({c: [100.0 * (i + 1)] * len(cls.IDX)
                             for i, c in enumerate(cls.QUOTED)}, index=cls.IDX)

    @classmethod
    def _day_with_dleft(cls, want: int) -> pd.Timestamp:
        for d in cls.IDX:
            if H.days_to_window_end("LH2611", d) == want:
                return d
        raise AssertionError(f"夹具里没有剩 {want} 日的交易日")

    @classmethod
    def _payload(cls, dleft: int, cur_net: float = 50000.0, **over):
        H.use("LH")
        cfg = {**H.RULES["roll_pressure"], **over}
        today = cls._day_with_dleft(dleft)
        return H.roll_pressure_payload(cls._seat(cur_net), cls._mkt(today),
                                       cls._st(), cfg, cls._vols())

    def test_夹具确实造出了历届分布(self):
        rp = self._payload(20)
        assert len(rp["history"]) >= 3, f"历届只有 {len(rp['history'])} 届,断言不成立"
        assert rp["level"] == "high", "当前届没被判成高位,后面的用例白测"

    def test_锚点区间内高位就亮并置抑制位(self):
        rp = self._payload(20)
        assert rp["forced"] is True, "被迫方没认出来"
        assert rp["entry_flag"] is True
        assert rp["suppress_long"] is True, "同窗口做多价差信号要降级挂 ⚠(DEC-137)"

    def test_锚点区间之外不亮(self):
        """剩 28 日:表已经在显示(≤30),但回测从没在这一段验过,不许亮。

        旧口径在这里会亮 —— 生猪 LH2609 在 2026-07-20~07-31(剩 30→21 日)真的
        这么亮过十个交易日,而 REPORT_ROLL_PRESSURE_v1 的成绩是在剩 ≤20 上跑的。
        DEC-189 把判据挪到被迫方近月的锚点区间之后,这一段归零。
        """
        rp = self._payload(28)
        assert rp["active"] is True, "展示窗口该是开着的"
        assert rp["forced"] is False
        assert rp["entry_flag"] is False
        assert rp["suppress_long"] is False

    def test_剩五日以内不亮(self):
        """研究量的是「锚点 → 剩 5 日」那一段,剩 5 日内进场零验证覆盖。"""
        rp = self._payload(4)
        assert rp["forced"] is False
        assert rp["entry_flag"] is False

    def test_判据读的是锚点日那一格不是今天(self):
        """两把尺子必须一样长:历届分位取在锚点,当前值也必须取在锚点。

        这同时天然实现了 DEC-137 的「每届一次」—— 整段窗口里判据读同一个数,
        不会因为剩仓当天抖一下就忽明忽灭。
        """
        # 同一届的两天:锚点日(剩 20)与走到一半那天(剩 12)。
        first, later = self._payload(20), self._payload(12)
        assert first["forced"] and later["forced"]
        assert first["anchor_date"] == later["anchor_date"], "锚点日应当是同一天"
        assert later["days_left"] == 12 and first["days_left"] == 20, "夹具没走动"
        assert later["retail_net"] == first["retail_net"], (
            "同一届里判据用的数应当恒定(锚点日那一格),现在跟着今天在变")
        assert later["retail_net_now"] is not None, "今天的剩仓要另给一个键,别和判据值混"

    def test_展示级品种永不亮但照标高位(self):
        rp = self._payload(20, criterion=False)
        assert rp["criterion"] is False
        assert rp["entry_flag"] is False
        assert rp["level"] == "high", "展示级只是不亮 ⚡,高位照标(REPORT_JM_THREE_GAPS_v1)"

    def test_镜像分支只给配了mirror的品种(self):
        """生猪历届零净空样本,镜像分支无从验证,不许亮(REPORT_ROLL_PRESSURE_v1)。"""
        rp = self._payload(20, cur_net=-50000.0)
        assert rp["level"] == "low"
        assert rp["entry_flag_long"] is False, "生猪没配 mirror,做多价差 ⚡ 不该亮"
        assert self._payload(20, cur_net=-50000.0, mirror=True)["entry_flag_long"] is True


class Test残缺行不许顶掉完整行:
    """多源并存时,缺 `open_interest` 的行不能因为来源优先级高就挑中它。

    2026-07-31 / 08-03 / 08-05 三天,JD/JM/LH 的行情由 akshare_v1(只有收盘价与
    结算价,持仓量全空)与新浪(四价持仓量俱全)各写一行。旧写法「先按来源挑一行、
    再让下游发现它缺东西」挑中了 akshare,`main_series` 开头的
    `dropna(subset=["open_interest"])` 于是把**整个交易日**丢掉 —— 那三天没有主力、
    没有收益、没有信号,而且不报错(DEC-145 附记,DEC-081 在 SQL 侧修过同一条纪律)。

    生产实测:全表 447 组多源并存全部是这批行,收盘价与结算价与被顶掉的那行
    逐行相等(447/447),所以正确的选择只补字段、不改价格。
    """

    @staticmethod
    def _rows(crippled: bool = True):
        """五个交易日,中间那天(第 3 天)多出一行残缺的高优先级数据。"""
        rows = []
        for i, d in enumerate(bdays("2026-07-29", 5)):
            px = 4000.0 + i
            rows.append({"contract": "JD2610", "trade_date": d,
                         "open_price": px - 5, "high_price": px + 8,
                         "low_price": px - 9, "close_price": px,
                         "settlement_price": px, "volume": 1000.0,
                         "open_interest": 50000.0 + i, "source": "sina"})
            if crippled and i == 2:
                # akshare 那行:价格与新浪逐行相等,四价与持仓量全空。
                rows.append({"contract": "JD2610", "trade_date": d,
                             "open_price": np.nan, "high_price": np.nan,
                             "low_price": np.nan, "close_price": px,
                             "settlement_price": px, "volume": np.nan,
                             "open_interest": np.nan, "source": "akshare_v1"})
        return pd.DataFrame(rows)

    def test_挑中的是有持仓量的那一行(self):
        df = H.clean_price(self._rows())
        assert len(df) == 5, "同合约同日应当收敛成一行"
        assert df["open_interest"].notna().all(), "残缺行又把完整行顶掉了"
        assert df["open_price"].notna().all()

    def test_那一天不会从主力序列里消失(self):
        mkt = H.main_series(H.clean_price(self._rows()))
        assert len(mkt) == 5, f"少了 {5 - len(mkt)} 个交易日"

    def test_反证_旧的排序真的会丢掉那一天(self):
        """确认夹具能重现事故,否则上面两条测试形同虚设。"""
        raw = self._rows()
        raw["trade_date"] = pd.to_datetime(raw["trade_date"])
        raw["_r"] = H._rank(raw["source"].astype(str), H.PRICE_RANK)
        old = (raw.sort_values(["contract", "trade_date", "_r", "source"])
                  .drop_duplicates(["contract", "trade_date"], keep="first"))
        old["px"] = old["close_price"]
        old["settle"] = old["settlement_price"]
        assert old["open_interest"].isna().any(), "夹具没让 akshare 行胜出"
        assert len(H.main_series(old)) == 4, "旧写法本该丢掉一天"

    def test_只补字段不改价格(self):
        """新旧两条路挑出来的价格必须逐日相等 —— 这一改不是在改口径。"""
        with_bad = H.clean_price(self._rows(crippled=True))
        without = H.clean_price(self._rows(crippled=False))
        for col in ("settle", "px"):
            assert list(with_bad[col]) == list(without[col]), f"{col} 被改动了"

    def test_完整性不许盖过官方源(self):
        """两行都齐全时,仍旧按来源可信度挑 —— 完整性只用来排除残缺行。"""
        rows = self._rows(crippled=False)
        official = rows.copy()
        official["source"] = "dce_official_history"
        official["settlement_price"] = official["settlement_price"] + 100.0
        df = H.clean_price(pd.concat([rows, official], ignore_index=True))
        assert (df["source"] == "dce_official_history").all(), "官方行被顶掉了"


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
        # 纯碱 2026-09-06 起改成 discipline(DEC-218:散户不参与出场),其余仍是 retail
        for code in ("LH", "FG", "JD"):
            H.use(code)
            assert H.RULES["exit_mode"] == "retail", code
        H.use("SA")
        assert H.RULES["exit_mode"] == "discipline"
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
        for code in ("FG", "SA", "JD", "JM", "I"):
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

    def test_只有明确配过的品种才有点名换人(self):
        """点名换人是**逐个品种拍板**的,不是全站开关 —— 没拍过板的必须是 None。

        玻璃 DEC-129(华泰 → 国泰君安)、纯碱 DEC-195(海通 → 瑞达)。
        再有新的要连着这条一起改,别让某个品种悄悄多出一次换人。
        """
        expect = {
            # 玻璃四条(DEC-129/196/199 换人 + DEC-219 剔瑞达)。原本都到 2026-10-01 失效,
            # DEC-220 把冻结日提前到 09-07 之后,冻住的就是换完人的那四家。
            # 玻璃四条:三条换人 + DEC-219 剔瑞达(09-07 生效),最终四家。
            "FG": [{"since": "2026-08-21", "replace": {"华泰期货": "国泰君安"}},
                   {"since": "2026-09-03", "replace": {"海通期货": "瑞达期货"}},
                   {"since": "2026-09-03", "replace": {"中信期货": "海通期货"}},
                   {"since": "2026-09-04", "drop": ["瑞达期货"]}],
            # 纯碱三条:DEC-195 海通→瑞达、DEC-215 华泰→海通、DEC-219 剔瑞达,
            # **按顺序叠加**,最终 = 国泰君安/东证/海通/永安 四家,都到 2027-03-01 失效。
            "SA": [{"since": "2026-09-03", "replace": {"海通期货": "瑞达期货"}},
                   {"since": "2026-09-04", "replace": {"华泰期货": "海通期货"}},
                   {"since": "2026-09-04", "drop": ["瑞达期货"]}],
            "JD": [{"since": "2026-09-03", "replace": {"宏源期货": "东证期货"}}],
        }
        for code, want in expect.items():
            H.use(code)
            assert H.RULES["group_overrides"] == want, code
        # 生猪是固定名单,点名换人走不到它(run_one 只在滚动分支应用),
        # 所以它必须是 None —— 想换生猪的人得直接改 fixed_members(DEC-199)。
        for code in ("LH", "JM", "I"):
            H.use(code)
            assert H.RULES["group_overrides"] is None, code

    def test_纯碱三条点名换人叠加后是运营者要的那四家(self):
        """DEC-215。两条 override 是**按顺序叠加**的,不是各管各的 ——
        只看配置看不出最终阵容,所以把结果本身钉死。

        滚动组 国泰君安/海通/东证/华泰/永安
          → 海通→瑞达  得 国泰君安/瑞达/东证/华泰/永安(09-03 起)
          → 华泰→海通  得 国泰君安/瑞达/东证/海通/永安(09-04 起)
          → 剔掉瑞达    得 **国泰君安/东证/海通/永安**(09-04 起,DEC-219/221)

        如果哪天有人把顺序调换,或者把第二条写成 华泰→瑞达(会撞重复被静默跳过),
        又或者 drop 没生效,这条测试立刻红。
        """
        H.use("SA")
        idx = bdays("2026-09-01", 8)          # 09-01 ~ 09-10
        roll = ("国泰君安", "海通期货", "东证期货", "华泰期货", "永安期货")
        groups = pd.Series([roll] * len(idx), index=idx, dtype=object)
        seat = pd.DataFrame({"trade_date": list(idx), "contract": "SA2701",
                             "member_key": "国泰君安", "net": 1, "net_off": 1,
                             "source": "akshare_v1"})
        price = pd.DataFrame({"trade_date": idx, "contract": "SA2701",
                              "settle": [1000.0] * len(idx), "source": "akshare_v1"})
        g, _log = H.apply_group_overrides(groups, [], ["2027-03-01"],
                                          H.RULES["group_overrides"], seat, price)
        assert set(g[pd.Timestamp("2026-09-02")]) == set(roll)          # 生效日之前不动
        # 09-03 只有第一条生效(海通→瑞达),仍是五家
        assert set(g[pd.Timestamp("2026-09-03")]) == {
            "国泰君安", "瑞达期货", "东证期货", "华泰期货", "永安期货"}
        # 09-04 起三条全生效(DEC-221 把后两条的生效日从 09-07 提到 09-04)
        assert set(g[pd.Timestamp("2026-09-04")]) == {
            "国泰君安", "东证期货", "海通期货", "永安期货"}
        # 三条叠加:换完再剔瑞达,只剩四家(DEC-219,运营者要合约小窗「4 比 4」)
        assert set(g[pd.Timestamp("2026-09-08")]) == {
            "国泰君安", "东证期货", "海通期货", "永安期货"}
        assert len(g[pd.Timestamp("2026-09-08")]) == 4

    def test_剔人不补人且不许把整组剔空(self):
        """DEC-219 的 `drop`:从 since 起剔掉指定席位,**不补人**,组变小。

        为什么不用 `fixed_members`:那个会重写整段历史,DEC-214 实测带 +40pp 前视。
        `drop` 与 `replace` 同一条管道,只从 since 起生效。
        """
        idx = bdays("2026-09-01", 8)
        grp = ("甲", "乙", "丙")
        groups = pd.Series([grp] * len(idx), index=idx, dtype=object)
        seat = pd.DataFrame({"trade_date": list(idx), "contract": "SA2701",
                             "member_key": "甲", "net": 1, "net_off": 1,
                             "source": "akshare_v1"})
        price = pd.DataFrame({"trade_date": idx, "contract": "SA2701",
                              "settle": [100.0] * len(idx), "source": "akshare_v1"})
        g, log = H.apply_group_overrides(groups, [], ["2027-01-01"],
                                         [{"since": "2026-09-04", "drop": ["乙"]}],
                                         seat, price)
        assert g[pd.Timestamp("2026-09-02")] == grp, "生效日之前不动"
        assert g[pd.Timestamp("2026-09-08")] == ("甲", "丙"), "剔人不补人"
        assert log and log[0]["drop"] == ["乙"] and log[0]["manual"] is True
        # 把整组剔空 → 整条不生效,宁可不动也不能产出空组
        g2, _l2 = H.apply_group_overrides(
            groups, [], ["2027-01-01"],
            [{"since": "2026-09-04", "drop": ["甲", "乙", "丙"]}], seat, price)
        assert g2[pd.Timestamp("2026-09-08")] == grp

    def test_换人日必须是有数据的交易日(self):
        """`since` 写成没有行情的日子(周末/节假日/未来),换人会静默不生效。

        2026-09-03 落 DEC-195 时本地快照只到 09-02,跑出来阵容没变 —— 一眼看去
        像是配置没写对。**这类失效不报错**,所以钉一条:since 必须落在工作日上。
        """
        for code in ("FG", "SA", "JD"):
            H.use(code)
            for o in H.RULES["group_overrides"]:
                d = pd.Timestamp(o["since"])
                assert d.weekday() < 5, f"{code} 的 since {o['since']} 是周末"

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


class Test跨月跟随方案:
    """DEC-168 的跨月跟随卡。钉的是两件**错了不会报错、只会给个看着合理的数**的事。

    夹具用运营者 2026-08-31 在焦煤净持仓页看到的**东证**真实结构,因为这一条的正确
    与否**唯一的判据就是"和那页对不对得上"**:多 5,775 / 空 10,059、比例 1:1.74。
    (卡上跟的席位 2026-09-01 已由东证换成中泰,见 CAL_FOLLOW;这里仍用东证那组数
    当夹具 —— 夹具要的是"能用纸笔核对的真实结构",跟当前跟谁无关,换人不该让测试变红。)
    """

    DZ = {"JM2610": -6151.0, "JM2611": -3739.0, "JM2612": 1001.0,
          "JM2701": 3547.0, "JM2702": -169.0, "JM2705": 1227.0}
    PX = {"JM2610": 1759.0, "JM2611": 1700.0, "JM2612": 1690.0,
          "JM2701": 1696.0, "JM2702": 1690.0, "JM2705": 1650.0}

    def setup_method(self):
        H.use("JM")
        H.CURRENT["_main_contract"] = "JM2701"

    def test_两腿合计与净持仓页一致(self):
        p = H.cal_follow_plan_payload("JM", self.DZ, self.PX)
        assert p["state"] == "spread", p
        # **比例照各方向的合计,不是单一最大腿**(DEC-154 在玻纯上踩过:只取最大腿
        # 会把对冲腿低估一半)。5,775 / 10,059 正是净持仓页那两个数。
        assert p["long_lots"] == 5775 and p["short_lots"] == 10059, p
        assert p["ratio"] == 1.74, p

    def test_下单合约优先主力方向对得上的那一侧(self):
        p = H.cal_follow_plan_payload("JM", self.DZ, self.PX)
        legs = {lg["side"]: lg["contract"] for lg in p["legs"]}
        # 主力 JM2701 是净多 → 多腿用主力;空腿主力方向不对,退回最大空腿 JM2610。
        assert legs == {"long": "JM2701", "short": "JM2610"}, p["legs"]

    def test_超过一比三判纯趋势不给方案(self):
        """运营者 2026-08-31:「最多 1:3,超过 1:3 的就算纯趋势」。"""
        one_sided = {"JM2701": 35739.0, "JM2705": -342.0}      # 国泰君安当天 1:104
        p = H.cal_follow_plan_payload("JM", one_sided, self.PX)
        assert p["state"] == "trend" and not p["legs"], p
        # 1:3 取等号仍算套利,过了才不算
        assert H.cal_follow_plan_payload(
            "JM", {"JM2701": 1000.0, "JM2610": -3000.0}, self.PX)["state"] == "spread"
        assert H.cal_follow_plan_payload(
            "JM", {"JM2701": 1000.0, "JM2610": -3001.0}, self.PX)["state"] == "trend"

    def test_单边不给方案(self):
        p = H.cal_follow_plan_payload("JM", {"JM2701": 3547.0}, self.PX)
        assert p["state"] == "trend" and not p["legs"], p

    def test_远近标签按合约月份不按多空(self):
        """2026-09-01:原来写死「多=远月、空=近月」,只对「多远空近」型成立。

        换成中泰这种**多近空远**的席位(当日 多 JM2610 7,345 / 空 JM2701 12,568),
        两个标签会整个标反 —— 而这张卡是给人照着下单看的,标反比不标更糟。
        """
        zt = {"JM2610": 7345.0, "JM2612": 711.0, "JM2701": -12568.0, "JM2702": 148.0}
        p = H.cal_follow_plan_payload("JM", zt, self.PX)
        assert p["state"] == "spread", p
        by_side = {lg["side"]: lg["contract"] for lg in p["legs"]}
        assert by_side["long"] == "JM2610" and by_side["short"] == "JM2701", p["legs"]
        # 多腿在近月 → 多腿那条 split 必须叫「近月净」
        labels = {lg["side"]: sp["label"] for lg, sp in zip(p["legs"], p["splits"])}
        assert labels["long"] == "近月净" and labels["short"] == "远月净", p["splits"]
        # 对照:东证是多远空近,标签正好相反
        q = H.cal_follow_plan_payload("JM", self.DZ, self.PX)
        qlabels = {lg["side"]: sp["label"] for lg, sp in zip(q["legs"], q["splits"])}
        assert qlabels["long"] == "远月净" and qlabels["short"] == "近月净", q["splits"]

    def test_保证金宁少不多绝不超预算(self):
        """运营者 2026-09-01:「改成宁少不多」。

        原来手数四舍五入,一取整就可能超预算 —— 中泰那张 6/9 手实测 37.2%,
        而卡头写的是 35%。**一张讲仓位的卡,自己的两个数打架就没法用。**
        这里按多组资金量扫一遍,保证金占比**永远 ≤ 卡头写的那个数**。
        """
        for cap in (200000.0, 500000.0, 1000000.0, 3000000.0):
            p = H.cal_follow_plan_payload("JM", self.DZ, self.PX, {"capital": cap})
            if p["state"] != "spread":
                continue          # 资金太小撑不起两腿,那时不给方案,也算对
            assert p["margin"] <= cap * 0.35 + 1, (cap, p["margin"], p["margin_pct"])
            assert p["margin_pct"] <= 35.0, (cap, p["margin_pct"])

    def test_套利只收单边保证金(self):
        """运营者 2026-09-01:「JM 单边保证金是 13%,套利只收单边保证金。」

        跨期套利指令的占用是**两腿里较大的那一腿**,不是两腿相加。这不是费率微调:
        占用砍掉近一半,同样预算下手数接近翻倍(实测 5/8 手 → 9/13 手)。
        钉住报出来的 margin **等于较大那一腿**,而不是两腿之和。
        """
        p = H.cal_follow_plan_payload("JM", self.DZ, self.PX)
        cfg = H.CAL_FOLLOW["JM"]
        per_leg = [lg["lots"] * cfg["mult"] * lg["px"] * cfg["margin"] for lg in p["legs"]]
        assert abs(p["margin"] - max(per_leg)) <= 1, (p["margin"], per_leg)
        assert p["margin"] < sum(per_leg) - 1, "两腿相加就是没按单边收"


class TestIH核心席位看板:
    """DEC-172 的 IH 看板。钉住那条**合成规则**——它是运营者拍板的口径,
    不是我挑的:在场的那几家方向一致才给方向,有分歧就观望。

    夹具手工构造(五个交易日、整数价),期望值能用纸笔算出来 —— 用真实数据当夹具
    等于把今天的历史冻进测试(见本文件开头)。
    """

    def _world(self):
        idx = pd.to_datetime(["2026-08-24", "2026-08-25", "2026-08-26",
                              "2026-08-27", "2026-08-28"])
        return idx

    def _seat_rows(self, rows):
        """rows = [(日期, 会员, 净持仓)] → clean_seat 认得的原始形状。"""
        out = []
        for d, m, net in rows:
            side = "long" if net > 0 else "short"
            out.append({"instrument": "IH", "contract": "IH2609", "is_variety_total": "f",
                        "trade_date": d, "rank_type": side, "member": m,
                        "quantity": abs(net), "change": 0, "source": "cffex_seats_v1"})
        return pd.DataFrame(out)

    def _price(self, idx):
        return pd.DataFrame({
            "exchange": "CFFEX", "instrument": "IH", "contract": "IH2609",
            "trade_date": [d.strftime("%Y-%m-%d") for d in idx],
            "open_price": [100, 101, 102, 103, 104],
            "high_price": [105] * 5, "low_price": [95] * 5,
            "close_price": [100, 101, 102, 103, 104],
            "settlement_price": [100, 101, 102, 103, 104],
            "volume": [10] * 5, "open_interest": [100] * 5, "source": "t",
        })

    def test_同向才给方向_分歧则观望(self):
        idx = self._world()
        H.VARIETIES["IH"]["replay_start"] = "2026-08-24"
        # 两家同为净空 → 应给 short
        same = self._seat_rows([("2026-08-28", "摩根大通", -100),
                                ("2026-08-28", "高盛期货", -200)])
        p = H.ih_follow_payload(self._price(idx), same)
        assert p["state"] == "short" and p["on_count"] == 2, p

        # 一多一空 → 分歧,观望(**不是**按手数大的那家算)
        split = self._seat_rows([("2026-08-28", "摩根大通", -100),
                                 ("2026-08-28", "高盛期货", +5000)])
        q = H.ih_follow_payload(self._price(idx), split)
        assert q["state"] == "split" and q["on_count"] == 2, q

    def test_沿用不许跨过持仓合约的交割日(self):
        """DEC-191:掉榜沿用 20 日,但持仓那个合约退市了就必须停。

        实抓:摩根大通 2026-07-21~08-20 的 697 手空单全在 IH2608 上,IH2608 于
        08-21 交割,看板照旧显示「在场·净空 697 手」,还要显示到沿用期用完。
        硬证据是 697 手高于当时**每一个**合约的空头榜门槛(2609 空 501 /
        2612 空 220 / 2703 空 51)—— 还拿着就不可能不在榜上。
        这一轮因此被记成「仍在场 −0.32%」,真实是「交割了结 +1.6%」,**符号是反的**。
        """
        idx = pd.to_datetime(["2026-08-24", "2026-08-25", "2026-08-26",
                              "2026-08-27", "2026-08-28"])
        H.VARIETIES["IH"]["replay_start"] = "2026-08-24"
        px = self._price(idx)
        # 老合约 IH2608 只活到 08-25 就退市;新合约 IH2609 全程都在。
        old = px.copy()
        old["contract"] = "IH2608"
        old = old[old["trade_date"] <= "2026-08-25"]
        price = pd.concat([px, old], ignore_index=True)

        def rows(contract):
            return pd.DataFrame([{
                "instrument": "IH", "contract": contract, "is_variety_total": "f",
                "trade_date": "2026-08-25", "rank_type": "short", "member": "摩根大通",
                "quantity": 697, "change": 0, "source": "cffex_seats_v1"}])

        # 持仓挂在**已退市**的 IH2608 上:08-25 之后不许再沿用。
        dead = H.ih_follow_payload(price, rows("IH2608"))
        assert dead["on_count"] == 0, "合约都交割了还说在场"
        assert not dead["seats"][0]["on"]

        # 同一天、同样掉榜,但持仓挂在仍在交易的 IH2609 上 → 沿用照旧生效。
        alive = H.ih_follow_payload(price, rows("IH2609"))
        assert alive["on_count"] == 1, "合约还在交易,沿用不该被砍掉"
        assert alive["seats"][0]["on"] and alive["seats"][0]["side"] == "short"

    def test_无人在场就是观望不是空仓信号(self):
        idx = self._world()
        H.VARIETIES["IH"]["replay_start"] = "2026-08-24"
        none = self._seat_rows([("2026-08-28", "中信期货", -100)])   # 不在核心三家里
        p = H.ih_follow_payload(self._price(idx), none)
        assert p["state"] == "flat" and p["on_count"] == 0, p
        assert all(not s["on"] for s in p["seats"]), p["seats"]

    def test_品种名和点值是IH自己的_不是上一个跑过的品种(self):
        """2026-09-01 上线后 Chrome 一眼看出来的:看板顶着「焦煤 JM」的名字。

        根因:读了 `CURRENT`(run_one 设的全局量,跑完五个品种后停在最后一个 JM),
        而 `use()` 只改 RULES 不改 CURRENT。**这里故意先把 CURRENT 弄脏再调**,
        钉住它不许再依赖那个全局量。
        """
        idx = self._world()
        H.VARIETIES["IH"]["replay_start"] = "2026-08-24"
        H.CURRENT = {"code": "JM", "name": "焦煤 JM", "multiplier": 60.0}   # 脏
        rows = self._seat_rows([("2026-08-28", "摩根大通", -100)])
        p = H.ih_follow_payload(self._price(idx), rows)
        assert p["name"] == "上证50 IH", p["name"]
        assert p["multiplier"] == 300.0, p["multiplier"]


# 2026-09-02 接铁矿石时发现的:合约代码的品种字母不是永远两位。
# `"I2601"[2:]` 得到 "01",主力换月那处拿它跟 "2601" 比大小,不报错,只是算错。
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("I2601", ("I", "2601")),      # 单字母品种：正是踩坑的那个
        ("JM2601", ("JM", "2601")),
        ("LH2611", ("LH", "2611")),
        ("i2601", ("I", "2601")),
        ("SPD_JM", ("", "")),          # 认不出来就说认不出来
        ("JM26011", ("", "")),
    ],
)
def test_split_contract_handles_single_letter_varieties(code, expected):
    assert H.split_contract(code) == expected


def test_main_series_rolls_forward_for_a_single_letter_variety():
    """铁矿石主力只向更远月切换,不因为「切了前两位」而回切。

    造两个合约:I2601 先当主力,持仓量交棒给 I2605 之后不再回头。
    旧写法下 ym("I2601")="01"、ym("I2605")="05",比较的是月份,
    跨年时(如 I2701 的 "01")会判成「更近」而回切 —— 这条用例钉住它。
    """
    rows = []
    for i, day in enumerate(pd.date_range("2026-01-05", periods=6, freq="D")):
        # 前三天 I2601 持仓大,后三天 I2705 反超
        for contract, oi in (("I2601", 900 - i * 200), ("I2705", 100 + i * 200)):
            rows.append({
                "exchange": "DCE", "instrument": "I", "contract": contract,
                "trade_date": day, "open_price": 700.0, "high_price": 710.0,
                "low_price": 690.0, "close_price": 700.0 + i, "settlement_price": 700.0 + i,
                "volume": 1000.0, "open_interest": float(oi), "source": "sina",
            })
    mkt = H.main_series(H.clean_price(pd.DataFrame(rows)))
    mains = list(mkt["main"])
    assert mains[0] == "I2601"
    assert mains[-1] == "I2705"
    # 一旦切到远月就不回头
    assert mains.index("I2705") == len(mains) - mains.count("I2705")


# 2026-09-02(DEC-182):retail_panel 变成可逐品种覆盖之后,`use()` 必须**每轮都写**。
# 只在「品种配了」时赋值的话,跑完铁矿石再跑玻璃,玻璃会顶着铁矿石的名单 ——
# 与 2026-09-01 IH 看板顶着「焦煤」名字的是同一个坑(RULES/CURRENT 是全局可变的)。
def test_retail_panel_falls_back_between_varieties():
    H.use("I")
    assert H.RULES["retail_panel"] == H.VARIETIES["I"]["retail_panel"]
    # 紧接着跑一个**没有配**覆盖的品种,必须回落到全站默认,不能继承上一个。
    # 这里原本用玻璃,2026-09-04 玻璃自己配了名单(DEC-202),换成焦煤 ——
    # 挑「没配覆盖的品种」当例子时,下面这条断言保证它真的没配。
    assert "retail_panel" not in H.VARIETIES["JM"]
    H.use("JM")
    assert H.RULES["retail_panel"] == H.DEFAULT_RETAIL_PANEL
    assert "新湖期货" not in H.RULES["retail_panel"]


# 2026-09-04:纯碱也换了一组五窗散户名单(运营者指名)。合约卡右列常年空三格,
# 根子在东方财富 —— 它在纯碱上的**单合约**上榜率只有 15.5%,广发是 91.4%。
def test_纯碱散户名单已换掉东方财富():
    """DEC-200 换掉东方财富;DEC-219 起再各去掉一家,**两个品种去掉的不是同一家**。

    合约小窗要「4 比 4」(机构侧同日剔掉瑞达)。纯碱去平安、玻璃去方正中期 ——
    哪天有人图省事把两份改成一致,下面那条不等式就会红。
    """
    H.use("SA")
    assert H.RULES["retail_panel"] == ["徽商期货", "方正中期", "广发期货", "中信建投"]
    # 换人的**唯一理由**就是把东方财富换成广发,这两条各钉一头:
    assert "东方财富" not in H.RULES["retail_panel"]
    assert "广发期货" in H.RULES["retail_panel"]
    assert "平安期货" not in H.RULES["retail_panel"], "纯碱去的是平安"
    H.use("FG")
    assert H.RULES["retail_panel"] == ["平安期货", "徽商期货", "广发期货", "中信建投"]
    assert "方正中期" not in H.RULES["retail_panel"], "玻璃去的是方正中期"
    assert H.VARIETIES["FG"]["retail_panel"] != H.VARIETIES["SA"]["retail_panel"]
    # 回落仍要成立 —— 挑一个**没配**覆盖的品种验
    H.use("JM")
    assert H.RULES["retail_panel"] == H.DEFAULT_RETAIL_PANEL
    assert "广发期货" not in H.RULES["retail_panel"]
    assert "东方财富" in H.RULES["retail_panel"]


def test_展示名单与判据名单不许混成一份():
    """`retail_panel` 只进展示,`retail_seed` 才进判据 —— 两份名单不许对齐。

    原意(2026-09-04):纯碱换展示名单时一个字都不该碰 seed。
    2026-09-05 运营者给玻纯各配了一份 seed(DEC-211),所以不再断言 seed 的具体取值,
    改断言**两份名单不相等** —— 哪天有人图省事把它们对齐,这条就会红。
    对齐是危险的:展示看的是「卡片填不填得满」(单合约上榜率),
    判据看的是「反向择时」,两个口径挑出来的人本就不该一样。
    """
    for code in ("SA", "FG"):
        H.use(code)
        panel = list(H.RULES["retail_panel"])
        seed = list(H.RULES["retail_seed"])
        assert set(panel) != set(seed), f"{code} 的展示名单与判据名单被对齐了"
        # DEC-219 起展示名单是 4 家(合约小窗「4 比 4」),判据名单仍是 3 家、一个字没动
        assert len(seed) == 3 and len(panel) == 4, (code, seed, panel)


def test_retail_seed_只有玻璃纯碱可以覆盖():
    """`retail_seed` 动的是进出场判据,**除玻璃与纯碱外不许逐品种配**。

    这条原来是「谁都不许配」(防止有人照 retail_panel 的样子顺手给 seed 加覆盖,
    在没做品种专属研究的情况下改掉方案 C 的进出场)。
    2026-09-05 运营者点名给玻璃与纯碱各配一份(DEC-211,知情破例,
    预注册 `PLAN_RETAIL_RESELECT_v1` 的 G1/G2 都没过),**守卫不删,改成白名单**:
    这两个品种是运营者明确拍板的,其余品种要配仍必须先立项。
    """
    allowed = {"FG", "SA"}
    for code in H.VARIETIES:
        if code in allowed:
            continue
        assert "retail_seed" not in H.VARIETIES[code], (
            f"{code} 不在白名单里却配了 retail_seed —— 要配先立项")


def test_玻纯的散户名单是运营者点名的那两份():
    H.use("FG")
    assert H.RULES["retail_seed"] == ["中信建投", "平安期货", "广发期货"]
    H.use("SA")
    assert H.RULES["retail_seed"] == ["徽商期货", "方正中期", "中信建投"]


def test_没配的品种必须回落到全站默认三家():
    """RULES 是全局可变的:跑完玻璃再跑焦煤,焦煤不能顶着玻璃那份名单。"""
    H.use("FG")
    assert H.RULES["retail_seed"] == ["中信建投", "平安期货", "广发期货"]
    H.use("JM")
    assert H.RULES["retail_seed"] == H.DEFAULT_RETAIL_SEED
    assert H.RULES["retail_seed"] == ["东方财富", "平安期货", "徽商期货"]
    H.use("LH")
    assert H.RULES["retail_seed"] == H.DEFAULT_RETAIL_SEED


def test_全站默认那三家不许被改():
    """2021 年定死、六年样本外验证过(REPORT_RETAIL_CROSS_v1)。

    玻纯的破例不该扩散到默认值 —— 默认值一动,四个没立过项的品种跟着变。
    """
    assert H.DEFAULT_RETAIL_SEED == ["东方财富", "平安期货", "徽商期货"]


# 2026-09-02(DEC-185):品种级公告与 retail_panel 同款 —— `use()` 必须每轮都写,
# 否则跑完铁矿石再跑玻璃,玻璃会顶着铁矿石那条「要看第二引擎」的公告。
def test_notice_does_not_leak_between_varieties():
    H.use("I")
    assert H.RULES["notice"] and "第二引擎" in H.RULES["notice"]
    H.use("FG")
    assert H.RULES["notice"] is None


def test_notice_is_separate_from_risk_flags():
    """公告是**编辑判断**,风险条是**门槛实算**,两者不能混。

    这条钉住的是:没有任何品种把 notice 塞进 risk_flags 的门槛逻辑里。
    混在一起会让人以为那句话也是算出来的。
    """
    flags = H.risk_flags({"sharpe": 0.5, "cum": 10.0}, [], pd.Series([0.01] * 30),
                         {"sharpe": 0.4, "cum": 8.0})
    assert all("第二引擎" not in f["text"] for f in flags)


# 2026-09-04(运营者:「显示 2 个主力合约,后面跟永安的净持仓」):
# 玻纯跟随卡从三条腿(FG 顺向 / FG 反向 / SA)收成两条腿(一个品种一条),
# 手数比照**全合约净持仓合计**,`member_net` 也换成全合约合计。
class Test玻纯跟随卡收成两条腿:
    """夹具用 2026-09-04 生产库实测的永安持仓,数字可以拿纸笔核。

    FG 全合约净 = +28,846(净多)、SA = −46,359(净空);主力 FG2701 / SA2701。
    """

    YA_FG = {"FG2609": -30.0, "FG2610": 6870.0, "FG2611": -24464.0,
             "FG2612": 7457.0, "FG2701": 36598.0, "FG2702": -83.0,
             "FG2703": 101.0, "FG2705": 2397.0}
    YA_SA = {"SA2609": -580.0, "SA2610": -2442.0, "SA2611": -13836.0,
             "SA2612": -2498.0, "SA2701": -16183.0, "SA2702": -918.0,
             "SA2703": -185.0, "SA2705": -9717.0}
    PX_FG = {c: 932.0 for c in YA_FG}
    PX_SA = {c: 1063.0 for c in YA_SA}

    def _pair(self, fg_main="FG2701", sa_main="SA2701",
              fg_date="2026-09-04", sa_date="2026-09-04",
              member="永安期货", prev=None):
        """prev = {"FG": {...}, "SA": {...}} 时才有昨天,用来测变动。"""
        import pandas as pd
        H.PAIR_EXTRA.clear()
        for k, ya, px, main, d in (("FG", self.YA_FG, self.PX_FG, fg_main, fg_date),
                                   ("SA", self.YA_SA, self.PX_SA, sa_main, sa_date)):
            H.PAIR_EXTRA[k] = {"nets": {member: dict(ya)}, "px_all": dict(px),
                               "main": pd.Series([main]),
                               "date": pd.Timestamp(d),
                               "nets_prev": ({member: dict(prev[k])} if prev else {}),
                               "px_prev": (dict(px) if prev else {}),
                               "date_prev": (pd.Timestamp(d) - pd.Timedelta(days=1)
                                             if prev else None)}

    def test_只出两条腿_一个品种一条(self):
        self._pair()
        p = H.follow_plan_payload()
        assert p["state"] == "opposite", p
        assert len(p["legs"]) == 2, p["legs"]
        assert [lg["instrument"] for lg in p["legs"]] == ["FG", "SA"]
        # 合约必须是主力,不是"该方向最大的那个合约"
        assert [lg["contract"] for lg in p["legs"]] == ["FG2701", "SA2701"]
        assert [lg["side"] for lg in p["legs"]] == ["long", "short"]

    def test_席位持仓显示全合约合计而不是该合约(self):
        self._pair()
        p = H.follow_plan_payload()
        got = {lg["instrument"]: lg["member_net"] for lg in p["legs"]}
        # **全合约合计**:玻璃 +28,846 / 纯碱 −46,359
        assert got["FG"] == 28846, got
        assert got["SA"] == -46359, got
        # 这两个数必须和卡头的 fg_net/sa_net 是同一个东西
        assert p["fg_net"] == 28846 and p["sa_net"] == -46359, p
        # **不能**是主力合约上的持仓(那是 +36,598 / −16,183)
        assert got["FG"] != 36598 and got["SA"] != -16183, got

    def test_手数比照净持仓比例(self):
        self._pair()
        p = H.follow_plan_payload()
        lots = {lg["instrument"]: lg["lots"] for lg in p["legs"]}
        # 目标比 46,359 / 28,846 = 1.607;取整后允许 ±8% 偏差
        want = 46359 / 28846
        got = lots["SA"] / lots["FG"]
        assert abs(got / want - 1) < 0.08, (lots, got, want)

    def test_两腿不同日要告警(self):
        self._pair(sa_date="2026-09-03")
        p = H.follow_plan_payload()
        assert p["stale"], "两条腿取数日期不一致时必须给出告警"
        assert "09-04" in p["stale"] and "09-03" in p["stale"], p["stale"]
        assert p["fg_date"] == "2026-09-04" and p["sa_date"] == "2026-09-03"

    def test_同日不告警(self):
        self._pair()
        p = H.follow_plan_payload()
        assert p["stale"] is None, p["stale"]

    def test_主力当天没价就退回最大腿(self):
        # 主力取不到价(比如当天没数据)时不能整张卡消失,退回绝对值最大的合约
        self._pair(fg_main="FG9999")
        p = H.follow_plan_payload()
        assert len(p["legs"]) == 2, p["legs"]
        assert p["legs"][0]["contract"] == "FG2701", p["legs"]   # |36,598| 最大

    def _aligned(self, member="永安期货"):
        """把纯碱那边翻成净多 → 两个品种同向。"""
        import pandas as pd
        H.PAIR_EXTRA.clear()
        for k, ya, px in (("FG", self.YA_FG, self.PX_FG),
                          ("SA", {c: abs(v) for c, v in self.YA_SA.items()}, self.PX_SA)):
            H.PAIR_EXTRA[k] = {"nets": {member: dict(ya)}, "px_all": dict(px),
                               "main": pd.Series(["FG2701" if k == "FG" else "SA2701"]),
                               "date": pd.Timestamp("2026-09-04"),
                               "nets_prev": {}, "px_prev": {}, "date_prev": None}

    # 2026-09-04 运营者:「同向的时候卡片也要显示,我自己选择席位进行跟随」。
    # 此前同向直接不出方案(DEC-142 状态门),而那道门挡掉了这两家 82~84% 的利润。
    def test_同向也出方案且两腿同一方向(self):
        self._aligned()
        p = H.follow_plan_payload()
        assert p["state"] == "aligned", p["state"]
        assert len(p["legs"]) == 2, p["legs"]
        sides = {lg["side"] for lg in p["legs"]}
        assert len(sides) == 1, ("同向态两腿必须同方向", p["legs"])

    def test_同向态不许给出价差反向那个数(self):
        """两腿同向时「价差反向」反而是对冲生效,照抄会凭空造出不存在的对冲收益。"""
        self._aligned()
        p = H.follow_plan_payload()
        assert p["risk_spread"] is None, p["risk_spread"]
        assert p["risk_same"] is not None
        # 对冲态那边必须仍然有这个数
        self._pair()
        q = H.follow_plan_payload()
        assert q["state"] == "opposite" and q["risk_spread"] is not None, q

    def test_同向态的说明必须当场讲清没有对冲腿(self):
        self._aligned()
        note = H.follow_plan_payload()["note"]
        assert "没有对冲腿" in note, note
        assert "保证金上限" in note, note      # 回撤远超 35% 这句不许省
        # 对冲态不该出现这段
        self._pair()
        assert "没有对冲腿" not in H.follow_plan_payload()["note"]

    def test_有一腿净持平时不出方案(self):
        import pandas as pd
        H.PAIR_EXTRA.clear()
        for k, ya, px in (("FG", self.YA_FG, self.PX_FG),
                          ("SA", {"SA2701": 0.0}, self.PX_SA)):
            H.PAIR_EXTRA[k] = {"nets": {"永安期货": dict(ya)}, "px_all": dict(px),
                               "main": pd.Series(["FG2701" if k == "FG" else "SA2701"]),
                               "date": pd.Timestamp("2026-09-04"),
                               "nets_prev": {}, "px_prev": {}, "date_prev": None}
        p = H.follow_plan_payload()
        assert p["state"] == "flat" and not p["legs"], p


    def test_东证也能出一张卡(self):
        """卡不再写死永安 —— 传谁就出谁(运营者 2026-09-04:「把东证也放进去」)。"""
        self._pair(member="东证期货")
        p = H.follow_plan_payload("东证期货")
        assert p["state"] == "opposite" and p["member"] == "东证期货", p
        assert "东证" in p["note"] and "永安" not in p["note"], p["note"]
        # 同一份 PAIR_EXTRA 里没有永安的仓位,永安那张就该是 None,不能借东证的数
        assert H.follow_plan_payload("永安期货") is None

    def test_变动按品种对齐并带方向(self):
        # 昨天:玻璃净多少一半、纯碱净空多一些 → 今天该加玻璃、减纯碱
        prev = {"FG": {"FG2701": 14000.0}, "SA": {"SA2701": -60000.0}}
        self._pair(prev=prev)
        p = H.follow_plan_payload()
        chg = {lg["instrument"]: lg for lg in p["legs"]}
        assert chg["FG"]["member_net_chg"] == 28846 - 14000
        assert chg["SA"]["member_net_chg"] == -46359 - (-60000)
        # 手数变动 = 仓位增减(不是下单方向)。玻璃净多翻倍 → 这条腿加仓
        assert chg["FG"]["lots_chg"] > 0, chg["FG"]
        # 纯碱净空从 60,000 收到 46,359 → 空腿**减仓**,必须是负数。
        # 若按「下单量」算这里会是正的(买单平空),那正是运营者不要的读法。
        assert chg["SA"]["lots_chg"] < 0, chg["SA"]
        assert chg["FG"]["flip"] is False and chg["SA"]["flip"] is False

    def test_没有昨天的数据时变动为空不报错(self):
        self._pair()                       # 不给 prev
        p = H.follow_plan_payload()
        for lg in p["legs"]:
            assert lg["lots_chg"] is None and lg["member_net_chg"] is None, lg

    def test_说明保持两行内且仍保留两条免责(self):
        """2026-09-04 加了仓位强度那句之后放宽到 130 字(约两行)。

        运营者当天说的是「简短说明,再加 2 行,免得空间不够」—— 两行是给的,
        四行是不行的。上限留在这里,免得以后又被一句句加回四行。
        """
        self._pair()
        note = H.follow_plan_payload()["note"]
        assert len(note) < 130, f"说明太长({len(note)} 字):{note}"
        assert "不是下单指令" in note and "估算" in note, note
        assert "仓位强度" in note, note
        # 2026-09-04:上限降到 12% 之后,note 必须带上实测回撤——
        # 「35% 档 −60%」这句是这次改动的全部理由,省了就等于没披露。
        assert "回撤" in note and "12%" in note, note

    def _with_hist(self, now_fg, now_sa, max_fg, max_sa, member="永安期货"):
        """给 gross_hist 造一段:最后一天是今天的量,中间某天是历史最大。"""
        import pandas as pd
        idx = pd.date_range("2025-01-02", periods=60, freq="B")
        for k, now, mx in (("FG", now_fg, max_fg), ("SA", now_sa, max_sa)):
            v = pd.Series(0.0, index=idx)
            v.iloc[10] = float(mx)
            v.iloc[-1] = float(now)
            H.PAIR_EXTRA[k]["gross_hist"] = {member: v}

    def test_对方轻仓时跟随也轻仓(self):
        """运营者 2026-09-04:「不能一直保证保证金的 35%,看最大持仓来算」。"""
        self._pair()
        self._with_hist(28846, 46359, 28846 * 4, 46359 * 4)     # 强度 = 25%
        p = H.follow_plan_payload()
        assert abs(p["strength_pct"] - 25.0) < 0.6, p["strength_pct"]
        # 保证金必须明显低于上限 35%,大致落在 35% × 25% ≈ 8.75%
        assert p["margin_pct"] < 12, p
        light = {lg["instrument"]: lg["lots"] for lg in p["legs"]}
        # 同一份持仓、把历史最大改成与今日相等(满仓)→ 手数应当明显变多
        self._pair()
        self._with_hist(28846, 46359, 28846, 46359)             # 强度 = 100%
        q = H.follow_plan_payload()
        assert abs(q["strength_pct"] - 100.0) < 0.6, q["strength_pct"]
        full = {lg["instrument"]: lg["lots"] for lg in q["legs"]}
        for k in ("FG", "SA"):
            assert full[k] > light[k] * 2, (k, full, light)

    def test_强度封顶一倍不许超过上限(self):
        # 今天就是历史新高 → 强度算出来 >1,必须封到 1.0,保证金不许超过 35%
        self._pair()
        self._with_hist(28846, 46359, 100, 100)
        p = H.follow_plan_payload()
        assert p["strength_pct"] == 100.0, p["strength_pct"]
        assert p["margin_pct"] <= 35.0, p["margin_pct"]

    def test_轻到跟不出一手时给说明而不是消失(self):
        self._pair()
        self._with_hist(28846, 46359, 28846 * 2000, 46359 * 2000)
        p = H.follow_plan_payload()
        assert p is not None, "整张卡不许消失"
        assert p["state"] == "tiny" and not p["legs"], p
        assert "跟不了" in p["note"], p["note"]

    def test_没有持仓历史时退回满仓不报错(self):
        self._pair()                       # 不设 gross_hist
        p = H.follow_plan_payload()
        assert p["strength_pct"] == 100.0, p
        assert p["gross_max"] is None, p
        # gross_now 不依赖历史序列,永远算得出 = |玻璃净| + |纯碱净|
        assert p["gross_now"] == 28846 + 46359, p["gross_now"]

    def test_今日规模不因两品种末日不同而漏掉一条腿(self):
        """首测踩过:两个品种最后一天不同时,从历史序列末尾取会把缺的填成 0。"""
        import pandas as pd
        self._pair()
        idx_fg = pd.date_range("2025-01-02", periods=50, freq="B")
        idx_sa = pd.date_range("2025-01-02", periods=51, freq="B")   # 多一天
        H.PAIR_EXTRA["FG"]["gross_hist"] = {
            "永安期货": pd.Series(28846.0, index=idx_fg)}
        H.PAIR_EXTRA["SA"]["gross_hist"] = {
            "永安期货": pd.Series(46359.0, index=idx_sa)}
        p = H.follow_plan_payload()
        # 必须是两条腿之和,不是只剩纯碱那 46,359
        assert p["gross_now"] == 28846 + 46359, p["gross_now"]
        assert p["gross_max"] == 28846 + 46359, p["gross_max"]


# 2026-09-04(运营者:「fg 暂时不要重选了,关掉这个自动重选」):
# freeze_since = 从这天起不再重选。**关键是历史照常滚动**——用 fixed_members 会把
# 整段历史按同一组重算,实测玻璃 +248.6%/0.64 → +17.6%/0.10。
class Test关掉自动重选:
    def _g(self):
        import pandas as pd
        idx = pd.date_range("2025-01-01", periods=8, freq="D")
        ser = pd.Series([("A", "B")] * 4 + [("C", "D")] * 4, index=idx, dtype=object)
        return idx, ser, ["2025-01-01", "2025-01-05", "2025-01-09"]

    def test_冻结日之后一路沿用当时那一组(self):
        idx, ser, cuts = self._g()
        g, log, c2 = H.freeze_groups(ser, [], cuts, "2025-01-05")
        # 冻结日当天那一组是 ("C","D"),之后每天都必须是它
        assert all(g[d] == ("C", "D") for d in idx[idx >= "2025-01-05"]), g.to_dict()

    def test_冻结日之前一个字不许动(self):
        idx, ser, cuts = self._g()
        g, _log, _c = H.freeze_groups(ser, [], cuts, "2025-01-05")
        for d in idx[idx < "2025-01-05"]:
            assert g[d] == ser[d], (d, g[d], ser[d])

    def test_未来的重选切点被砍掉(self):
        _idx, ser, cuts = self._g()
        _g, _log, c2 = H.freeze_groups(ser, [], cuts, "2025-01-05")
        assert c2 == ["2025-01-01"], c2      # ≥ 冻结日的都没了

    def test_冻结日在数据之后时不影响任何一天(self):
        """当前正是这种情形:冻结日 2026-09-07,而研究快照到 09-02。"""
        idx, ser, cuts = self._g()
        g, _log, c2 = H.freeze_groups(ser, [], cuts, "2099-01-01")
        assert list(g) == list(ser), "未来冻结日不该改动任何一天的组"
        assert c2 == cuts, "但未来切点仍应保留(它们都早于冻结日)"

    def test_玻璃配了冻结而其它品种没有(self):
        assert H.VARIETIES["FG"].get("freeze_since") == "2026-09-07"
        for code in H.VARIETIES:
            if code != "FG":
                assert not H.VARIETIES[code].get("freeze_since"), code
        # 且不许漏给下一个品种(RULES 是全局可变的)
        H.use("FG")
        assert H.RULES["freeze_since"] == "2026-09-07"
        H.use("SA")
        assert H.RULES["freeze_since"] is None

    def test_冻结日不许落在重选切点上(self):
        """DEC-220 的教训,值得一条独立的钉子。

        冻结日原本写的是 2026-10-01,而那**正好是玻璃下一个重选切点**。
        `apply_group_overrides` 在切点处失效、`freeze_groups` 又跑在它之后,
        于是冻住的会是**失效之后滚动选出来的那 5 家** —— 点名换人三周后自己没了,
        而且**不报错、不留痕**,只有翻日历才看得出来。

        冻结日必须落在「想冻住的那个状态**已经成立**」的那天之后,
        并且**早于**下一个切点。
        """
        H.use("FG")
        fz = pd.Timestamp(H.RULES["freeze_since"])
        sinces = [pd.Timestamp(o["since"]) for o in H.RULES["group_overrides"]]
        assert fz >= max(sinces), "冻结日必须在最后一条点名换人生效之后"
        # 冻结日之后不许还有切点落在它前面被漏掉:freeze_groups 会砍掉 ≥ 冻结日的切点,
        # 所以这里只要求冻结日本身不等于任何一个已知切点。
        assert H.RULES["freeze_since"] not in ("2026-10-01",), (
            "冻结日不能等于重选切点 —— DEC-220 就是栽在这里")

    def test_玻璃没有改用固定名单(self):
        """固定名单会重写整段历史(+248.6% → +17.6%),这次有意不用它。"""
        assert not H.VARIETIES["FG"].get("fixed_members")


class Test跟随卡资金与上限:
    def test_资金一百万上限十二(self):
        assert H.FOLLOW_PLAN["capital"] == 1000000.0
        assert H.FOLLOW_PLAN["use"] == 0.12


class Test出场只留纪律:
    """DEC-218:纯碱把散户那一路从出场里拿掉,只留 止损 / 持满 / 临近交割。"""

    def test_只有纯碱开了discipline并且持满改成六十(self):
        """**逐品种钉死**,免得某个品种悄悄跟着变。

        `max_hold` 这个键在 DEC-218 之前**根本没有逐品种加载** —— 品种里写了
        也不生效、也不报错。加载逻辑是这次补的,所以这条同时钉住加载有没有断。
        """
        want = {"SA": ("discipline", 60), "FG": ("retail", 40), "JD": ("retail", 40),
                "LH": ("retail", 40), "JM": ("inst", 40), "I": ("retail", 40)}
        for code, (mode, hold) in want.items():
            H.use(code)
            assert (H.RULES["exit_mode"], H.RULES["max_hold"]) == (mode, hold), code

    def test_全站默认那两个值不许被改(self):
        """默认一动,四个没立过项的品种跟着变。"""
        assert H.DEFAULT_MAX_HOLD == 40

    def test_discipline下出场序列全是nan而进场一个字不动(self):
        H.use("SA")
        idx = bdays("2026-01-05", 6)
        sig = pd.DataFrame({"cost_z": [1.5, -1.5, 0.0, 1.5, -1.5, 0.0],
                            "z": 0.0, "net": 1.0, "chg": 1.0}, index=idx)
        retail = pd.DataFrame({"net": 1.0, "chg": 1.0,
                               "rz": [2.0, -2.0, 2.0, -2.0, 2.0, -2.0]}, index=idx)
        z_in, z_out = H.entry_exit_signals(sig, retail)
        assert z_in.equals(sig["cost_z"]), "进场那一路必须原样"
        assert z_out.isna().all(), "出场那一路必须整条 NaN"
        # 换成别的品种(retail 口径)时,出场仍旧是散户那一路 —— 不许被这次改动波及
        H.use("FG")
        _zi, zo = H.entry_exit_signals(sig, retail)
        assert zo.equals(retail["rz"])

    def test_纯碱回测里不许再出现散户驱动的出场(self, tmp_path):
        """真跑一遍纯碱,出场原因里「反向」「消退」必须一条都没有。

        判据是 `np.isfinite(z)`:NaN 让反向与消退同时跳过,而止损/持满/交割
        不读这个序列。这条测试就是钉那个「同时跳过」。
        """
        import os
        from pathlib import Path
        d = Path(os.environ.get("CSV_DIR", "research/data"))
        if not (d / "sa_price.csv.gz").exists():
            pytest.skip("本机没有 research/data 快照")
        H.use("SA")
        price = H.clean_price(pd.read_csv(d / "sa_price.csv.gz"))
        seat = H.clean_seat(pd.read_csv(d / "sa_seat.csv.gz"))
        mkt = H.main_series(price)
        op, st = H.contract_prices(price)
        mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
        g, log, cuts = H.rolling_groups(seat, price, mkt.index)
        if H.RULES.get("group_overrides"):
            g, log = H.apply_group_overrides(g, log, cuts, H.RULES["group_overrides"],
                                             seat, price)
        rdf, _ = H.retail_series(seat, mkt.index)
        sig = H.attach_cost_signal(H.signal_series(seat, g), seat, mkt, g)
        trades, _pos, _daily = H.replay(sig, mkt, rdf, op, st)
        # 未平仓那笔 exit_reason 是 None,不算出场原因(数据末日正好持仓中时会出现)
        reasons = {t["exit_reason"] for t in trades if t["exit_reason"]}
        assert reasons <= {"止损", "持满", "临近交割"}, reasons
        assert max(t["hold_days"] for t in trades) <= H.RULES["max_hold"] + 3


class Test几成仓:
    """DEC-222:每家席位自己的仓位水位,只进展示。"""

    def _seat(self, n=200, peak=1000, last=600):
        idx = bdays("2025-01-06", n)
        net = [peak] * (n - 1) + [last]
        return pd.DataFrame({"trade_date": list(idx), "contract": "SA2701",
                             "member_key": "甲", "net": net, "net_off": net,
                             "source": "akshare_v1"}), idx

    def test_水位是自己跟自己比(self):
        seat, idx = self._seat()
        out = H.seat_levels(seat, idx, ["甲"])
        assert out["甲"]["peak"] == 1000
        assert out["甲"]["level"] == 0.6

    def test_预热不足不给值(self):
        """DEC-229 起窗口 200 日、min_periods 60。给两个月数据就算「自身高位」是骗人的。"""
        seat, idx = self._seat(n=40)
        assert H.seat_levels(seat, idx, ["甲"]) == {}

    def test_用当日可见口径不用回榜反推(self):
        """反推值是**回榜日**倒推出来的,当天并不可见 —— 用它就是前视
        (REPORT_PIT_LOOKAHEAD_v1)。`seat_levels` 走 `_pit_pair` 的第一条。
        """
        seat, idx = self._seat()
        seat.loc[seat.index[-1], "net_off"] = np.nan      # 末日掉榜:只有反推值
        out = H.seat_levels(seat, idx, ["甲"])
        assert out == {}, "掉榜日不许拿反推值凑出一个水位"

    def test_不进任何判据(self):
        """`replay` 一个字都不读它 —— 源码里搜得到就是接错了线。"""
        import inspect
        src = inspect.getsource(H.replay)
        for k in ("seat_levels", "SEAT_LEVEL_HOT", '"level"'):
            assert k not in src, k

    def test_门槛与窗口是全站常量不许逐品种配(self):
        # DEC-229 定稿:400 日窗口、分母 = 最高 3 次的平均、三峰彼此至少隔 20 个交易日
        assert (H.SEAT_LEVEL_WIN, H.SEAT_LEVEL_MIN, H.SEAT_LEVEL_HOT) == (400, 120, 0.60)
        assert (H.SEAT_LEVEL_TOPK, H.SEAT_LEVEL_GAP) == (3, 20)
        for code in H.VARIETIES:
            for k in ("seat_level_win", "seat_level_hot", "seat_level_topk", "seat_level_gap"):
                assert k not in H.VARIETIES[code], (code, k)

    def test_分母是最高三次的平均不是单个最大值(self):
        """DEC-229。单日尖峰会把分母顶高、水位常年偏低,运营者据此说「百分比不准」。"""
        v = [100.0] * 399 + [1000.0]
        s = pd.Series(v, index=bdays("2025-01-06", 400))
        assert H.rolling_top_mean(s).iloc[-1] == pytest.approx(400.0)   # (1000+100+100)/3
        assert s.abs().rolling(400, min_periods=120).max().iloc[-1] == 1000.0

    def test_三个峰必须互相隔开(self):
        """**不加间隔就等于没做平均**:实测取到的是同一波建仓顶部的连续三天
        (纯碱海通 2025-06-13/17/24、国泰君安 07-04/07/08、永安 08-07/11/13),
        平均下来几乎等于单个最大值。
        """
        v = [10.0] * 400
        for i, x in ((399, 100.0), (398, 99.0), (397, 98.0), (300, 90.0), (200, 80.0)):
            v[i] = x
        s = pd.Series(v, index=bdays("2025-01-06", 400))
        # 不隔开会取 100/99/98(同一波的连续三天)
        assert H.rolling_top_mean(s, gap=1).iloc[-1] == pytest.approx(99.0)
        # 隔 20 日就跳过 99/98,取到 100/90/80 三个不同的峰
        assert H.rolling_top_mean(s).iloc[-1] == pytest.approx(90.0)


class Test分数仓位:
    """DEC-224:按整组水位建仓,replay 一个字没改。"""

    def test_只有玻纯开了这个开关(self):
        for code in ("SA", "FG"):
            H.use(code)
            assert H.RULES["sizing"] is True, code
        for code in ("JD", "JM", "LH", "I"):
            H.use(code)
            assert H.RULES["sizing"] is False, code

    def test_权重是整组水位不是某一家(self):
        """跟单一席位实测更差(纯碱 0.23,还不如固定缩仓 0.60)。

        这条钉的是**入参**:`sizing_weights` 只接一条合计净持仓序列,
        没有「跟谁」这个参数 —— 想改成跟某一家,得先改签名,改签名就会看到这条注释。
        """
        import inspect
        sg = inspect.signature(H.sizing_weights)
        assert list(sg.parameters) == ["net"]
        net = pd.Series([100.0] * 200 + [40.0], index=bdays("2025-01-06", 201))
        w = H.sizing_weights(net)
        assert w.iloc[-1] == 0.4 and w.iloc[-2] == 1.0

    def test_预热不足不给权重(self):
        net = pd.Series([100.0] * 40, index=bdays("2025-01-06", 40))
        assert H.sizing_weights(net).isna().all()

    def test_上限一不许放大(self):
        assert H.SIZING_PLAN["cap"] == 1.0
        net = pd.Series([10.0] * 200 + [1e9], index=bdays("2025-01-06", 201))
        assert H.sizing_weights(net).max() <= 1.0

    def test_权重用前一日的值不许当天生效(self):
        """当天的水位是当天收盘后才知道的,当天就用它 = 前视(DEC-090 同一条纪律)。"""
        idx = bdays("2025-01-06", 4)
        daily = pd.Series([0.10, 0.10, 0.10, 0.10], index=idx)
        pos = pd.Series([1, 1, 1, 1], index=idx)
        w = pd.Series([0.0, 0.0, 1.0, 1.0], index=idx)
        settle = pd.Series([1000.0] * 4, index=idx)
        out = H.apply_sizing(daily, pos, w, settle, 20.0, fee=0.0)
        assert out.iloc[1] == 0.0, "第 2 天该用第 1 天的权重 0"
        assert out.iloc[3] == pytest.approx(0.10), "第 4 天才吃到满权重"

    def test_调仓成本按真实费率且很小(self):
        """一手名义 = 结算价 × 点值 ≈ 2 万元,2 元/手/边 ≈ 1 个基点。

        DEC-203 的教训:上一版按「名义 × 0.05%」算,错了 4.8~5.3 倍。
        """
        assert H.SIZING_PLAN["fee"] == 2.0
        idx = bdays("2025-01-06", 3)
        daily = pd.Series(0.0, index=idx)
        pos = pd.Series([0, 1, 1], index=idx)
        w = pd.Series([0.0, 1.0, 1.0], index=idx)      # 第 2 天从 0 加到满仓
        settle = pd.Series([1000.0] * 3, index=idx)
        out = H.apply_sizing(daily, pos, w, settle, 20.0)
        assert out.iloc[1] == pytest.approx(-2.0 / (1000.0 * 20.0))   # 正好 1 个基点

    def test_replay一个字都没改(self):
        """分数仓位在汇总处加权,`replay` 仍按满仓记账 —— 源码里搜得到就是接错线。"""
        import inspect
        src = inspect.getsource(H.replay)
        for k in ("sizing", "apply_sizing", "SIZING_PLAN"):
            assert k not in src, k

    def test_手数是名义敞口除以一手名义(self):
        """满仓 = 名义敞口等于总资金(杠杆 1 倍),与运营者「上限 100%」对得上。"""
        idx = bdays("2025-01-06", 200)
        net = pd.Series([100.0] * 199 + [50.0], index=idx)
        settle = pd.Series([1000.0] * 200, index=idx)
        w = H.sizing_weights(net)
        card = H.sizing_card(w, settle, net, "SA", 20.0, -1)
        # 权重 0.5 × 100 万 = 50 万名义;一手 1000×20 = 2 万 → 25 手
        assert card["strength"] == 0.5 and card["lots"] == 25
        assert card["lots_prev"] == 50 and card["lots_chg"] == -25
        assert card["leverage"] == 0.5 and card["margin_rate"] == 0.08


class Test已卸掉的两个口径:
    """DEC-225:展示按近 9 个月高位,判据仍按本轮峰值。**两个数有意并存。**"""

    def test_窗口是九个月(self):
        assert H.UNLOAD_VIEW_WIN == 180

    def test_展示口径不受换组影响(self):
        """这就是运营者指出的那个 bug:2026-09-04 剔掉瑞达之后,
        判据那条把峰值重置成当天的值 → 显示「已卸掉 0%」,而机构其实卸了一大半。
        """
        idx = bdays("2025-01-06", 200)
        net = pd.Series([-300000.0] * 150 + [-100000.0] * 50, index=idx)
        v = H.unload_view(net)
        assert v["pct"] == pytest.approx(1 - 100000 / 300000, abs=1e-3)
        assert v["peak_net"] == -300000 and v["win"] == 180

    def test_预热不足不给值(self):
        net = pd.Series([-100.0] * 10, index=bdays("2025-01-06", 10))
        assert H.unload_view(net)["pct"] is None

    def test_不许拿它去改判据(self):
        """实测滚动窗口当判据会把门大幅收紧(纯碱 26 笔 → 7 笔、+126.3% → +27.1%)。

        `cost_entry_frame` 收的是调用方传进来的 `unload`,而生产的
        `attach_cost_signal` 传的必须是 `unload_series`(本轮峰值)那一条。
        """
        import inspect
        src = inspect.getsource(H.attach_cost_signal)
        assert "unload_series" in src
        assert "unload_window" not in src and "unload_view" not in src
        assert "unload_window" not in inspect.getsource(H.replay)


class Test重仓翻向门:
    """DEC-228(方案丙):某席位在**主力合约**上重仓翻向 → 不往它的反方向进场。"""

    def _mk(self, series, main="FG2701", n=260):
        """series: {席位: [逐日主力净持仓]}(长度 n)。"""
        idx = bdays("2025-01-06", n)
        rows = []
        for m, v in series.items():
            rows.append(pd.DataFrame({"trade_date": list(idx), "contract": main,
                                      "member_key": m, "net": v, "net_off": v,
                                      "source": "akshare_v1"}))
        seat = pd.concat(rows, ignore_index=True)
        mkt = pd.DataFrame({"main": [main] * n, "settle": [1000.0] * n}, index=idx)
        groups = pd.Series([tuple(series)] * n, index=idx, dtype=object)
        return seat, groups, mkt, idx

    def test_三个数里两个是运营者给的(self):
        assert H.FLIP_GATE["lvl"] == 0.60 and H.FLIP_GATE["lvl_win"] == 200
        assert H.FLIP_GATE["back"] == 20      # 这个是实现时定的

    def test_重仓翻多就挡做空(self):
        # 前 240 天净空(峰值 -1000),末 20 天翻成净多 +700(水位 70%)
        v = [-1000.0] * 240 + [700.0] * 20
        seat, g, mkt, idx = self._mk({"甲": v, "乙": [-100.0] * 260})
        b = H.flip_block(seat, g, mkt)
        assert b.iloc[-1] == -1, "翻向者做多 → 挡做空"

    def test_翻向了但仓位不够重就不挡(self):
        v = [-1000.0] * 240 + [100.0] * 20     # 水位 10%
        seat, g, mkt, idx = self._mk({"甲": v, "乙": [-100.0] * 260})
        assert H.flip_block(seat, g, mkt).iloc[-1] == 0

    def test_仓位重但没翻向就不挡(self):
        v = [1000.0] * 260                     # 一直净多,水位 100% 但没翻
        seat, g, mkt, idx = self._mk({"甲": v, "乙": [-100.0] * 260})
        assert H.flip_block(seat, g, mkt).iloc[-1] == 0

    def test_主力合约掉榜那天不判(self):
        """掉榜是「不知道」不是「没有」(PITFALLS 第 4 条)。"""
        v = [-1000.0] * 240 + [700.0] * 20
        seat, g, mkt, idx = self._mk({"甲": v, "乙": [-100.0] * 260})
        seat = seat[~((seat["trade_date"] == idx[-1]) & (seat["member_key"] == "甲"))]
        assert H.flip_block(seat, g, mkt).iloc[-1] == 0

    def test_只有玻纯开了这个开关(self):
        for code in ("SA", "FG"):
            H.use(code)
            assert H.RULES["flip_gate"] is True, code
        for code in ("JD", "JM", "LH", "I"):
            H.use(code)
            assert H.RULES["flip_gate"] is False, code

    def test_只挡一个方向不是两边都挡(self):
        """**运营者原话是「不能跟随三家的方向做」,三家 = 翻向者的对面。**

        两边都挡会把翻向者自己那个方向也堵死,那不是他的意思。
        """
        import inspect
        src = inspect.getsource(H.attach_cost_signal)
        assert "(fb == -1) & (cz < 0)" in src and "(fb == 1) & (cz > 0)" in src

    def test_回测钉住现状(self, tmp_path):
        """玻璃会被挡掉两笔(+52.3%→+46.8%),纯碱不变。红了就是口径变了。"""
        import os
        from pathlib import Path as _P
        d = _P(os.environ.get("CSV_DIR", "research/data"))
        if not (d / "fg_price.csv.gz").exists():
            pytest.skip("本机没有 research/data 快照")
        # DEC-229 定稿(400 日 top3 隔 20)+ 数据拉到 2026-09-04 之后的数
        for code, stem, want in (("FG", "fg", (71, 43.8)), ("SA", "sa", (22, 116.3))):
            H.use(code)
            price = H.clean_price(pd.read_csv(d / f"{stem}_price.csv.gz"))
            seat = H.clean_seat(pd.read_csv(d / f"{stem}_seat.csv.gz"))
            mkt = H.main_series(price)
            op, st = H.contract_prices(price)
            mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
            g, log, cuts = H.rolling_groups(seat, price, mkt.index)
            g, log = H.apply_group_overrides(g, log, cuts, H.RULES["group_overrides"], seat, price)
            if H.RULES.get("freeze_since"):
                g, log, cuts = H.freeze_groups(g, log, cuts, H.RULES["freeze_since"])
            rdf, _ = H.retail_series(seat, mkt.index)
            raw = H.signal_series(seat, g)
            sig = H.attach_cost_signal(raw, seat, mkt, g)
            trades, pos, daily = H.replay(sig, mkt, rdf, op, st)
            sd = H.apply_sizing(daily, pos, H.sizing_weights(raw["net"]).reindex(mkt.index),
                                mkt["settle"], H.RULES["multiplier"])
            assert (len(trades), H._perf(sd)["cum_pct"]) == want, code


class Test换组不清零卸仓:
    """DEC-230:换组时用同方向的近 9 个月高位当卸仓起点,不再清成 0%。"""

    def test_只有玻纯开了这个开关(self):
        for code in ("SA", "FG"):
            H.use(code)
            assert H.RULES["unload_regroup_seed"] is True, code
        # **这是全站共用的函数**,不设开关会连着改掉别的品种
        # (实测鸡蛋 +66.5%/1.43 → +51.1%/1.13)
        for code in ("JD", "JM", "LH", "I"):
            H.use(code)
            assert H.RULES["unload_regroup_seed"] is False, code

    def _run(self, code):
        idx = bdays("2025-01-06", 260)
        # 前 240 天净空,峰值 -300;后 20 天换一批人,净空只剩 -100
        net = [-300.0] * 100 + [-200.0] * 140 + [-100.0] * 20
        seat = pd.DataFrame({"trade_date": list(idx), "contract": "SA2701",
                             "member_key": "甲", "net": net, "net_off": net,
                             "source": "akshare_v1"})
        groups = pd.Series([("甲", "乙")] * 240 + [("甲", "丙")] * 20,
                           index=idx, dtype=object)
        sig = pd.DataFrame({"net": net}, index=idx)
        H.use(code)
        return H.unload_series(sig, seat, groups)

    def test_开着的时候换组不清零(self):
        u = self._run("SA")
        # 换组那天(第 241 天)净空 -100,同方向近 180 日高位是 -300 → 已卸掉 67%
        assert u["pct"].iloc[-1] == pytest.approx(1 - 100 / 300, abs=1e-3)
        assert u["peak_net"].iloc[-1] == -300

    def test_关着的时候还是老行为(self):
        u = self._run("JD")
        # 老行为:换组当天峰值重置成当天的 100 → 已卸掉 0%
        assert u["pct"].iloc[-1] == 0.0
        assert u["peak_net"].iloc[-1] == -100


class Test进场水位上限:
    """DEC-231:机构已建到近 9 个月峰值的 60% 以上就不追。**只给玻璃**。"""

    def test_只有玻璃配了(self):
        H.use("FG")
        assert H.RULES["entry_level_max"] == 0.60
        # **纯碱有意不配**:它赚钱的进场集中在极低与极高水位两头,加上限反而减分
        # (≤60% 时 +116.3%/1.07 → +62.2%/0.81)。别顺手给它开。
        for code in ("SA", "JD", "JM", "LH", "I"):
            H.use(code)
            assert H.RULES["entry_level_max"] is None, code

    def test_水位与已卸掉必须加得到一(self):
        """两个数是同一件事的两面,窗口必须一致,否则页面上会自相矛盾。"""
        assert H.ENTRY_LEVEL_WIN == H.UNLOAD_VIEW_WIN
        net = pd.Series([-300.0] * 199 + [-120.0], index=bdays("2025-01-06", 200))
        lv = H.entry_level(net).iloc[-1]
        pct = H.unload_window(net)["pct"].iloc[-1]
        assert lv == pytest.approx(0.4) and lv + pct == pytest.approx(1.0)

    def test_超过上限就挡住而且给得出理由(self):
        idx = bdays("2025-01-06", 200)
        # 末日 |净持仓| = 270,近 180 日峰值 300 → 水位 90% > 60%,该挡
        net = pd.Series([-300.0] * 199 + [-270.0], index=idx)
        mkt = pd.DataFrame({"settle": [1100.0] * 200, "main": "FG2701"}, index=idx)
        seat = pd.DataFrame({"trade_date": list(idx), "contract": "FG2701",
                             "member_key": "甲", "net": net.values,
                             "net_off": net.values, "source": "akshare_v1"})
        groups = pd.Series([("甲",)] * 200, index=idx, dtype=object)
        sig = pd.DataFrame({"net": net, "chg": -1.0}, index=idx)
        H.use("FG")
        out = H.attach_cost_signal(sig, seat, mkt, groups)
        assert out["cost_z"].iloc[-1] == 0.0
        assert "进得太晚" in str(out["cost_reason"].iloc[-1])
        # 关掉开关就该放行(纯碱口径)
        H.use("SA")
        out2 = H.attach_cost_signal(sig, seat, mkt, groups)
        assert out2["cost_z"].iloc[-1] != 0.0
