import csv
import io

import pytest
import requests

from futures_collector.api import render_csv
from futures_collector.sources import official_requests_only


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
