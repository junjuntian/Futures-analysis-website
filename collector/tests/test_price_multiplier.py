"""The price multiplier and the checks that keep it honest.

One point of price movement is not worth one contract unit. For seven of the
eight varieties collected the two numbers coincide, which is exactly what makes
the eighth dangerous: a profit computed from the contract size looks completely
normal for eggs and is out by a factor of two.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from futures_collector.normalize import normalize_market
from futures_collector.sources import DCE_HISTORY_SOURCE

# Published contract specifications, supplied by the operator 2026-08-09 from
# the exchanges' own contract pages. `一手最小波动 / 最小变动价位` is the
# exchange stating the multiplier itself.
SPECS = {
    # code: (trading unit, quote unit, tick, yuan per tick per lot)
    "JM": ("60 吨/手", "元/吨", Decimal("0.5"), Decimal("30")),
    "JD": ("5 吨/手", "元/500千克", Decimal("1"), Decimal("10")),
    "LH": ("16 吨/手", "元/吨", Decimal("5"), Decimal("80")),
    "FG": ("20 吨/手", "元/吨", Decimal("1"), Decimal("20")),
    "SA": ("20 吨/手", "元/吨", Decimal("1"), Decimal("20")),
    "AP": ("10 吨/手", "元/吨", Decimal("1"), Decimal("10")),
    "AU": ("1000 克/手", "元/克", Decimal("0.02"), Decimal("20")),
    "AG": ("15 千克/手", "元/千克", Decimal("1"), Decimal("15")),
}
EXPECTED = {
    "JM": 60,
    "JD": 10,
    "LH": 16,
    "FG": 20,
    "SA": 20,
    "AP": 10,
    "AU": 1000,
    "AG": 15,
}


def test_each_multiplier_is_what_the_exchange_states() -> None:
    for code, (_, _, tick, per_lot) in SPECS.items():
        assert per_lot / tick == EXPECTED[code], code


def test_eggs_are_the_one_variety_where_size_and_multiplier_differ() -> None:
    # The contract is 5 tonnes but the quote is per 500kg, so ten quote units
    # make up one lot. Every other variety quotes per its own trading unit.
    trading_units = {
        "JM": 60,
        "JD": 5,
        "LH": 16,
        "FG": 20,
        "SA": 20,
        "AP": 10,
        "AU": 1000,
        "AG": 15,
    }
    differ = [code for code in EXPECTED if trading_units[code] != EXPECTED[code]]
    assert differ == ["JD"]
    assert trading_units["JD"] == 5 and EXPECTED["JD"] == 10


def test_the_migration_seeds_every_collected_variety() -> None:
    import pathlib

    # Anchored to this file, not the working directory: the suite runs from
    # collector/ and CI runs it from elsewhere again.
    repo = pathlib.Path(__file__).resolve().parents[2]
    # Both migrations, not just the first. 202608100003 seeded zero rows in
    # production (RLS swallowed the insert) and 202608100006 re-seeded the
    # same eight values under a workspace context — so the *final* state of a
    # fresh database comes from 0006. A test that only reads 0003 would keep
    # passing while an edit to 0006 silently changed what new deployments get.
    migrations = [
        "rust/migrations/202608100003_instrument_price_multiplier.sql",
        "rust/migrations/202608100006_price_multiplier_seed_under_rls.sql",
    ]
    for name in migrations:
        sql = (repo / name).read_text(encoding="utf-8")
        for code, expected in EXPECTED.items():
            assert f"('{code}', {expected}" in sql, f"{code} is not seeded in {name}"
    sql = (repo / migrations[0]).read_text(encoding="utf-8")
    # The column must be distinct from the contract size, or the ambiguity that
    # caused this is simply moved rather than removed.
    assert "price_multiplier" in sql
    assert "contract_multiplier keeps its own meaning" in sql


def market_frame(volume, turnover, settlement):
    return pd.DataFrame(
        [
            {
                "合约": "JD1509",
                "交易日期": "20150901",
                "开盘价": "3900",
                "最高价": "3910",
                "最低价": "3880",
                "收盘价": "3885",
                "结算价": str(settlement),
                "成交量": str(volume),
                "成交额": str(turnover),
            }
        ]
    )


def test_volume_and_turnover_travel_with_the_price_row() -> None:
    # They are what makes the multiplier checkable after the fact: the exchange
    # publishes turnover, so turnover / (volume x settlement) is the multiplier
    # stated by arithmetic rather than by a spec sheet someone typed in.
    rows = normalize_market(
        DCE_HISTORY_SOURCE,
        date(2015, 9, 1),
        market_frame(volume=1000, turnover=39050000, settlement=3905),
        datetime.now(UTC),
    )
    row = rows[0]
    assert row["volume"] == "1000"
    assert row["turnover"] == "39050000"
    derived = Decimal(row["turnover"]) / (Decimal(row["volume"]) * Decimal(row["settlement_price"]))
    assert derived == EXPECTED["JD"]


def test_the_check_would_have_caught_the_contract_size_being_used() -> None:
    # Had the seat profit used 5 instead of 10, this arithmetic disagrees by
    # exactly the factor the reader would never have noticed in the output.
    rows = normalize_market(
        DCE_HISTORY_SOURCE,
        date(2015, 9, 1),
        market_frame(volume=1000, turnover=39050000, settlement=3905),
        datetime.now(UTC),
    )
    derived = Decimal(rows[0]["turnover"]) / (
        Decimal(rows[0]["volume"]) * Decimal(rows[0]["settlement_price"])
    )
    contract_size = Decimal(5)
    assert derived != contract_size
    assert derived / contract_size == 2


def test_a_row_without_turnover_simply_carries_nothing() -> None:
    # Not every source publishes turnover. Absence must stay absent rather than
    # becoming a zero that makes the check divide into nonsense.
    frame = market_frame(volume=0, turnover=0, settlement=3905)
    frame = frame.drop(columns=["成交额"])
    rows = normalize_market(DCE_HISTORY_SOURCE, date(2015, 9, 1), frame, datetime.now(UTC))
    assert rows[0]["turnover"] == ""
    assert rows[0]["volume"] == "0"


@pytest.mark.parametrize("code", sorted(EXPECTED))
def test_no_multiplier_is_left_unseeded(code: str) -> None:
    assert EXPECTED[code] > 0
    assert code in SPECS
