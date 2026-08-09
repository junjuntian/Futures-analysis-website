import logging
from datetime import date

import pandas as pd
import pytest

from futures_collector.sources import SOURCES, AkshareAdapter, DatasetCompletenessError


def test_sina_fallback_discovers_dce_contract_market_and_three_seat_ranks(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "futures_collector.sources.akshare.futures_symbol_mark",
        lambda: pd.DataFrame(
            [
                {"exchange": "大连商品交易所", "symbol": "豆一", "mark": "dce_a"},
                {"exchange": "上海期货交易所", "symbol": "铜", "mark": "shfe_cu"},
            ]
        ),
    )
    monkeypatch.setattr(
        "futures_collector.sources.akshare.futures_zh_realtime",
        lambda symbol: pd.DataFrame([{"symbol": "A2609"}, {"symbol": "A0"}]),
    )
    monkeypatch.setattr(
        "futures_collector.sources.akshare.futures_contract_detail",
        lambda symbol: pd.DataFrame(
            [
                {"item": "交易单位", "value": "10吨/手"},
                {"item": "最小变动价位", "value": "1元/吨"},
                {"item": "上市日期", "value": "2025-09-15"},
                {"item": "最后交易日", "value": "2026-09-14"},
            ]
        ),
    )
    monkeypatch.setattr(
        "futures_collector.sources.akshare.futures_zh_daily_sina",
        lambda symbol: pd.DataFrame(
            [
                {"date": "2026-07-30", "close": 3999, "settle": 4000},
                {"date": "2026-07-31", "close": 4001, "settle": 4002},
            ]
        ),
    )

    def seats(symbol, contract, date):
        values = {
            "成交量": ("成交量", 10),
            "多单持仓": ("多单持仓", 20),
            "空单持仓": ("空单持仓", 30),
        }
        column, value = values[symbol]
        return pd.DataFrame([{"名次": 1, "会员简称": "会员甲", column: value}])

    monkeypatch.setattr("futures_collector.sources.akshare.futures_hold_pos_sina", seats)

    adapter = AkshareAdapter()
    collection_date = date(2026, 7, 31)
    catalog = adapter.fallback_catalog(SOURCES["DCE"], collection_date)
    market = adapter.fallback_market(SOURCES["DCE"], collection_date)
    positions = adapter.fallback_seats(SOURCES["DCE"], collection_date)

    assert catalog["合约"].tolist() == ["A2609"]
    assert market[["symbol", "close", "settle"]].to_dict("records") == [
        {"symbol": "A2609", "close": 4001, "settle": 4002}
    ]
    assert set(positions) == {"A2609"}
    assert set(positions["A2609"].columns) >= {
        "vol_party_name",
        "vol",
        "long_party_name",
        "long_open_interest",
        "short_party_name",
        "short_open_interest",
    }


def test_sina_fallback_is_rejected_for_every_non_dce_exchange() -> None:
    adapter = AkshareAdapter()
    for code in ("SHFE", "CZCE", "GFEX", "CFFEX"):
        with pytest.raises(ValueError, match="only authorized for DCE"):
            adapter.fallback_catalog(SOURCES[code], date(2026, 7, 31))


def test_dce_market_rejects_partial_contract_set_after_collecting_skip_count(
    monkeypatch, caplog
) -> None:
    collection_date = date(2026, 7, 31)
    adapter = AkshareAdapter()
    adapter._dce_catalog_cache[(collection_date, None)] = pd.DataFrame(
        [{"合约": "A2609"}, {"合约": "M2609"}]
    )

    def market(symbol):
        if symbol == "M2609":
            raise ConnectionError("injected")
        return pd.DataFrame([{"date": "2026-07-31", "close": 4000, "settle": 3999}])

    monkeypatch.setattr("futures_collector.sources.akshare.futures_zh_daily_sina", market)
    with pytest.raises(DatasetCompletenessError) as captured:
        adapter.fallback_market(SOURCES["DCE"], collection_date)
    assert captured.value.skipped_count == 1
    assert "dataset=market skipped_count=1 expected_contracts=2" in caplog.text


def test_dce_market_excludes_contracts_without_an_observation_for_the_target_date(
    monkeypatch, caplog
) -> None:
    caplog.set_level(logging.INFO)
    collection_date = date(2026, 7, 31)
    adapter = AkshareAdapter()
    adapter._dce_catalog_cache[(collection_date, None)] = pd.DataFrame(
        [{"合约": "A2609"}, {"合约": "M2609"}]
    )

    def market(symbol):
        target_date = "2026-07-31" if symbol == "A2609" else "2026-08-03"
        return pd.DataFrame([{"date": target_date, "close": 4000, "settle": 3999}])

    monkeypatch.setattr("futures_collector.sources.akshare.futures_zh_daily_sina", market)
    result = adapter.fallback_market(SOURCES["DCE"], collection_date)
    assert result["symbol"].tolist() == ["A2609"]
    assert "contract=M2609 collection_date=2026-07-31" in caplog.text


def test_dce_market_rejects_malformed_history_instead_of_treating_it_as_no_observation(
    monkeypatch,
) -> None:
    collection_date = date(2026, 7, 31)
    adapter = AkshareAdapter()
    adapter._dce_catalog_cache[(collection_date, None)] = pd.DataFrame([{"合约": "A2609"}])
    monkeypatch.setattr(
        "futures_collector.sources.akshare.futures_zh_daily_sina",
        lambda symbol: pd.DataFrame([{"date": "not-a-date", "close": 4000}]),
    )
    with pytest.raises(DatasetCompletenessError) as captured:
        adapter.fallback_market(SOURCES["DCE"], collection_date)
    assert captured.value.skipped_count == 1


def test_dce_seats_require_every_contract_and_rank_type(monkeypatch, caplog) -> None:
    collection_date = date(2026, 7, 31)
    adapter = AkshareAdapter()
    adapter._dce_catalog_cache[(collection_date, None)] = pd.DataFrame(
        [{"合约": "A2609"}, {"合约": "M2609"}]
    )

    def seats(symbol, contract, date):
        del date
        if contract == "M2609" and symbol == "空单持仓":
            raise ConnectionError("injected")
        columns = {
            "成交量": ("成交量", 10),
            "多单持仓": ("多单持仓", 20),
            "空单持仓": ("空单持仓", 30),
        }
        column, value = columns[symbol]
        return pd.DataFrame([{"名次": 1, "会员简称": "会员甲", column: value}])

    monkeypatch.setattr("futures_collector.sources.akshare.futures_hold_pos_sina", seats)
    with pytest.raises(DatasetCompletenessError) as captured:
        adapter.fallback_seats(SOURCES["DCE"], collection_date)
    assert captured.value.skipped_count == 1
    assert "dataset=seats skipped_count=1 expected_requests=6" in caplog.text


def test_dce_seats_exclude_contracts_with_no_published_rankings(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)
    collection_date = date(2026, 7, 31)
    adapter = AkshareAdapter()
    adapter._dce_catalog_cache[(collection_date, None)] = pd.DataFrame(
        [{"合约": "A2609"}, {"合约": "M2609"}]
    )

    def seats(symbol, contract, date):
        del date
        if contract == "M2609":
            return pd.DataFrame()
        columns = {
            "成交量": ("成交量", 10),
            "多单持仓": ("多单持仓", 20),
            "空单持仓": ("空单持仓", 30),
        }
        column, value = columns[symbol]
        return pd.DataFrame([{"名次": 1, "会员简称": "会员甲", column: value}])

    monkeypatch.setattr("futures_collector.sources.akshare.futures_hold_pos_sina", seats)
    result = adapter.fallback_seats(SOURCES["DCE"], collection_date)
    assert set(result) == {"A2609"}
    assert "contract=M2609 collection_date=2026-07-31" in caplog.text


def test_dce_seats_reject_partially_published_rank_types(monkeypatch) -> None:
    collection_date = date(2026, 7, 31)
    adapter = AkshareAdapter()
    adapter._dce_catalog_cache[(collection_date, None)] = pd.DataFrame([{"合约": "A2609"}])

    def seats(symbol, contract, date):
        del contract, date
        if symbol == "空单持仓":
            return pd.DataFrame()
        column = {"成交量": "成交量", "多单持仓": "多单持仓"}[symbol]
        return pd.DataFrame([{"名次": 1, "会员简称": "会员甲", column: 10}])

    monkeypatch.setattr("futures_collector.sources.akshare.futures_hold_pos_sina", seats)
    with pytest.raises(DatasetCompletenessError) as captured:
        adapter.fallback_seats(SOURCES["DCE"], collection_date)
    assert captured.value.skipped_count == 1
