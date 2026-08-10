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

    def eastmoney_dce_catalog(self, collection_date, varieties=None):
        return pd.DataFrame([{"品种名称": "豆一", "合约": "a2609"}])

    def eastmoney_dce_market(self, collection_date, varieties=None):
        return pd.DataFrame(
            [
                {
                    "symbol": "A2609",
                    "date": collection_date.isoformat(),
                    "open": "4100",
                    "high": "4130",
                    "low": "4090",
                    "close": "4120",
                    "settle": "4115",
                    "volume": "1000",
                    "turnover": "41150000",
                }
            ]
        )


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
    assert platform.submitted == ["eastmoney_dce_market:futures_catalog_v1:1"]


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
    assert platform.submitted == ["eastmoney_dce_market:futures_catalog_v1:1"]


def test_dce_prices_are_filed_under_the_quote_source_not_an_exchange_source() -> None:
    # DEC-045: DCE's prices come from Eastmoney now. The batch has to say so.
    # Filing an aggregator's numbers under the exchange's own source code would
    # make a second-hand reading indistinguishable from the exchange speaking.
    platform = FakePlatform()
    failures = CollectionRunner(FakeAdapter(), platform, retry_delay_seconds=0).run(
        date(2026, 8, 1), ["DCE"], ["market"]
    )
    assert failures == 0
    assert platform.failed == []
    assert platform.submitted == ["eastmoney_dce_market:daily_market_prices_v1:1"]


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


class TransientDceAdapter(FakeAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def eastmoney_dce_market(self, collection_date, varieties=None):
        self.calls += 1
        if self.calls < 3:
            raise ValueError("transient empty response")
        return super().eastmoney_dce_market(collection_date, varieties)


def test_dce_now_retries_like_every_other_exchange() -> None:
    # It used to get a single attempt, because its official endpoint answered
    # 412 to everything and retrying only delayed the fallback. There is no
    # official endpoint in the chain any more, so a transient failure deserves
    # the same three attempts everything else gets.
    adapter = TransientDceAdapter()
    platform = FakePlatform()
    failures = CollectionRunner(adapter, platform, retry_delay_seconds=0).run(
        date(2026, 8, 1), ["DCE"], ["market"]
    )
    assert failures == 0
    assert adapter.calls == 3
    assert platform.failed == []


class IncompleteDceAdapter(FakeAdapter):
    def eastmoney_dce_market(self, collection_date, varieties=None):
        raise DatasetCompletenessError("market", 10, 10)


def test_an_incomplete_dce_day_fails_the_dataset_and_audits_the_skip_count() -> None:
    # Nothing stands behind the quote source for prices, so an incomplete day is
    # the end of it. The skip count still has to reach the audit record: a day
    # that failed for being incomplete has to be distinguishable later from one
    # that failed because nothing answered.
    platform = FakePlatform()
    failures = CollectionRunner(IncompleteDceAdapter(), platform, retry_delay_seconds=0).run(
        date(2026, 8, 1), ["DCE"], ["market"]
    )
    assert failures == 1
    assert platform.failed == ["DCE:daily_market_prices_v1"]
    assert platform.submitted == []
