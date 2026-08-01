from datetime import date
from types import SimpleNamespace

import pandas as pd

from futures_collector.runner import CollectionRunner


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


class FakePlatform:
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.failed: list[str] = []

    def submit(self, source, dataset_type, collection_date, rows):
        self.submitted.append(f"{source.code}:{dataset_type}:{len(rows)}")
        return SimpleNamespace(import_id="test", inserted=len(rows), skipped=0)

    def record_failure(self, source, dataset_type, collection_date):
        self.failed.append(f"{source.code}:{dataset_type}")
        return "automatic_source_failed"


def test_one_exchange_failure_does_not_stop_another_exchange() -> None:
    platform = FakePlatform()
    failures = CollectionRunner(FakeAdapter(), platform).run(
        date(2026, 8, 1), ["SHFE", "DCE"], ["catalog"]
    )
    assert failures == 1
    assert platform.failed == ["SHFE:futures_catalog_v1"]
    assert platform.submitted == ["DCE:futures_catalog_v1:1"]


def test_explicit_fault_injection_isolates_one_exchange() -> None:
    platform = FakePlatform()
    failures = CollectionRunner(FakeAdapter(), platform).run(
        date(2026, 8, 1),
        ["SHFE", "DCE"],
        ["catalog"],
        injected_failure_exchange="SHFE",
    )
    assert failures == 1
    assert platform.failed == ["SHFE:futures_catalog_v1"]
    assert platform.submitted == ["DCE:futures_catalog_v1:1"]
