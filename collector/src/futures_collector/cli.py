from __future__ import annotations

import argparse
import logging
from datetime import date

from futures_collector.api import PlatformClient, safe_error_code
from futures_collector.config import load_credentials
from futures_collector.runner import CollectionRunner
from futures_collector.sources import SOURCES, AkshareAdapter
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
            "live endpoint serves. The files are downloaded once by hand."
        ),
    )
    value.add_argument(
        "--inject-failure-exchange",
        choices=[*SOURCES],
        help="Acceptance-only source-isolation fault; records failure without network access",
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
    exchanges = list(SOURCES) if args.exchange == "all" else [args.exchange]
    varieties = parse_varieties(args.variety)
    datasets = (
        ["catalog", "calendar", "market", "seats"] if args.dataset == "all" else [args.dataset]
    )
    try:
        credentials = load_credentials()
        with PlatformClient(credentials) as platform:
            failures = CollectionRunner(AkshareAdapter(), platform).run(
                args.date,
                exchanges,
                datasets,
                injected_failure_exchange=args.inject_failure_exchange,
                varieties=varieties,
                history=args.dce_history,
            )
    except Exception as error:
        logging.getLogger("futures_collector").error(
            "collector_start_failed error=%s", safe_error_code(error)
        )
        return 1
    return 0 if failures == 0 else 1
