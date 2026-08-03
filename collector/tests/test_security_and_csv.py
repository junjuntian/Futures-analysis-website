import csv
import io
import socket
from datetime import date

import httpx
import pytest
import requests

from futures_collector.api import (
    PlatformRequestError,
    confirmation_idempotency_key,
    render_csv,
)
from futures_collector.sources import SOURCES, OutboundPolicyError, official_requests_only


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


def _public_dns(host: str, port: int, *args, **kwargs):
    del args, kwargs
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def _response(url: str, status: int, location: str | None = None) -> requests.Response:
    response = requests.Response()
    response.url = url
    response.status_code = status
    if location is not None:
        response.headers["location"] = location
    response._content = b"ok"
    response._content_consumed = True
    return response


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1:9080/probe",
        "https://not-allowed.example/probe",
    ],
)
def test_redirect_to_forbidden_target_is_never_requested(monkeypatch, location) -> None:
    requested: list[str] = []

    def transport(_session, _method, url, *args, **kwargs):
        del args, kwargs
        requested.append(url)
        return _response(url, 302, location)

    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    monkeypatch.setattr(requests.sessions.Session, "request", transport)
    with official_requests_only(frozenset({"allowed.example"})):
        with pytest.raises(OutboundPolicyError):
            requests.get("https://allowed.example/start", timeout=1)
    assert requested == ["https://allowed.example/start"]


def test_each_allowed_redirect_hop_is_checked_and_limit_is_enforced(monkeypatch) -> None:
    requested: list[str] = []

    def transport(_session, _method, url, *args, **kwargs):
        del args, kwargs
        requested.append(url)
        hop = int(url.rsplit("/", 1)[1])
        return _response(url, 302, f"https://allowed.example/{hop + 1}")

    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    monkeypatch.setattr(requests.sessions.Session, "request", transport)
    with official_requests_only(frozenset({"allowed.example"})):
        with pytest.raises(OutboundPolicyError, match="limit"):
            requests.get("https://allowed.example/0", timeout=1)
    assert requested == [f"https://allowed.example/{hop}" for hop in range(6)]


def test_dns_change_is_rejected_before_transport_receives_request(monkeypatch) -> None:
    requested: list[str] = []
    answers = iter(["93.184.216.34", "127.0.0.1"])

    def changing_dns(host: str, port: int, *args, **kwargs):
        del host, args, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]

    def transport(_session, _method, url, *args, **kwargs):
        del args, kwargs
        requested.append(url)
        return _response(url, 200)

    monkeypatch.setattr(socket, "getaddrinfo", changing_dns)
    monkeypatch.setattr(requests.sessions.Session, "request", transport)
    with official_requests_only(frozenset({"allowed.example"})):
        with pytest.raises(OutboundPolicyError, match="DNS answer changed|non-public"):
            requests.get("https://allowed.example/data", timeout=1)
    assert requested == []


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
