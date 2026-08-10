from datetime import UTC, date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from futures_collector.cli import parse_varieties
from futures_collector.normalize import filter_rows_by_variety, row_instrument
from futures_collector.runner import CollectionRunner, EmptyAfterVarietyFilter
from futures_collector.sources import SOURCES

EIGHT = frozenset({"AP", "JD", "JM", "FG", "SA", "AU", "AG", "LH"})


def test_parse_varieties_distinguishes_all_from_nothing() -> None:
    # None means "do not narrow"; an empty selection would mean "collect
    # nothing", which is never intended and must not be silently accepted.
    assert parse_varieties("all") is None
    assert parse_varieties("ALL") is None
    assert parse_varieties("jm, jd ,lh") == frozenset({"JM", "JD", "LH"})
    with pytest.raises(ValueError):
        parse_varieties(" , ")


def test_row_instrument_reads_catalog_market_and_seat_rows() -> None:
    assert row_instrument({"instrument_code": "jm"}) == "JM"
    assert row_instrument({"contract_code": "JM2609"}) == "JM"
    assert row_instrument({"contract_code": "AP2610"}) == "AP"
    # A calendar row names no instrument at all.
    assert row_instrument({"trade_date": "2026-08-07"}) is None


def test_rows_without_an_instrument_survive_the_filter() -> None:
    rows = [
        {"contract_code": "JM2609"},
        {"contract_code": "RB2610"},
        {"trade_date": "2026-08-07"},
    ]
    kept = filter_rows_by_variety(rows, frozenset({"JM"}))
    assert kept == [{"contract_code": "JM2609"}, {"trade_date": "2026-08-07"}]
    # None narrows nothing.
    assert filter_rows_by_variety(rows, None) == rows


class RecordingAdapter:
    """Records what the per-contract crawl was actually asked to fetch."""

    def __init__(self) -> None:
        self.market_varieties = []

    def _frame(self):
        return pd.DataFrame(
            [
                {"symbol": "JM2609", "date": "2026-08-07", "close": 1277, "settle": 1275},
                {"symbol": "RB2610", "date": "2026-08-07", "close": 3400, "settle": 3399},
            ]
        )

    def market(self, source, collection_date):
        return self._frame()

    def eastmoney_dce_market(self, collection_date, varieties=None):
        self.market_varieties.append(varieties)
        return self._frame()

    def eastmoney_dce_catalog(self, collection_date, varieties=None):
        return pd.DataFrame([{"品种名称": "焦煤", "合约": "JM2609"}])


class Platform:
    def __init__(self) -> None:
        self.submitted = []

    def submit(self, source, dataset_type, collection_date, rows):
        self.submitted.append((source.code, dataset_type, len(rows)))
        return SimpleNamespace(import_id="t", inserted=len(rows), skipped=0)

    def record_failure(self, source, dataset_type, collection_date, *, skipped_source_item_count=0):
        return "automatic_source_failed"


def test_whole_exchange_responses_are_narrowed_after_normalization() -> None:
    adapter = RecordingAdapter()
    platform = Platform()
    runner = CollectionRunner(adapter, platform, retry_delay_seconds=0)
    runner.run(date(2026, 8, 7), ["DCE"], ["market"], varieties=frozenset({"JM"}))
    # Only the JM row survives; the RB row belongs to a variety we did not ask for.
    assert platform.submitted == [("DCE", "daily_market_prices_v1", 1)]


def test_the_narrowing_reaches_the_per_contract_crawl() -> None:
    # DCE costs one candle request and one quote request per contract, so
    # filtering afterwards would pay for every variety we did not ask for. The
    # selection has to arrive before the requests are issued.
    adapter = RecordingAdapter()
    runner = CollectionRunner(adapter, Platform(), retry_delay_seconds=0)
    runner._varieties = EIGHT
    runner._collect(SOURCES["DCE"], date(2026, 8, 7), "market", datetime.now(UTC))
    assert adapter.market_varieties == [EIGHT]


def test_an_exchange_with_none_of_the_requested_varieties_is_a_skip_not_a_failure() -> None:
    adapter = RecordingAdapter()
    platform = Platform()
    runner = CollectionRunner(adapter, platform, retry_delay_seconds=0)
    failures = runner.run(date(2026, 8, 7), ["DCE"], ["market"], varieties=frozenset({"CU"}))
    assert failures == 0
    assert platform.submitted == []


def test_the_skip_is_a_dedicated_signal_not_a_generic_error() -> None:
    runner = CollectionRunner(RecordingAdapter(), Platform(), retry_delay_seconds=0)
    runner._varieties = frozenset({"CU"})
    with pytest.raises(EmptyAfterVarietyFilter):
        runner._collect(SOURCES["DCE"], date(2026, 8, 7), "market", datetime.now(UTC))


def test_the_eight_requested_varieties_touch_only_three_exchanges() -> None:
    # Documents the scoping decision: GFEX and CFFEX list none of them, so a
    # backfill for this set never needs to call those two at all.
    mapping = {
        "AP": "CZCE",
        "FG": "CZCE",
        "SA": "CZCE",
        "AU": "SHFE",
        "AG": "SHFE",
        "JM": "DCE",
        "JD": "DCE",
        "LH": "DCE",
    }
    assert set(mapping) == EIGHT
    assert set(mapping.values()) == {"DCE", "CZCE", "SHFE"}
    assert "GFEX" not in mapping.values() and "CFFEX" not in mapping.values()


def test_the_market_cache_key_pins_the_variety_narrowing() -> None:
    # A key mismatch does not raise: the lookup simply misses and falls through
    # to the live network. That is how this suite once spent eight minutes
    # issuing real requests after the key gained a second element. Pin the shape
    # so any future change breaks here first, loudly.
    from futures_collector.sources import AkshareAdapter

    adapter = AkshareAdapter()
    day = date(2026, 8, 7)
    narrowed = pd.DataFrame([{"symbol": "JD2609"}])
    adapter._dce_market_cache[(day, frozenset({"JD"}))] = narrowed
    assert adapter.eastmoney_dce_market(day, frozenset({"JD"})).equals(narrowed)

    unnarrowed = pd.DataFrame([{"symbol": "JM2609"}])
    adapter._dce_market_cache[(day, None)] = unnarrowed
    assert adapter.eastmoney_dce_market(day, None).equals(unnarrowed)
