"""郑商所三位合约代码补世纪，锚点必须是交易日。

三位代码只带一个年份数字，十年一轮回。原来锚在品种上市年上，苹果上市 2017，于是
数字 7/8/9 停在 2017/2018/2019，只有 0–6 才被推到 2020 年代——2026 年文件里的
`AP701`（真实含义 2027-01）被展成 `AP1701`，一个 2017 年就交割完的合约出现在
2026-08-07 的席位表里。生产上这样的行有 524,165 条，占郑商所席位的 27%，价格
15,212 条。运营者是在席位页上看见 `AP1701` 才发现的。

下面的用例取自生产实际数据：`AP701` 与 `FG609` 在 2026-08-07 那天的郑商所文件里，
日更（akshare，四位代码）把它们记成 `AP2701` 和 `FG2609`，逐个字段与回填那份相同，
所以正确答案是有旁证的，不是我推的。
"""

import importlib.util
from datetime import date
from pathlib import Path

import pytest

PARSERS = Path(__file__).resolve().parents[2] / "backfill" / "parsers.py"


def load():
    spec = importlib.util.spec_from_file_location("backfill_parsers", PARSERS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("raw", "traded", "expected"),
    [
        # 生产实证：日更把同一条记成 AP2701，字段逐个相同。
        ("AP701", "2026-08-07", "AP2701"),
        ("FG609", "2026-08-07", "FG2609"),
        # 同一天、同一份文件里的其它月份，原来的规则碰巧是对的，不能被改坏。
        ("AP610", "2026-08-07", "AP2610"),
        ("AP611", "2026-08-07", "AP2611"),
        # 历史上真正的 2017 年苹果合约，仍要解析成 2017。
        ("AP701", "2016-11-15", "AP1701"),
        # 交割当月仍在交易，属于「不早于」，不该被推到下一个十年。
        ("AP701", "2017-01-10", "AP1701"),
        # 玻璃上市 2012，跨了两个十年，原规则对 2022 年之后全错。
        ("FG301", "2022-06-15", "FG2301"),
        ("FG309", "2012-12-05", "FG1309"),
    ],
)
def test_the_century_comes_from_the_trade_date(raw, traded, expected):
    assert load().normalise_contract(raw, traded) == expected


def test_a_four_digit_code_passes_through_untouched():
    module = load()
    assert module.normalise_contract("AU2412", "2024-01-05") == "AU2412"
    assert module.normalise_contract("ap2501", "2024-01-05") == "AP2501"


def test_the_result_never_delivers_before_the_trade_date():
    """这条是自证：解析出来的交割年月不得早于交易年月，那在物理上不可能。

    生产上正是用这个判据量出 53 万条错行的——合约不会在交割月之后还挂牌交易。
    """
    module = load()
    traded = date(2026, 8, 7)
    for digit in range(10):
        for month in (1, 3, 5, 9, 12):
            code = module.normalise_contract(f"AP{digit}{month:02d}", traded)
            year = 2000 + int(code[2:4])
            assert (year, int(code[4:6])) >= (traded.year, traded.month), (
                f"{code} 的交割早于交易日 {traded}"
            )


def test_without_a_trade_date_it_refuses_rather_than_guesses():
    """猜出来的合约代码看不出对错，比解析失败糟得多。

    原来的实现在没有锚点时会拿品种上市年顶上，于是错得悄无声息。
    """
    assert load().normalise_contract("AP701") is None
