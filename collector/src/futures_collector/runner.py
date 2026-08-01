from __future__ import annotations

import logging
from datetime import date

from futures_collector.api import PlatformClient
from futures_collector.normalize import (
    normalize_calendar,
    normalize_catalog,
    normalize_market,
    normalize_seats,
)
from futures_collector.sources import SOURCES, AkshareAdapter, ExchangeSource

LOG = logging.getLogger("futures_collector")


class CollectionRunner:
    def __init__(self, adapter: AkshareAdapter, platform: PlatformClient) -> None:
        self.adapter = adapter
        self.platform = platform

    def run(
        self,
        collection_date: date,
        exchanges: list[str],
        datasets: list[str],
        injected_failure_exchange: str | None = None,
    ) -> int:
        failures = 0
        for code in exchanges:
            source = SOURCES[code]
            try:
                if code == injected_failure_exchange:
                    raise ConnectionError("injected source isolation failure")
                failures += self._run_exchange(source, collection_date, datasets)
            except Exception as error:  # source isolation is the top-level contract
                LOG.error("exchange_failed exchange=%s error=%s", code, type(error).__name__)
                for dataset in datasets:
                    dataset_type = _dataset_type(dataset)
                    reason = self.platform.record_failure(source, dataset_type, collection_date)
                    LOG.error(
                        "batch_failed exchange=%s dataset=%s reason=%s",
                        code,
                        dataset_type,
                        reason,
                    )
                failures += len(datasets)
        return failures

    def _run_exchange(
        self, source: ExchangeSource, collection_date: date, datasets: list[str]
    ) -> int:
        failures = 0
        market_rows: list[dict[str, str]] | None = None
        for dataset in datasets:
            dataset_type = _dataset_type(dataset)
            try:
                if dataset == "catalog":
                    rows = normalize_catalog(
                        source, collection_date, self.adapter.catalog(source, collection_date)
                    )
                elif dataset == "market":
                    market_rows = normalize_market(
                        source, collection_date, self.adapter.market(source, collection_date)
                    )
                    rows = market_rows
                elif dataset == "calendar":
                    if market_rows is None:
                        market_rows = normalize_market(
                            source, collection_date, self.adapter.market(source, collection_date)
                        )
                    rows = normalize_calendar(source, collection_date)
                elif dataset == "seats":
                    rows = normalize_seats(
                        source, collection_date, self.adapter.seats(source, collection_date)
                    )
                else:
                    raise ValueError("unsupported dataset")
                result = self.platform.submit(source, dataset_type, collection_date, rows)
                LOG.info(
                    "batch_succeeded exchange=%s dataset=%s import_id=%s "
                    "rows=%d inserted=%d skipped=%d",
                    source.code,
                    dataset_type,
                    result.import_id,
                    len(rows),
                    result.inserted,
                    result.skipped,
                )
            except Exception as error:
                LOG.error(
                    "dataset_failed exchange=%s dataset=%s error=%s",
                    source.code,
                    dataset_type,
                    type(error).__name__,
                )
                reason = self.platform.record_failure(source, dataset_type, collection_date)
                LOG.error(
                    "batch_failed exchange=%s dataset=%s reason=%s",
                    source.code,
                    dataset_type,
                    reason,
                )
                failures += 1
        return failures


def _dataset_type(dataset: str) -> str:
    return {
        "catalog": "futures_catalog_v1",
        "calendar": "trading_calendar_v1",
        "market": "daily_market_prices_v1",
        "seats": "seat_positions_v1",
    }[dataset]
