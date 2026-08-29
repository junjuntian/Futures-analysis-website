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


def test_dce_market_and_calendar_are_skipped_not_attempted() -> None:
    # Operator decision 2026-08-13: push2his has never answered from the
    # production VPS, so live DCE prices/calendar are not collected at all —
    # no attempt, no retry wall, no failure record. Sina fills the prices
    # outside this collector. A skip must not count as a failure, or every
    # cron run would report a broken day that is in fact the designed state.
    platform = FakePlatform()
    failures = CollectionRunner(FakeAdapter(), platform, retry_delay_seconds=0).run(
        date(2026, 8, 1), ["DCE"], ["market", "calendar"]
    )
    assert failures == 0
    assert platform.failed == []
    assert platform.submitted == []


def test_dce_catalog_and_seats_are_still_collected() -> None:
    # The skip is two datasets, not the exchange: push2delay (catalog) and
    # datacenter-web (seats) do answer, and seats carry the change column the
    # signal engine lives on. Skipping too much would be silent data loss.
    platform = FakePlatform()
    failures = CollectionRunner(FakeAdapter(), platform, retry_delay_seconds=0).run(
        date(2026, 8, 1), ["DCE"], ["catalog"]
    )
    assert failures == 0
    assert platform.submitted == ["eastmoney_dce_market:futures_catalog_v1:1"]


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


# (Two retired tests lived here: live-DCE-market retry behaviour and the
#  incomplete-day failure. Both described a dataset that is no longer
#  attempted live — operator decision 2026-08-13, see the runner skip.)


def test_the_default_exchange_set_carries_only_exchanges_with_live_varieties() -> None:
    # 八个品种全在上期所(AU/AG)、郑商所(AP/FG/SA)、大商所(JD/JM/LH)。
    # 中金所与广期所一个都没有:2026-08-13 查生产,它们采了三个月、进 canonical
    # 1590 行,而页面读的宽表里是 0 行——采回来当场被品种范围过滤掉,只留下
    # 99,044 行导入中间产物。
    #
    # 这条守的是「别不小心加回去」。真要做那两家的品种,先有品种再加交易所,
    # 顺序反了就是又白采三个月。
    from futures_collector.sources import DEFAULT_EXCHANGES, SOURCES

    # 2026-08-30 起 CFFEX 回到默认集(DEC-158):上证50 IH 立项,「先有品种再加
    # 交易所」的顺序这次是对的。INE 有意不在:SC 原油只有行情没有席位排名,
    # 行情走 ine-daily.py 直灌,不经 collector。
    assert set(DEFAULT_EXCHANGES) == {"DCE", "SHFE", "CZCE", "CFFEX"}
    # 仍然定义着,`--exchange GFEX` 可用——停的是默认采集,不是采集能力。
    assert {"GFEX", "CFFEX"} <= set(SOURCES)
