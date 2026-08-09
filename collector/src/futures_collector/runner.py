from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime

from futures_collector.api import PlatformClient, safe_error_code
from futures_collector.normalize import (
    normalize_calendar,
    normalize_catalog,
    normalize_market,
    normalize_seats,
)
from futures_collector.sources import (
    DCE_FALLBACK_SOURCE,
    EASTMONEY_SEATS_SOURCE_CODE,
    SOURCES,
    AkshareAdapter,
    DatasetCompletenessError,
    ExchangeSource,
    eastmoney_seats_source,
)

LOG = logging.getLogger("futures_collector")


class CollectionRunner:
    def __init__(
        self,
        adapter: AkshareAdapter,
        platform: PlatformClient,
        *,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.adapter = adapter
        self.platform = platform
        self.retry_delay_seconds = retry_delay_seconds

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
        for dataset in datasets:
            dataset_type = _dataset_type(dataset)
            effective_source = source
            observed_at = datetime.now(UTC)
            try:
                rows = self._collect_with_retries(
                    source,
                    collection_date,
                    dataset,
                    observed_at,
                    fallback=False,
                )
            except Exception as error:
                LOG.error(
                    "dataset_failed exchange=%s dataset=%s error=%s",
                    source.code,
                    dataset_type,
                    safe_error_code(error),
                )
                reason = self.platform.record_failure(source, dataset_type, collection_date)
                LOG.error(
                    "batch_failed exchange=%s dataset=%s reason=%s",
                    source.code,
                    dataset_type,
                    reason,
                )
                rows = None
                for candidate, candidate_is_fallback in _fallback_chain(source, dataset):
                    LOG.warning(
                        "fallback_activated exchange=%s dataset=%s source=%s",
                        source.code,
                        dataset_type,
                        candidate.source_code,
                    )
                    try:
                        rows = self._collect_with_retries(
                            candidate,
                            collection_date,
                            dataset,
                            observed_at,
                            fallback=candidate_is_fallback,
                        )
                    except Exception as fallback_error:
                        LOG.error(
                            "fallback_failed exchange=%s dataset=%s source=%s error=%s",
                            source.code,
                            dataset_type,
                            candidate.source_code,
                            safe_error_code(fallback_error),
                        )
                        skipped_count = (
                            fallback_error.skipped_count
                            if isinstance(fallback_error, DatasetCompletenessError)
                            else 0
                        )
                        fallback_reason = self.platform.record_failure(
                            candidate,
                            dataset_type,
                            collection_date,
                            skipped_source_item_count=skipped_count,
                        )
                        LOG.error(
                            "batch_failed exchange=%s dataset=%s source=%s reason=%s",
                            source.code,
                            dataset_type,
                            candidate.source_code,
                            fallback_reason,
                        )
                        rows = None
                        continue
                    effective_source = candidate
                    break
                if rows is None:
                    failures += 1
                    continue
            LOG.info(
                "dataset_collected exchange=%s dataset=%s source=%s rows=%d",
                effective_source.code,
                dataset_type,
                effective_source.source_code,
                len(rows),
            )
            try:
                result = self.platform.submit(effective_source, dataset_type, collection_date, rows)
            except Exception as error:
                LOG.error(
                    "dataset_submit_failed exchange=%s dataset=%s source=%s error=%s",
                    effective_source.code,
                    dataset_type,
                    effective_source.source_code,
                    safe_error_code(error),
                )
                failures += 1
                continue
            LOG.info(
                "batch_succeeded exchange=%s dataset=%s source=%s import_id=%s "
                "rows=%d inserted=%d skipped=%d",
                effective_source.code,
                dataset_type,
                effective_source.source_code,
                result.import_id,
                len(rows),
                result.inserted,
                result.skipped,
            )
        return failures

    def _collect_with_retries(
        self,
        source: ExchangeSource,
        collection_date: date,
        dataset: str,
        observed_at: datetime,
        *,
        fallback: bool,
    ) -> list[dict[str, str]]:
        max_attempts = 1 if source.code == "DCE" and not fallback else 3
        for attempt in range(1, max_attempts + 1):
            try:
                return self._collect(
                    source,
                    collection_date,
                    dataset,
                    observed_at,
                    fallback=fallback,
                )
            except Exception as error:
                if attempt == max_attempts:
                    raise
                LOG.warning(
                    "dataset_retry exchange=%s dataset=%s source=%s attempt=%d "
                    "max_attempts=%d error=%s",
                    source.code,
                    _dataset_type(dataset),
                    source.source_code,
                    attempt,
                    max_attempts,
                    safe_error_code(error),
                )
                if self.retry_delay_seconds > 0:
                    time.sleep(self.retry_delay_seconds * attempt)
        raise AssertionError("retry loop exhausted without returning or raising")

    def _collect(
        self,
        source: ExchangeSource,
        collection_date: date,
        dataset: str,
        observed_at: datetime,
        *,
        fallback: bool,
    ) -> list[dict[str, str]]:
        if source.source_code == EASTMONEY_SEATS_SOURCE_CODE and dataset != "seats":
            # Eastmoney's report carries no settlement price and no contract
            # catalog, so it must never stand in for anything but seats.
            raise ValueError("the Eastmoney source only serves the seats dataset")
        if dataset == "catalog":
            frame = (
                self.adapter.fallback_catalog(source, collection_date)
                if fallback
                else self.adapter.catalog(source, collection_date)
            )
            return normalize_catalog(source, collection_date, frame)
        if dataset == "market":
            frame = (
                self.adapter.fallback_market(source, collection_date)
                if fallback
                else self.adapter.market(source, collection_date)
            )
            return normalize_market(source, collection_date, frame, observed_at)
        if dataset == "calendar":
            frame = (
                self.adapter.fallback_market(source, collection_date)
                if fallback
                else self.adapter.market(source, collection_date)
            )
            normalize_market(source, collection_date, frame, observed_at)
            return normalize_calendar(source, collection_date)
        if dataset == "seats":
            if source.source_code == EASTMONEY_SEATS_SOURCE_CODE:
                tables = self.adapter.eastmoney_seats(source, collection_date)
            elif fallback:
                tables = self.adapter.fallback_seats(source, collection_date)
            else:
                tables = self.adapter.seats(source, collection_date)
            return normalize_seats(source, collection_date, tables)
        raise ValueError("unsupported dataset")


def _fallback_chain(source: ExchangeSource, dataset: str) -> list[tuple[ExchangeSource, bool]]:
    """Sources to try, in order, once the official one has failed.

    Official first is the standing rule, so nothing here ever pre-empts an
    exchange that is answering. DCE keeps its existing Sina fallback for every
    dataset. Eastmoney is appended for seats only, and only last: it is the one
    source that covers all five exchanges, so it turns a hard seat failure into
    an attempt where previously there was none.
    """
    chain: list[tuple[ExchangeSource, bool]] = []
    if source.code == "DCE":
        chain.append((DCE_FALLBACK_SOURCE, True))
    if dataset == "seats":
        chain.append((eastmoney_seats_source(source.code), False))
    return chain


def _dataset_type(dataset: str) -> str:
    return {
        "catalog": "futures_catalog_v1",
        "calendar": "trading_calendar_v1",
        "market": "daily_market_prices_v1",
        "seats": "seat_positions_v1",
    }[dataset]
