from datetime import date

import pandas as pd
import pytest

from futures_collector import sources as sources_module
from futures_collector.sources import AkshareAdapter, _derive_dce_contracts


def test_candidates_are_the_current_delivery_month_through_twelve_ahead() -> None:
    codes = _derive_dce_contracts(date(2019, 6, 14), frozenset({"JM"}))
    assert codes == [
        "JM1906", "JM1907", "JM1908", "JM1909", "JM1910", "JM1911", "JM1912",
        "JM2001", "JM2002", "JM2003", "JM2004", "JM2005", "JM2006",
    ]


def test_candidates_roll_the_year_and_cover_every_requested_variety() -> None:
    codes = _derive_dce_contracts(date(2020, 12, 31), frozenset({"JD", "LH"}))
    assert codes[0] == "JD2012" and "JD2112" in codes
    assert codes[13] == "LH2012" and "LH2112" in codes
    assert len(codes) == 26


def install_history(monkeypatch, table):
    """`table` maps contract -> frame, or contract -> an exception to raise."""
    def fake(symbol):
        outcome = table.get(symbol)
        if outcome is None:
            # Not listed on this date: akshare cannot parse Sina's answer.
            raise IndexError("list index out of range")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sources_module.akshare, "futures_zh_daily_sina", fake)
    monkeypatch.setattr(
        sources_module.akshare, "futures_contract_detail", lambda symbol: pd.DataFrame()
    )
    import contextlib

    @contextlib.contextmanager
    def open_gate(domains):
        yield

    monkeypatch.setattr(sources_module, "official_requests_only", open_gate)


def history(*dates):
    return pd.DataFrame({"date": list(dates), "close": [1] * len(dates)})


def test_a_historical_date_yields_the_contracts_that_actually_traded(monkeypatch) -> None:
    # The whole point of the change: the live listing endpoints answer for
    # today, so backfilling 2019 used to look for today's contracts, find
    # nothing, and fail the dataset.
    install_history(
        monkeypatch,
        {
            "JM1906": history("2019-06-14"),
            "JM1909": history("2019-06-14"),
            "JM2001": history("2019-06-14"),
        },
    )
    catalog = AkshareAdapter()._dce_catalog(date(2019, 6, 14), frozenset({"JM"}))
    assert sorted(catalog["合约"]) == ["JM1906", "JM1909", "JM2001"]


def test_both_upstream_failure_shapes_mean_not_listed(monkeypatch) -> None:
    # A delisted contract older than Sina keeps raises ValueError; one not
    # listed yet raises IndexError. Neither says the contract traded.
    install_history(
        monkeypatch,
        {
            # A hit first, so the run is plainly being answered: the guard for a
            # refusing upstream only applies while nothing has succeeded.
            "JM1906": history("2019-06-14"),
            "JM1909": history("2019-06-14"),
            "JM1910": ValueError("Length mismatch: Expected axis has 0 elements"),
            "JM1911": IndexError("list index out of range"),
        },
    )
    catalog = AkshareAdapter()._dce_catalog(date(2019, 6, 14), frozenset({"JM"}))
    assert sorted(catalog["合约"]) == ["JM1906", "JM1909"]


def test_a_transport_failure_is_never_read_as_not_listed(monkeypatch) -> None:
    # Silently treating "the network broke" as "the contract does not exist"
    # would shrink a catalog without anyone noticing.
    import requests

    install_history(
        monkeypatch, {"JM1909": requests.ConnectionError("connection reset")}
    )
    with pytest.raises(requests.RequestException):
        AkshareAdapter()._dce_catalog(date(2019, 6, 14), frozenset({"JM"}))


def test_a_wholly_unreadable_day_blames_the_upstream_not_the_calendar(monkeypatch) -> None:
    # Sina answers a rate limit with an HTML page and HTTP 456, which is a
    # successful transaction to the client and an ordinary parse error to the
    # wrapper. Reporting that as "nothing traded that day" would be a lie.
    install_history(monkeypatch, {})
    with pytest.raises(ValueError, match="refusing the run"):
        AkshareAdapter()._dce_catalog(date(2019, 6, 14), frozenset({"JM"}))


def test_a_contract_history_crosses_the_network_once(monkeypatch) -> None:
    calls = []

    def counting(symbol):
        calls.append(symbol)
        if symbol in {"JM1906", "JM1909"}:
            return history("2019-06-14")
        raise IndexError("list index out of range")

    monkeypatch.setattr(sources_module.akshare, "futures_zh_daily_sina", counting)
    monkeypatch.setattr(
        sources_module.akshare, "futures_contract_detail", lambda symbol: pd.DataFrame()
    )
    import contextlib

    @contextlib.contextmanager
    def open_gate(domains):
        yield

    monkeypatch.setattr(sources_module, "official_requests_only", open_gate)

    adapter = AkshareAdapter()
    day = date(2019, 6, 14)
    adapter._dce_catalog(day, frozenset({"JM"}))
    before = len(calls)
    # The market dataset reuses the catalog's fetches instead of repeating them.
    adapter.fallback_market(sources_module.DCE_FALLBACK_SOURCE, day, frozenset({"JM"}))
    assert calls[before:] == []


def test_without_a_variety_selection_the_live_listing_is_still_used(monkeypatch) -> None:
    # There is nothing to enumerate without a selection, so today's listing
    # remains the only available basis. Guard against the derived path being
    # reached with nothing to derive from.
    called = []
    monkeypatch.setattr(
        AkshareAdapter,
        "_derived_dce_catalog",
        lambda self, d, v: called.append(v) or pd.DataFrame(),
    )
    adapter = AkshareAdapter()
    adapter._dce_catalog_cache[(date(2019, 6, 14), None)] = pd.DataFrame([{"合约": "JM1909"}])
    adapter._dce_catalog(date(2019, 6, 14))
    assert called == []


def test_only_dce_varieties_are_derived() -> None:
    # The Sina exception is DCE-only. Enumerating a CZCE or SHFE variety here
    # would spend one request per delivery month to rediscover what the mapping
    # already says, and could only ever come back "not listed".
    eight = frozenset({"AP", "JD", "JM", "FG", "SA", "AU", "AG", "LH"})
    codes = _derive_dce_contracts(date(2026, 8, 7), eight)
    assert {code[:2] for code in codes} == {"JD", "JM", "LH"}
    assert len(codes) == 3 * (sources_module.DCE_FORWARD_DELIVERY_MONTHS + 1)


def test_an_unclassified_variety_is_derived_rather_than_guessed_away() -> None:
    # Dropping a variety nobody classified would silently collect nothing for
    # it. Spending the lookups is the safe direction.
    codes = _derive_dce_contracts(date(2026, 8, 7), frozenset({"ZZ"}))
    assert codes and all(code.startswith("ZZ") for code in codes)


def test_every_sina_call_waits_for_the_floor(monkeypatch) -> None:
    slept = []
    monkeypatch.setenv("FUTURES_SINA_MIN_INTERVAL_SECONDS", "0.25")
    monkeypatch.setattr(sources_module.time, "sleep", slept.append)
    # A monotonic clock that never advances, so every call is due to wait.
    monkeypatch.setattr(sources_module.time, "monotonic", lambda: 0.0)

    adapter = AkshareAdapter()
    adapter._pace_sina()
    adapter._pace_sina()
    adapter._pace_sina()
    # The first call has nothing to wait for; each one after it holds the floor.
    assert slept == [0.25, 0.25]


def test_the_floor_is_tunable_because_the_real_limit_is_unknown(monkeypatch) -> None:
    # Verifying the derived list tripped Sina after roughly 120 unpaced
    # requests. The published limit is not documented anywhere, so the default
    # is a guess and has to be adjustable on a box that is already being
    # throttled, without rebuilding an image.
    monkeypatch.delenv("FUTURES_SINA_MIN_INTERVAL_SECONDS", raising=False)
    assert sources_module.sina_min_request_interval_seconds() == 0.5
    monkeypatch.setenv("FUTURES_SINA_MIN_INTERVAL_SECONDS", "2.5")
    assert sources_module.sina_min_request_interval_seconds() == 2.5
    # A typo must not silently disable the floor.
    monkeypatch.setenv("FUTURES_SINA_MIN_INTERVAL_SECONDS", "half a second")
    assert sources_module.sina_min_request_interval_seconds() == 0.5
