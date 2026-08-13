from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime

from futures_collector.api import PlatformClient, safe_error_code
from futures_collector.normalize import (
    filter_rows_by_variety,
    normalize_calendar,
    normalize_catalog,
    normalize_market,
    normalize_seats,
)
from futures_collector.sources import (
    DCE_HISTORY_SOURCE,
    DCE_HISTORY_SOURCE_CODE,
    EASTMONEY_DCE_SOURCE_CODE,
    EASTMONEY_SEATS_SOURCE_CODE,
    SOURCES,
    AkshareAdapter,
    DatasetCompletenessError,
    ExchangeSource,
    eastmoney_seats_source,
)

LOG = logging.getLogger("futures_collector")


class EmptyAfterVarietyFilter(Exception):
    """Raised when a response held nothing for the requested varieties.

    This is an ordinary outcome when an exchange lists none of them, so the
    runner records it and moves on instead of counting it as a failure or
    firing the fallback chain at a source that answered perfectly well.
    """


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
        # None means "every variety". An empty set would mean "none at all",
        # which is never what a caller wants and would submit empty batches.
        self._varieties: frozenset[str] | None = None
        # Read DCE from the exchange's own annual files instead of the network.
        self._history = False

    def run(
        self,
        collection_date: date,
        exchanges: list[str],
        datasets: list[str],
        injected_failure_exchange: str | None = None,
        varieties: frozenset[str] | None = None,
        history: bool = False,
    ) -> int:
        self._varieties = varieties
        self._history = history
        failures = 0
        for code in exchanges:
            # History mode replaces DCE outright: for those years no live
            # endpoint answers, so there is nothing to fall back from.
            source = DCE_HISTORY_SOURCE if (self._history and code == "DCE") else SOURCES[code]
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
            # DCE prices and calendar are not collected live (operator decision
            # 2026-08-13). Both need push2his, and that endpoint has never once
            # answered from the production VPS — the log has zero successful DCE
            # market batches, only three-attempt retry walls burning minutes on
            # every cron run and every live acceptance pass. DCE daily prices
            # come from the Sina loader outside this collector; the catalog
            # (push2delay) and seats (datacenter-web) endpoints do answer and
            # stay collected. History mode is untouched: the annual files never
            # open a socket.
            if source.source_code == EASTMONEY_DCE_SOURCE_CODE and dataset in (
                "market",
                "calendar",
            ):
                LOG.info(
                    "dataset_skipped_dead_endpoint exchange=%s dataset=%s "
                    "reason=push2his_unreachable_from_vps",
                    source.code,
                    dataset_type,
                )
                continue
            # DCE's quote source carries prices, not rankings, so for seats the
            # seat report is the primary rather than a fallback. Going through
            # the fallback path would first record a failed attempt that was
            # never made.
            primary = (
                eastmoney_seats_source(source.code)
                if dataset == "seats" and source.source_code == EASTMONEY_DCE_SOURCE_CODE
                else source
            )
            effective_source = primary
            observed_at = datetime.now(UTC)
            try:
                rows = self._collect_with_retries(
                    primary,
                    collection_date,
                    dataset,
                    observed_at,
                )
            except EmptyAfterVarietyFilter:
                LOG.info(
                    "dataset_skipped_no_requested_variety exchange=%s dataset=%s",
                    primary.code,
                    dataset_type,
                )
                continue
            except Exception as error:
                LOG.error(
                    "dataset_failed exchange=%s dataset=%s error=%s",
                    primary.code,
                    dataset_type,
                    safe_error_code(error),
                )
                reason = self.platform.record_failure(primary, dataset_type, collection_date)
                LOG.error(
                    "batch_failed exchange=%s dataset=%s reason=%s",
                    primary.code,
                    dataset_type,
                    reason,
                )
                rows = None
                for candidate in _fallback_chain(primary, dataset):
                    LOG.warning(
                        "fallback_activated exchange=%s dataset=%s source=%s",
                        primary.code,
                        dataset_type,
                        candidate.source_code,
                    )
                    try:
                        rows = self._collect_with_retries(
                            candidate,
                            collection_date,
                            dataset,
                            observed_at,
                        )
                    except Exception as fallback_error:
                        LOG.error(
                            "fallback_failed exchange=%s dataset=%s source=%s error=%s",
                            primary.code,
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
                            primary.code,
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
    ) -> list[dict[str, str]]:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                return self._collect(
                    source,
                    collection_date,
                    dataset,
                    observed_at,
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
    ) -> list[dict[str, str]]:
        if source.source_code == EASTMONEY_SEATS_SOURCE_CODE and dataset != "seats":
            # The seat report carries no settlement price and no contract
            # catalog, so it must never stand in for anything but seats.
            raise ValueError("the Eastmoney seat source only serves the seats dataset")
        varieties = self._varieties
        if source.source_code == EASTMONEY_DCE_SOURCE_CODE:
            if dataset == "seats":
                # Seats come from a different Eastmoney report with a source
                # record of its own, so this source never answers for them.
                raise ValueError("the Eastmoney DCE quote source carries no seat rankings")
            if dataset == "catalog":
                frame = self.adapter.eastmoney_dce_catalog(collection_date, varieties)
                return self._narrow(normalize_catalog(source, collection_date, frame))
            frame = self.adapter.eastmoney_dce_market(collection_date, varieties)
            if dataset == "market":
                return self._narrow(normalize_market(source, collection_date, frame, observed_at))
            if dataset == "calendar":
                # Proven the same way as for every other source: rows exist for
                # this date, so it was a trading day.
                normalize_market(source, collection_date, frame, observed_at)
                return normalize_calendar(source, collection_date)
            raise ValueError("unsupported dataset")
        if source.source_code == DCE_HISTORY_SOURCE_CODE:
            # The annual files carry the prices and the contracts that traded,
            # and nothing else. Seats for those years live in the exchange's
            # daily ranking archives, which are a separate download.
            if dataset == "seats":
                raise ValueError("the DCE history source carries no seat rankings")
            frame = self.adapter.dce_history_frame(collection_date, varieties)
            if dataset == "catalog":
                # The annual file is one row per contract per trading day, so a
                # catalog built from it whole would repeat every contract once
                # per day of the year. The catalog for a date is the distinct
                # contracts the exchange listed that day.
                day = frame[frame["交易日期"].astype(str) == collection_date.strftime("%Y%m%d")]
                day = day.drop_duplicates(subset=["合约"])
                return self._narrow(normalize_catalog(source, collection_date, day))
            if dataset == "market":
                return self._narrow(normalize_market(source, collection_date, frame, observed_at))
            if dataset == "calendar":
                # Proven the same way as for every other source: the exchange
                # published rows for this date, so it was a trading day.
                normalize_market(source, collection_date, frame, observed_at)
                return normalize_calendar(source, collection_date)
            raise ValueError("unsupported dataset")
        if dataset == "catalog":
            frame = self.adapter.catalog(source, collection_date)
            return self._narrow(normalize_catalog(source, collection_date, frame))
        if dataset == "market":
            frame = self.adapter.market(source, collection_date)
            return self._narrow(normalize_market(source, collection_date, frame, observed_at))
        if dataset == "calendar":
            frame = self.adapter.market(source, collection_date)
            # The calendar is proven by the exchange answering for the date at
            # all, so it is normalized from the unnarrowed response and never
            # filtered: a trading day belongs to every variety.
            normalize_market(source, collection_date, frame, observed_at)
            return normalize_calendar(source, collection_date)
        if dataset == "seats":
            if source.source_code == EASTMONEY_SEATS_SOURCE_CODE:
                tables = self.adapter.eastmoney_seats(source, collection_date, varieties)
            else:
                tables = self.adapter.seats(source, collection_date)
            return self._narrow(normalize_seats(source, collection_date, tables))
        raise ValueError("unsupported dataset")

    def _narrow(self, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        narrowed = filter_rows_by_variety(rows, self._varieties)
        if not narrowed:
            # An exchange that carries none of the requested varieties is not a
            # failure, but an empty batch would look like one downstream.
            raise EmptyAfterVarietyFilter("no rows left after the variety filter")
        return narrowed


def _fallback_chain(source: ExchangeSource, dataset: str) -> list[ExchangeSource]:
    """Sources to try, in order, once the primary one has failed.

    Only seats have one. DCE used to keep a Sina fallback for every dataset;
    `DEC-045` removed it, because Sina had no history at all for 105 of the 186
    contracts DCE listed on 2026-08-07 and so could never complete a day.

    The Eastmoney seat report is the one source covering all five exchanges, so
    for seats it turns what would be a hard failure into one more attempt. It is
    never tried ahead of a source that is answering.
    """
    if dataset == "seats" and source.source_code != EASTMONEY_SEATS_SOURCE_CODE:
        return [eastmoney_seats_source(source.code)]
    return []


def _dataset_type(dataset: str) -> str:
    return {
        "catalog": "futures_catalog_v1",
        "calendar": "trading_calendar_v1",
        "market": "daily_market_prices_v1",
        "seats": "seat_positions_v1",
    }[dataset]
