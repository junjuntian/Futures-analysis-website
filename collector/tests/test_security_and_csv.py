import csv
import io
from datetime import date

import httpx
import pytest
import requests

from futures_collector.api import (
    PlatformRequestError,
    confirmation_idempotency_key,
    render_csv,
)
from futures_collector.sources import SOURCES, official_requests_only


def test_csv_uses_fixed_header_and_quotes_values() -> None:
    data = render_csv(
        "seat_positions_v1",
        [
            {
                "exchange_code": "DCE",
                "contract_code": "A2609",
                "trade_date": "2026-08-01",
                "seat_name": "会员,甲",
                "rank_type": "volume",
                "rank": "1",
                "volume": "10",
                "long_position": "",
                "short_position": "",
                "source_record_ref": "DCE:A2609:2026-08-01:volume:1",
            }
        ],
    )
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
    assert rows[0]["seat_name"] == "会员,甲"


def test_non_whitelisted_host_is_rejected_before_request() -> None:
    with official_requests_only(frozenset({"www.dce.com.cn"})):
        with pytest.raises(ValueError, match="whitelist"):
            requests.get("https://example.com/data", timeout=1)


def test_platform_error_exposes_only_stage_status_and_stable_code() -> None:
    response = httpx.Response(
        422,
        json={
            "data": {"code": "automatic_validation_failed"},
            "password": "must-not-appear",
        },
    )
    error = PlatformRequestError("upload", response)
    assert error.safe_code == "upload:422:automatic_validation_failed"
    assert "must-not-appear" not in str(error)


def test_confirmation_idempotency_is_stable_per_import_not_per_daily_replay() -> None:
    first = confirmation_idempotency_key(
        SOURCES["DCE"], "daily_market_prices_v1", date(2026, 7, 30), "import-1"
    )
    retry = confirmation_idempotency_key(
        SOURCES["DCE"], "daily_market_prices_v1", date(2026, 7, 30), "import-1"
    )
    replay = confirmation_idempotency_key(
        SOURCES["DCE"], "daily_market_prices_v1", date(2026, 7, 30), "import-2"
    )

    assert retry == first
    assert replay != first
    assert first.endswith(":import-1")
