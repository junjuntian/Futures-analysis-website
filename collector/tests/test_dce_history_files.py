from datetime import UTC, date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from futures_collector import sources as sources_module
from futures_collector.runner import CollectionRunner
from futures_collector.sources import (
    DCE_HISTORY_SOURCE,
    DCE_HISTORY_SOURCE_CODE,
    SOURCES,
    AkshareAdapter,
)

COLUMNS = [
    "商品名称",
    "合约名称",
    "交易日期",
    "开盘价",
    "最高价",
    "最低价",
    "收盘价",
    "前结算价",
    "结算价",
]


def annual_file(path, rows):
    pd.DataFrame(rows, columns=COLUMNS).to_excel(path, index=False)


def make_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FUTURES_DCE_HISTORY_DIR", str(tmp_path))
    return tmp_path


def jm_rows():
    return [
        ["焦煤", "jm1509", "20150901", "700", "710", "690", "705", "702", "703"],
        ["焦煤", "jm1601", "20150901", "690", "700", "685", "695", "693", "694"],
        # A different trading day in the same file, which must not leak into
        # another day's batch.
        ["焦煤", "jm1509", "20150902", "705", "715", "700", "712", "703", "710"],
    ]


def runner(varieties=frozenset({"JM"})):
    made = CollectionRunner(AkshareAdapter(), SimpleNamespace(), retry_delay_seconds=0)
    made._varieties = varieties
    return made


def test_market_rows_carry_the_settlement_price_the_aggregators_lack(tmp_path, monkeypatch):
    # The whole reason for using the exchange's own files: Sina keeps no
    # delisted contract this old and Eastmoney keeps none at all, and the one
    # aggregator that does reach back has no settlement price.
    root = make_dir(tmp_path, monkeypatch)
    annual_file(root / "jm_2015.xlsx", jm_rows())
    rows = runner()._collect(
        DCE_HISTORY_SOURCE, date(2015, 9, 1), "market", datetime.now(UTC), fallback=False
    )
    assert [(r["contract_code"], r["close_price"], r["settlement_price"]) for r in rows] == [
        ("JM1509", "705", "703"),
        ("JM1601", "695", "694"),
    ]


def test_a_catalog_lists_each_contract_once_not_once_per_day(tmp_path, monkeypatch):
    # The annual file is one row per contract per trading day. Feeding it whole
    # to the catalog normalizer would repeat every contract once for every day
    # of the year.
    root = make_dir(tmp_path, monkeypatch)
    annual_file(root / "jm_2015.xlsx", jm_rows())
    rows = runner()._collect(
        DCE_HISTORY_SOURCE, date(2015, 9, 1), "catalog", datetime.now(UTC), fallback=False
    )
    assert sorted(r["contract_code"] for r in rows) == ["JM1509", "JM1601"]


def test_a_day_the_exchange_published_nothing_is_refused(tmp_path, monkeypatch):
    # A holiday or a weekend. Emitting an empty batch would assert the day
    # traded and had no prices, which is a different and false claim.
    root = make_dir(tmp_path, monkeypatch)
    annual_file(root / "jm_2015.xlsx", jm_rows())
    with pytest.raises(ValueError, match="no rows for the requested date"):
        runner()._collect(
            DCE_HISTORY_SOURCE, date(2015, 10, 3), "market", datetime.now(UTC), fallback=False
        )


def test_a_missing_file_names_what_is_missing(tmp_path, monkeypatch):
    make_dir(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="jm_2015.xlsx"):
        runner()._collect(
            DCE_HISTORY_SOURCE, date(2015, 9, 1), "market", datetime.now(UTC), fallback=False
        )


def test_the_history_source_never_claims_to_carry_seats(tmp_path, monkeypatch):
    # Seats for these years are a separate archive. Serving an empty seat batch
    # from a price file would look like the exchange published no rankings.
    root = make_dir(tmp_path, monkeypatch)
    annual_file(root / "jm_2015.xlsx", jm_rows())
    with pytest.raises(ValueError, match="no seat rankings"):
        runner()._collect(
            DCE_HISTORY_SOURCE, date(2015, 9, 1), "seats", datetime.now(UTC), fallback=False
        )


def test_the_source_opens_no_socket() -> None:
    # It reads files fetched once through the operator's browser at their
    # instruction, not by this code. An empty allowlist means the
    # outbound guard would reject any host, so a future edit that tried to fetch
    # from here fails loudly instead of quietly bypassing the exchange's WAF.
    assert DCE_HISTORY_SOURCE.domains == frozenset()
    assert DCE_HISTORY_SOURCE.source_code == DCE_HISTORY_SOURCE_CODE
    assert DCE_HISTORY_SOURCE.code == "DCE"


def test_history_mode_replaces_dce_and_leaves_the_other_exchanges_alone(tmp_path, monkeypatch):
    root = make_dir(tmp_path, monkeypatch)
    annual_file(root / "jm_2015.xlsx", jm_rows())
    submitted = []

    class Platform:
        def submit(self, source, dataset_type, collection_date, rows):
            submitted.append((source.source_code, dataset_type, len(rows)))
            return SimpleNamespace(import_id="t", inserted=len(rows), skipped=0)

        def record_failure(self, source, dataset_type, collection_date, **kwargs):
            submitted.append((source.source_code, dataset_type, "FAILED"))
            return "automatic_source_failed"

    made = CollectionRunner(AkshareAdapter(), Platform(), retry_delay_seconds=0)
    made.run(date(2015, 9, 1), ["DCE"], ["market"], varieties=frozenset({"JM"}), history=True)
    assert submitted == [(DCE_HISTORY_SOURCE_CODE, "daily_market_prices_v1", 2)]

    # Without the flag the live source is used, exactly as before.
    assert SOURCES["DCE"].source_code == "akshare_dce_official"


def test_a_year_is_parsed_once_and_shared_across_dates(tmp_path, monkeypatch):
    root = make_dir(tmp_path, monkeypatch)
    annual_file(root / "jm_2015.xlsx", jm_rows())
    reads = []
    real = sources_module.pd.read_excel

    def counting(path, **kwargs):
        reads.append(str(path))
        return real(path, **kwargs)

    monkeypatch.setattr(sources_module.pd, "read_excel", counting)
    adapter = AkshareAdapter()
    adapter.dce_history_frame(date(2015, 9, 1), frozenset({"JM"}))
    adapter.dce_history_frame(date(2015, 9, 2), frozenset({"JM"}))
    assert len(reads) == 1
