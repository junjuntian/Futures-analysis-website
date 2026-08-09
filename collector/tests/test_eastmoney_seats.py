from datetime import date
from types import SimpleNamespace

import pytest

from futures_collector import sources as sources_module
from futures_collector.normalize import normalize_seats
from futures_collector.runner import CollectionRunner
from futures_collector.sources import (
    EASTMONEY_SEATS_SOURCE_CODE,
    SOURCES,
    AkshareAdapter,
    eastmoney_seats_source,
)


def member(code, market, name, **ranks):
    row = {
        "SECURITY_CODE": code,
        "TRADE_MARKET_CODE": market,
        "MEMBER_NAME_ABBR": name,
        "VOLUME_RANK": 9999,
        "LP_RANK": 9999,
        "SP_RANK": 9999,
        "VOLUME": None,
        "LONG_POSITION": None,
        "SHORT_POSITION": None,
    }
    row.update(ranks)
    return row


DCE = "069001007"
SHFE = "069001005"
INE = "069001016"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def install_pages(monkeypatch, pages):
    calls = []

    def fake_get(url, params=None, headers=None, **kwargs):
        calls.append(params)
        return FakeResponse(pages[params["pageNumber"] - 1])

    monkeypatch.setattr(sources_module.requests, "get", fake_get)
    monkeypatch.setattr(
        sources_module, "official_requests_only", lambda domains: _noop_context()
    )
    return calls


def _noop_context():
    import contextlib

    @contextlib.contextmanager
    def ctx():
        yield

    return ctx()


def envelope(rows, pages=1):
    return {"success": True, "code": 0, "result": {"data": rows, "pages": pages}}


def test_member_rows_are_rebuilt_into_the_exchange_rank_table(monkeypatch) -> None:
    # Eastmoney gives one row per member carrying that member's rank in all
    # three measures; the exchanges publish one row per rank whose three
    # columns name three different members.
    install_pages(
        monkeypatch,
        [
            envelope(
                [
                    member("JM2609", DCE, "甲期货", VOLUME_RANK=1, VOLUME=500),
                    member("JM2609", DCE, "乙期货", VOLUME_RANK=2, VOLUME=300, LP_RANK=1,
                           LONG_POSITION=120),
                    member("JM2609", DCE, "丙期货", LP_RANK=2, LONG_POSITION=90, SP_RANK=1,
                           SHORT_POSITION=80),
                ]
            )
        ],
    )
    tables = AkshareAdapter().eastmoney_seats(eastmoney_seats_source("DCE"), date(2026, 8, 7))

    frame = tables["JM2609"]
    assert list(frame["rank"]) == [1, 2]
    first = frame.iloc[0]
    assert first["vol_party_name"] == "甲期货"
    assert first["long_party_name"] == "乙期货"
    assert first["short_party_name"] == "丙期货"

    rows = normalize_seats(eastmoney_seats_source("DCE"), date(2026, 8, 7), tables)
    assert {(row["rank_type"], row["rank"], row["seat_name"]) for row in rows} == {
        ("volume", "1", "甲期货"),
        ("volume", "2", "乙期货"),
        ("long", "1", "乙期货"),
        ("long", "2", "丙期货"),
        ("short", "1", "丙期货"),
    }


def test_unranked_sentinel_never_becomes_a_rank(monkeypatch) -> None:
    install_pages(
        monkeypatch,
        [
            envelope(
                [
                    member("JM2609", DCE, "甲期货", VOLUME_RANK=1, VOLUME=500),
                    # 9999 means "not ranked for this measure", not rank 9999.
                    member("JM2609", DCE, "乙期货", VOLUME_RANK=9999, VOLUME=1),
                ]
            )
        ],
    )
    tables = AkshareAdapter().eastmoney_seats(eastmoney_seats_source("DCE"), date(2026, 8, 7))
    assert list(tables["JM2609"]["rank"]) == [1]


def test_only_the_requested_exchange_is_returned_and_ine_is_never_admitted(monkeypatch) -> None:
    install_pages(
        monkeypatch,
        [
            envelope(
                [
                    member("JM2609", DCE, "甲期货", VOLUME_RANK=1, VOLUME=500),
                    member("RB2610", SHFE, "乙期货", VOLUME_RANK=1, VOLUME=400),
                    member("SC2609", INE, "丙期货", VOLUME_RANK=1, VOLUME=300),
                ]
            )
        ],
    )
    adapter = AkshareAdapter()
    assert set(adapter.eastmoney_seats(eastmoney_seats_source("DCE"), date(2026, 8, 7))) == {
        "JM2609"
    }
    assert set(adapter.eastmoney_seats(eastmoney_seats_source("SHFE"), date(2026, 8, 7))) == {
        "RB2610"
    }
    # INE is not one of the five catalogued exchanges, so no source maps to it.
    for exchange in SOURCES:
        tables = {}
        try:
            tables = adapter.eastmoney_seats(eastmoney_seats_source(exchange), date(2026, 8, 7))
        except ValueError:
            continue
        assert "SC2609" not in tables


def test_every_exchange_shares_one_download(monkeypatch) -> None:
    calls = install_pages(
        monkeypatch,
        [
            envelope(
                [
                    member("JM2609", DCE, "甲期货", VOLUME_RANK=1, VOLUME=500),
                    member("RB2610", SHFE, "乙期货", VOLUME_RANK=1, VOLUME=400),
                ]
            )
        ],
    )
    adapter = AkshareAdapter()
    adapter.eastmoney_seats(eastmoney_seats_source("DCE"), date(2026, 8, 7))
    adapter.eastmoney_seats(eastmoney_seats_source("SHFE"), date(2026, 8, 7))
    assert len(calls) == 1


def test_a_truncated_crawl_is_refused_rather_than_submitted(monkeypatch) -> None:
    # The report claims more pages than the budget allows, so the day would be
    # incomplete. Submitting a partial seat table is worse than failing.
    monkeypatch.setattr(sources_module, "EASTMONEY_MAX_PAGES", 2)
    install_pages(
        monkeypatch,
        [
            envelope([member("JM2609", DCE, "甲期货", VOLUME_RANK=1, VOLUME=1)], pages=99),
            envelope([member("JM2609", DCE, "乙期货", VOLUME_RANK=2, VOLUME=1)], pages=99),
        ],
    )
    with pytest.raises(ValueError, match="page budget"):
        AkshareAdapter().eastmoney_seats(eastmoney_seats_source("DCE"), date(2026, 8, 7))


def test_eastmoney_is_refused_for_every_dataset_but_seats() -> None:
    runner = CollectionRunner(AkshareAdapter(), SimpleNamespace(), retry_delay_seconds=0)
    for dataset in ("catalog", "market", "calendar"):
        with pytest.raises(ValueError, match="only serves the seats dataset"):
            runner._collect(
                eastmoney_seats_source("DCE"),
                date(2026, 8, 7),
                dataset,
                None,
                fallback=False,
            )


def test_eastmoney_is_tried_last_and_never_ahead_of_a_working_exchange() -> None:
    from futures_collector.runner import _fallback_chain

    # Nothing is attempted before the official source fails at all: the chain is
    # only consulted after it has.
    dce_seats = [candidate.source_code for candidate, _ in _fallback_chain(SOURCES["DCE"], "seats")]
    assert dce_seats == ["akshare_sina_dce_fallback", EASTMONEY_SEATS_SOURCE_CODE]

    # DCE market keeps Sina only -- Eastmoney has no settlement price.
    dce_market = [
        candidate.source_code for candidate, _ in _fallback_chain(SOURCES["DCE"], "market")
    ]
    assert dce_market == ["akshare_sina_dce_fallback"]

    for exchange in ("SHFE", "CZCE", "GFEX", "CFFEX"):
        seats = [
            candidate.source_code for candidate, _ in _fallback_chain(SOURCES[exchange], "seats")
        ]
        assert seats == [EASTMONEY_SEATS_SOURCE_CODE]
        assert _fallback_chain(SOURCES[exchange], "market") == []
