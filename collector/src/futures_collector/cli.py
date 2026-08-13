from __future__ import annotations

import argparse
import logging
from datetime import date

from futures_collector.errors import safe_error_code
from futures_collector.csv_sink import CsvSink
from futures_collector.runner import CollectionRunner
from futures_collector.sources import DEFAULT_EXCHANGES, SOURCES, AkshareAdapter
from futures_collector.trading_calendar import latest_trading_date


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="futures-collector")
    date_mode = value.add_mutually_exclusive_group(required=True)
    date_mode.add_argument("--date", type=date.fromisoformat)
    date_mode.add_argument("--resolve-date", type=date.fromisoformat, metavar="AS_OF")
    value.add_argument("--exchange", default="all", choices=["all", *SOURCES])
    value.add_argument(
        "--dataset",
        default="all",
        choices=["all", "catalog", "calendar", "market", "seats"],
    )
    value.add_argument(
        "--variety",
        default="all",
        help=(
            "Comma-separated variety symbols to collect, e.g. JM,JD,LH. "
            "Default 'all' collects every variety the exchange publishes."
        ),
    )
    value.add_argument(
        "--dce-history",
        action="store_true",
        help=(
            "Read DCE from the exchange's annual history files in "
            "$FUTURES_DCE_HISTORY_DIR instead of the network, for the years no "
            "live endpoint serves. The files are fetched once and read from disk."
        ),
    )
    value.add_argument(
        "--through",
        type=date.fromisoformat,
        metavar="END_DATE",
        help=(
            "With --date and --dce-history, import every trading date from "
            "--date through END_DATE inclusive, in one process. Only the file "
            "source accepts a range: it reads from disk, so there is no upstream "
            "to pace, and each annual file is parsed once instead of once per "
            "date. Live sources stay one date per invocation."
        ),
    )
    value.add_argument(
        "--inject-failure-exchange",
        choices=[*SOURCES],
        help="Acceptance-only source-isolation fault; records failure without network access",
    )
    value.add_argument(
        "--emit-csv",
        metavar="DIR",
        help=(
            "Write each dataset to a CSV in DIR. This is the only output: the "
            "loader SQL then copies those files straight into the wide tables the "
            "pages read. It used to be optional, with the default going through the "
            "audited import channel -- that channel was removed on 2026-08-13 "
            "(DEC-049), so leaving the flag off would now POST to endpoints that "
            "return 404."
        ),
    )
    return value


def parse_varieties(value: str) -> frozenset[str] | None:
    """`all` means no narrowing; anything else is a set of variety symbols.

    An explicitly empty selection is rejected rather than silently collecting
    nothing, which would submit empty batches and read as a source failure.
    """
    if value.strip().lower() == "all":
        return None
    symbols = frozenset(part.strip().upper() for part in value.split(",") if part.strip())
    if not symbols:
        raise ValueError("--variety selected no symbols")
    return symbols


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.resolve_date is not None:
        try:
            print(latest_trading_date(args.resolve_date).isoformat())
        except ValueError as error:
            logging.getLogger("futures_collector").error(
                "schedule_date_failed error=%s", safe_error_code(error)
            )
            return 1
        return 0
    log_ = logging.getLogger("futures_collector")
    if not args.emit_csv:
        # 不能用 argparse 的 required:那会把 --resolve-date 一起挡掉,而那条路
        # 只查交易日历、不产出任何数据。
        log_.error("collector_start_failed error=emit_csv_is_required")
        return 1
    # `all` 指的是「默认要采的那些」,不是「代码里定义过的全部」——见
    # sources.DEFAULT_EXCHANGES 的说明。显式指定某一家时仍可采任意一家。
    exchanges = list(DEFAULT_EXCHANGES) if args.exchange == "all" else [args.exchange]
    varieties = parse_varieties(args.variety)
    datasets = (
        ["catalog", "calendar", "market", "seats"] if args.dataset == "all" else [args.dataset]
    )
    log = log_
    if args.through is not None and not args.dce_history:
        # A range against a live source would hammer an exchange with no pacing
        # between dates. `run-backfill.sh` is what paces those.
        log.error("collector_start_failed error=range_requires_dce_history")
        return 1
    if args.through is not None and args.through < args.date:
        log.error("collector_start_failed error=range_ends_before_it_starts")
        return 1
    try:
        adapter = AkshareAdapter()
        dates = (
            [args.date]
            if args.through is None
            else adapter.dce_history_trading_dates(varieties, args.date, args.through)
        )
        if not dates:
            log.error("collector_start_failed error=no_trading_dates_in_range")
            return 1
        sink = CsvSink(args.emit_csv)
        failures = 0
        with sink as platform:
            runner = CollectionRunner(adapter, platform)
            for index, day in enumerate(dates, start=1):
                day_failures = runner.run(
                    day,
                    exchanges,
                    datasets,
                    injected_failure_exchange=args.inject_failure_exchange,
                    varieties=varieties,
                    history=args.dce_history,
                )
                failures += day_failures
                if args.through is not None:
                    # One line per date, because a range runs for a long time and
                    # a silent process gives no way to tell progress from a hang.
                    log.info(
                        "range_date_done date=%s index=%d of=%d failures=%d total_failures=%d",
                        day.isoformat(),
                        index,
                        len(dates),
                        day_failures,
                        failures,
                    )
    except Exception as error:
        log.error("collector_start_failed error=%s", safe_error_code(error))
        return 1
    return 0 if failures == 0 else 1
