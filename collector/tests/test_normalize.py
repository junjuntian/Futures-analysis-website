from datetime import UTC, date, datetime

import pandas as pd

from futures_collector.normalize import (
    normalize_catalog,
    normalize_market,
    normalize_seats,
)
from futures_collector.sources import SOURCES


def test_catalog_maps_contract_parameters_and_czce_delivery_month() -> None:
    frame = pd.DataFrame(
        [
            {
                "产品名称": "鲜苹果期货",
                "产品代码": "AP",
                "合约代码": "AP610",
                "交易币种ISO编码": "CNY",
                "交易单位": "10",
                "最小变动价位": "1",
                "第一交易日": "2025-10-16",
                "最后交易日待国家公布2025年节假日安排后进行调整": "2026-10-20",
            }
        ]
    )
    rows = normalize_catalog(SOURCES["CZCE"], date(2026, 8, 2), frame)
    assert rows == [
        {
            "exchange_code": "CZCE",
            "exchange_name": "郑州商品交易所",
            "timezone": "Asia/Shanghai",
            "instrument_code": "AP",
            "instrument_name": "鲜苹果期货",
            "currency_code": "CNY",
            "contract_multiplier": "10",
            "price_tick": "1",
            "contract_code": "AP610",
            "delivery_month": "2026-10",
            "listed_at": "2025-10-16",
            "expires_at": "2026-10-20",
            "source_record_ref": "CZCE:AP610:2026-08-02",
        }
    ]


def test_market_preserves_close_and_settlement_separately() -> None:
    frame = pd.DataFrame(
        [
            {
                "symbol": "au2610",
                "date": "2026-08-01",
                "close": "800.5",
                "settle": "799.5",
            },
            {
                "symbol": "ag2610",
                "date": "2026-08-01",
                "close": "900.5",
                "settle": "899.5",
            },
        ]
    )
    collection_started_at = datetime.now(UTC)
    rows = normalize_market(SOURCES["SHFE"], date(2026, 8, 1), frame, collection_started_at)
    collection_finished_at = datetime.now(UTC)
    assert rows[0]["contract_code"] == "AU2610"
    assert rows[0]["close_price"] == "800.5"
    assert rows[0]["settlement_price"] == "799.5"
    observed_at = datetime.fromisoformat(rows[0]["observed_at"].replace("Z", "+00:00"))
    assert collection_started_at <= observed_at <= collection_finished_at
    assert {row["observed_at"] for row in rows} == {rows[0]["observed_at"]}


def test_seat_row_is_split_into_three_rank_types() -> None:
    frame = pd.DataFrame(
        [
            {
                "symbol": "a2609",
                "rank": 1,
                "vol_party_name": "会员甲",
                "vol": 10,
                "long_party_name": "会员乙",
                "long_open_interest": 20,
                "short_party_name": "会员丙",
                "short_open_interest": 30,
            }
        ]
    )
    rows = normalize_seats(SOURCES["DCE"], date(2026, 8, 1), {"a2609": frame})
    assert [row["rank_type"] for row in rows] == ["volume", "long", "short"]
    assert rows[0]["volume"] == "10"
    assert rows[1]["long_position"] == "20"
    assert rows[2]["short_position"] == "30"


def test_contract_parameters_survive_their_units() -> None:
    # CZCE writes these with the unit attached. A plain Decimal parse rejects
    # the whole field, which is why apple, glass and soda ash reached
    # production with no contract multiplier at all — and a position's profit
    # cannot be computed without one.
    from futures_collector.normalize import _decimal

    assert _decimal("10吨/手") == "10"
    assert _decimal("20吨/手") == "20"
    assert _decimal("1.00元/吨") == "1.00"
    assert _decimal("1,000吨/手") == "1000"
    # Plain values keep working.
    assert _decimal("60") == "60"
    assert _decimal("0.5") == "0.5"
    # A value with no leading number is still refused rather than guessed at.
    assert _decimal("吨/手") == ""
    assert _decimal("") == ""
    assert _decimal("不适用") == ""
