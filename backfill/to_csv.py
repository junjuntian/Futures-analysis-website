"""Turn the raw files into two CSVs shaped like the two tables.

CSV then `\\copy` rather than a driver: it needs nothing installed on the host,
and for millions of rows it is the faster path anyway. The load into the real
tables is done by SQL that upserts from a staging copy, so a re-run corrects
rows instead of duplicating them.
"""

import argparse
import csv
import glob
import sys
from pathlib import Path

sys.path.insert(0, "/opt/futures-platform")
import parsers

RAW = Path("/opt/futures-platform/exchange-raw")
SANHE = Path("/opt/futures-platform/sanhe-seats/raw")
OUT = Path("/opt/futures-platform/load")

PRICE_COLUMNS = [
    "exchange",
    "instrument",
    "contract",
    "trade_date",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "settlement_price",
    "prev_settlement_price",
    "volume",
    "volume_basis",
    "turnover",
    "open_interest",
    "open_interest_change",
    "source",
]
SEAT_COLUMNS = [
    "exchange",
    "instrument",
    "contract",
    "is_variety_total",
    "variety_total_is_computed",
    "trade_date",
    "rank_type",
    "rank",
    "member",
    "quantity",
    "change",
    "source",
]


def price_row(r):
    return [
        r["exchange"],
        r["instrument"],
        r["contract"],
        r["trade_date"],
        r.get("open", ""),
        r.get("high", ""),
        r.get("low", ""),
        r.get("close", ""),
        r.get("settlement", ""),
        r.get("prev_settlement", ""),
        r.get("volume", ""),
        r["volume_basis"],
        r.get("turnover", ""),
        r.get("open_interest", ""),
        r.get("open_interest_change", ""),
        r["source"],
    ]


def seat_row(r):
    return [
        r["exchange"],
        r["instrument"],
        r.get("contract") or "",
        "true" if r["is_variety_total"] else "false",
        "true" if r.get("variety_total_is_computed") else "false",
        r["trade_date"],
        r["rank_type"],
        r.get("rank", ""),
        r["member"],
        r["quantity"],
        r.get("change", ""),
        r["source"],
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--what", required=True, choices=["czce", "shfe", "sanhe", "all"])
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    price_path = OUT / f"price_{args.what}.csv"
    seat_path = OUT / f"seat_{args.what}.csv"
    prices = seats = parsed = failed = 0

    with (
        price_path.open("w", newline="", encoding="utf-8") as pf,
        seat_path.open("w", newline="", encoding="utf-8") as sf,
    ):
        pw, sw = csv.writer(pf), csv.writer(sf)
        pw.writerow(PRICE_COLUMNS)
        sw.writerow(SEAT_COLUMNS)

        jobs = []
        if args.what in ("czce", "all"):
            jobs += [
                ("czce/market", parsers.czce_market, pw, price_row, "*.txt"),
                ("czce/seats", parsers.czce_seats, sw, seat_row, "*.txt"),
            ]
        if args.what in ("shfe", "all"):
            jobs += [
                ("shfe/market", parsers.shfe_market, pw, price_row, "*.dat"),
                ("shfe/seats", parsers.shfe_seats, sw, seat_row, "*.dat"),
            ]

        for sub, fn, writer, shape, pattern in jobs:
            files = sorted(glob.glob(str(RAW / sub / pattern)))
            if args.limit:
                files = files[: args.limit]
            for path in files:
                try:
                    rows = fn(path)
                except Exception as error:  # noqa: BLE001 - named, then next file
                    failed += 1
                    print(
                        f"PARSE_FAIL {path}: {type(error).__name__}: {error}",
                        flush=True,
                    )
                    continue
                parsed += 1
                for r in rows:
                    writer.writerow(shape(r))
                    if writer is pw:
                        prices += 1
                    else:
                        seats += 1
            print(f"  {sub}: {len(files)} 个文件", flush=True)

        if args.what in ("sanhe", "all"):
            files = sorted(glob.glob(str(SANHE / "*" / "*.json")))
            if args.limit:
                files = files[: args.limit]
            for path in files:
                try:
                    rows = parsers.sanhe_seats(path)
                except Exception as error:  # noqa: BLE001
                    failed += 1
                    print(
                        f"PARSE_FAIL {path}: {type(error).__name__}: {error}",
                        flush=True,
                    )
                    continue
                parsed += 1
                for r in rows:
                    sw.writerow(seat_row(r))
                    seats += 1
            print(f"  sanhe: {len(files)} 个文件", flush=True)

    print(f"\n解析 {parsed} 个文件，失败 {failed}")
    print(f"价格 {prices} 行 -> {price_path}")
    print(f"席位 {seats} 行 -> {seat_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
