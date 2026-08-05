from datetime import date
from types import SimpleNamespace

import pandas as pd

from futures_collector.runner import CollectionRunner
from futures_collector.sources import DatasetCompletenessError


class FakeAdapter:
    def catalog(self, source, collection_date):
        if source.code == "SHFE":
            raise ConnectionError("injected")
        return pd.DataFrame(
            [
                {
                    "品种名称": "豆一",
                    "合约": "a2609",
                    "交易单位": 10,
                    "最小变动价位": 1,
                    "开始交易日": "2025-09-15",
                    "最后交易日": "2026-09-14",
                }
            ]
        )

    def fallback_catalog(self, source, collection_date):
        raise AssertionError("fallback must not run for a successful official source")


class FakePlatform:
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.failed: list[str] = []
        self.failure_skips: list[int] = []

    def submit(self, source, dataset_type, collection_date, rows):
        self.submitted.append(f"{source.source_code}:{dataset_type}:{len(rows)}")
        return SimpleNamespace(import_id="test", inserted=len(rows), skipped=0)

    def record_failure(self, source, dataset_type, collection_date, *, skipped_source_item_count=0):
        self.failed.append(f"{source.code}:{dataset_type}")
        self.failure_skips.append(skipped_source_item_count)
        return "automatic_source_failed"


def test_one_exchange_failure_does_not_stop_another_exchange() -> None:
    platform = FakePlatform()
    failures = CollectionRunner(FakeAdapter(), platform, retry_delay_seconds=0).run(
        date(2026, 8, 1), ["SHFE", "DCE"], ["catalog"]
    )
    assert failures == 1
    assert platform.failed == ["SHFE:futures_catalog_v1"]
    assert platform.submitted == ["akshare_dce_official:futures_catalog_v1:1"]


def test_explicit_fault_injection_isolates_one_exchange() -> None:
    platform = FakePlatform()
    failures = CollectionRunner(FakeAdapter(), platform, retry_delay_seconds=0).run(
        date(2026, 8, 1),
        ["SHFE", "DCE"],
        ["catalog"],
        injected_failure_exchange="SHFE",
    )
    assert failures == 1
    assert platform.failed == ["SHFE:futures_catalog_v1"]
    assert platform.submitted == ["akshare_dce_official:futures_catalog_v1:1"]


class DceFallbackAdapter(FakeAdapter):
    def catalog(self, source, collection_date):
        raise ValueError("official response was not JSON")

    def fallback_catalog(self, source, collection_date):
        return pd.DataFrame(
            [
                {
                    "品种名称": "豆一",
                    "合约": "a2609",
                    "交易单位": 10,
                    "最小变动价位": 1,
                }
            ]
        )


def test_dce_official_failure_is_audited_before_sina_fallback_succeeds() -> None:
    platform = FakePlatform()
    failures = CollectionRunner(DceFallbackAdapter(), platform, retry_delay_seconds=0).run(
        date(2026, 8, 1), ["DCE"], ["catalog"]
    )
    assert failures == 0
    assert platform.failed == ["DCE:futures_catalog_v1"]
    assert platform.submitted == ["akshare_sina_dce_fallback:futures_catalog_v1:1"]


class TransientGfexAdapter(FakeAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def catalog(self, source, collection_date):
        self.calls += 1
        if self.calls < 3:
            raise ValueError("transient empty response")
        return super().catalog(source, collection_date)


def test_non_dce_official_source_recovers_from_transient_response() -> None:
    adapter = TransientGfexAdapter()
    platform = FakePlatform()
    failures = CollectionRunner(adapter, platform, retry_delay_seconds=0).run(
        date(2026, 8, 1), ["GFEX"], ["catalog"]
    )
    assert failures == 0
    assert adapter.calls == 3
    assert platform.failed == []
    assert platform.submitted == ["akshare_gfex_official:futures_catalog_v1:1"]


class IncompleteDceAdapter(FakeAdapter):
    def market(self, source, collection_date):
        raise ConnectionError("official unavailable")

    def fallback_market(self, source, collection_date):
        raise DatasetCompletenessError("market", 2, 10)


def test_dce_incomplete_fallback_fails_whole_dataset_and_audits_skip_count() -> None:
    platform = FakePlatform()
    failures = CollectionRunner(IncompleteDceAdapter(), platform, retry_delay_seconds=0).run(
        date(2026, 8, 1), ["DCE"], ["market"]
    )
    assert failures == 1
    assert platform.failed == [
        "DCE:daily_market_prices_v1",
        "DCE:daily_market_prices_v1",
    ]
    assert platform.failure_skips == [0, 2]
    assert platform.submitted == []
