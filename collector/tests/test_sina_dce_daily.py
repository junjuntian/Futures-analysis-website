"""新浪大商所日更的到货检查，用真实故障当用例。

2026-08-10 大商所的四条路全断，新浪成了唯一还应答的源（`DEC-047`）。用它的代价是
它偶尔会给出不自洽的行，而这些行灌进去不会报任何错——结算价为 0 会让席位持仓成本
按零结算价算，收盘价越界会在套利页上留下一个凭空的尖峰。

下面这些用例不是编的：`2024-09-25` 那天新浪把六个大商所合约的结算价全写成 0，是拿
843 个重叠日对照交易所年度文件时抓出来的；越界那条用的是 JM2506 当天的真实数值
（新浪 1487，交易所 1427，当日成交 2 手）。
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "collector" / "sina-dce-daily.py"


def load_module():
    # 这个脚本装在发布包里、由采集脚本直接调用，不是 collector 包的一部分，
    # 所以按路径加载而不是 import。
    spec = importlib.util.spec_from_file_location("sina_dce_daily", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOOD = {
    "d": "2026-08-10",
    "o": 1264.5,
    "h": 1286.5,
    "l": 1264.5,
    "c": 1273.5,
    "s": 1274.5,
    "v": 422891,
    "p": 331538,
}


def test_a_sound_day_is_accepted():
    assert load_module().usable(GOOD, "JM2609", GOOD["d"]) is None


@pytest.mark.parametrize(
    ("name", "bar", "expected"),
    [
        (
            # 真实故障：2024-09-25 六个合约的结算价全是 0。
            "settlement_zeroed",
            {**GOOD, "d": "2024-09-25", "s": 0},
            "结算价",
        ),
        ("close_zeroed", {**GOOD, "c": 0}, "收盘价"),
        (
            # 真实数值：JM2506 2024-10-18，新浪 1487，交易所 1427，当日成交 2 手。
            "close_outside_the_day_range",
            {**GOOD, "h": 1310, "l": 1290, "c": 1487, "v": 2},
            "不在区间",
        ),
        ("no_trading", {**GOOD, "v": 0}, "成交量"),
        ("no_price_at_all", {**GOOD, "c": "", "s": ""}, "都没有"),
    ],
)
def test_an_unsound_day_is_refused_and_says_why(name, bar, expected):
    reason = load_module().usable(bar, "JM2609", bar["d"])
    assert reason is not None, f"{name} 应当被拒"
    # 理由要能读懂：日志里只写「跳过」，以后回头查那天为什么没数据就查不下去了。
    assert expected in reason, f"{name} 的理由不对：{reason}"


def test_zero_is_treated_as_missing_not_as_a_price():
    """0 不是一个价格。这条单拎出来，因为它是最贵的那个错。

    结算价 0 一旦入库，席位持仓成本会按零结算价算，`build_cost_series` 拿到的是一个
    合法的数而不是缺失，于是它不会标 `no_settlement_on_add`，而是安静地算出一个错的
    成本。整条链路上没有一处会报错。
    """
    module = load_module()
    assert module.usable({**GOOD, "s": 0}, "JM2609", GOOD["d"]) is not None
    assert module.usable({**GOOD, "s": -1}, "JM2609", GOOD["d"]) is not None
    # 缺失（空字符串）与 0 是两回事：缺失时另一个价格还在，这一行仍然有话说。
    assert module.usable({**GOOD, "s": ""}, "JM2609", GOOD["d"]) is None


# 2026-09-02:铁矿石行情停在 08-28 不动,查出来是 `symbol[:2]` —— `"I2601"[:2]`
# 得到 "I2",既过不了 WANT 的门,写进 instrument 列也是错的。WANT 里 08-30 就
# 加了 "I",可日更一行都没采到过,而 JM/JD/LH 照常出数,零产出守卫照不到。
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("I2601", "I"),      # 单字母品种：正是踩坑的那个
        ("JM2601", "JM"),
        ("JD2701", "JD"),
        ("LH2611", "LH"),
        ("i2601", "I"),      # 小写也认，来源大小写不由我们决定
        ("SPD_JM", ""),      # 认不出来就说认不出来，别写出一行畸形数据
        ("I26", ""),
    ],
)
def test_single_letter_varieties_are_not_sliced_in_half(code, expected):
    assert load_module().variety_of(code) == expected


def test_every_wanted_variety_survives_its_own_filter():
    """WANT 里的每个品种都要能被 variety_of 认回来。

    这条测试的意义不在于「I 现在好了」,而在于**下次再加单字母品种(如 A、C、V、
    P、M、Y)时,漏改这里会当场红**。原来的 `symbol[:2]` 对双字母全对、对单字母
    全错,而错的方式是静默跳过。
    """
    module = load_module()
    for symbol in module.WANT:
        code = f"{symbol}2601"
        assert module.variety_of(code) == symbol, code
