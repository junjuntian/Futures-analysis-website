from __future__ import annotations

import argparse
import logging
from datetime import date

from futures_collector.api import PlatformClient, safe_error_code
from futures_collector.config import load_credentials
from futures_collector.runner import CollectionRunner
from futures_collector.sources import SOURCES, AkshareAdapter


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="futures-collector")
    value.add_argument("--date", required=True, type=date.fromisoformat)
    value.add_argument("--exchange", default="all", choices=["all", *SOURCES])
    value.add_argument(
        "--dataset",
        default="all",
        choices=["all", "catalog", "calendar", "market", "seats"],
    )
    value.add_argument(
        "--inject-failure-exchange",
        choices=[*SOURCES],
        help="Acceptance-only source-isolation fault; records failure without network access",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    exchanges = list(SOURCES) if args.exchange == "all" else [args.exchange]
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
            )
    except Exception as error:
        logging.getLogger("futures_collector").error(
            "collector_start_failed error=%s", safe_error_code(error)
        )
        return 1
    return 0 if failures == 0 else 1
