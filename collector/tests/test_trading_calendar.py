from datetime import date

import pytest

from futures_collector import cli
from futures_collector.trading_calendar import latest_trading_date


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        (date(2026, 2, 21), date(2026, 2, 13)),
        (date(2026, 2, 23), date(2026, 2, 13)),
        (date(2026, 2, 24), date(2026, 2, 24)),
        (date(2026, 4, 6), date(2026, 4, 3)),
        (date(2026, 4, 7), date(2026, 4, 7)),
        (date(2026, 8, 2), date(2026, 7, 31)),
    ],
)
def test_latest_trading_date_covers_holiday_weekend_and_first_reopen(as_of, expected) -> None:
    assert latest_trading_date(as_of) == expected


def test_scheduler_refuses_uncontrolled_year() -> None:
    with pytest.raises(ValueError, match="no coverage"):
        latest_trading_date(date(2027, 1, 4))


def test_resolve_date_cli_needs_no_output_directory(capsys) -> None:
    """--resolve-date 只查日历、不产出数据,所以不该要求 --emit-csv。

    这也是 --emit-csv 用运行期校验而不是 argparse required 的原因:后者会在
    解析阶段就把这条路挡死。"""
    assert cli.main(["--resolve-date", "2026-02-24"]) == 0
    assert capsys.readouterr().out == "2026-02-24\n"
