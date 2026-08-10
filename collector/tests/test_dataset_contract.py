"""The collector's columns and the platform's dataset fields are one contract.

They live in two languages and two crates, and nothing connected them. The daily
range showed what that costs: the collector emitted 开高低量额 for weeks, the
platform's dataset rules never declared them, and the ingestion dropped every
one at the door. Nothing failed -- the imports reported success and the rows
landed with a close and a settlement and nothing else -- so it stayed invisible
until a day's prices were compared field by field against the exchange's file.
"""

import re
from pathlib import Path

import pytest

from futures_collector.normalize import DATASET_FIELDS

DOMAIN_IMPORT_RS = (
    Path(__file__).resolve().parents[2] / "rust" / "crates" / "domain" / "src" / "import.rs"
)
# The platform's dataset name, and the Rust constant that declares its columns.
DATASET_CONSTANTS = {
    "futures_catalog_v1": "CATALOG_DATASET_FIELDS",
    "trading_calendar_v1": "CALENDAR_DATASET_FIELDS",
    "daily_market_prices_v1": "MARKET_DATASET_FIELDS",
    "seat_positions_v1": "SEAT_DATASET_FIELDS",
}


def declared_fields(constant: str) -> list[str]:
    source = DOMAIN_IMPORT_RS.read_text(encoding="utf-8")
    start = source.index(f"const {constant}: &[DatasetFieldRule] = dataset_fields! {{")
    body = source[start : source.index("};", start)]
    return re.findall(r'"([a-z_]+)"\s*=>', body)


@pytest.mark.parametrize(("dataset", "constant"), sorted(DATASET_CONSTANTS.items()))
def test_the_collector_and_the_platform_agree_on_every_column(dataset, constant) -> None:
    assert DOMAIN_IMPORT_RS.is_file(), f"{DOMAIN_IMPORT_RS} moved; this contract needs re-pointing"
    platform = declared_fields(constant)
    collector = list(DATASET_FIELDS[dataset])
    missing = [field for field in collector if field not in platform]
    extra = [field for field in platform if field not in collector]
    assert not missing, (
        f"{dataset}: the collector sends {missing} and the platform does not declare them, "
        "so ingestion drops them at the door and the rows land incomplete"
    )
    assert not extra, (
        f"{dataset}: the platform declares {extra} and the collector never sends them, "
        "so they would be silently null on every automatic row"
    )
