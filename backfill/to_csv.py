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
    # 每日增量:只解析文件名日期 >= SINCE 的原始文件(文件名即 YYYYMMDD 戳)。
    # 全量回填不带此参,行为不变。
    ap.add_argument("--since", default="")
    # 下面三个都只为「按需取一部分」而加，全部留空时行为与从前一字不差。
    #
    # --sanhe-dir:三禾原始目录。日更那份只存三个大商所品种(`raw`)，2026-08-30 起
    #   另起 `raw-all` 存全部品种全部会员——同一套解析器，两个目录。
    # --want:只要这几个品种。**必要而非方便**:raw-all 里躺着 73 个品种，其中
    #   金银/苹果/玻璃/纯碱在库里已有交易所官方席位(带名次、带增减)。三禾那份
    #   不带名次，两份同日共存会让不做来源去重的下游把同一个会员算两遍。
    # --until:只要这一天**之前**的。用来和已经在库的日更区间对齐，做到零重叠。
    ap.add_argument("--sanhe-dir", default="")
    ap.add_argument("--want", default="")
    ap.add_argument("--until", default="")
    args = ap.parse_args()
    since_stamp = args.since.replace("-", "")
    want = {v.strip().upper() for v in args.want.split(",") if v.strip()}
    OUT.mkdir(parents=True, exist_ok=True)

    def keep(r):
        if want and r["instrument"] not in want:
            return False
        if args.until and r["trade_date"] >= args.until:
            return False
        return True

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
            if since_stamp:
                files = [f for f in files
                         if "".join(ch for ch in Path(f).stem if ch.isdigit()) >= since_stamp]
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
                    if not keep(r):
                        continue
                    writer.writerow(shape(r))
                    if writer is pw:
                        prices += 1
                    else:
                        seats += 1
            print(f"  {sub}: {len(files)} 个文件", flush=True)

        if args.what in ("sanhe", "all"):
            sanhe_dir = Path(args.sanhe_dir) if args.sanhe_dir else SANHE
            files = sorted(glob.glob(str(sanhe_dir / "*" / "*.json")))
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
                    if not keep(r):
                        continue
                    sw.writerow(seat_row(r))
                    seats += 1
            print(f"  sanhe: {len(files)} 个文件（{sanhe_dir}）", flush=True)

    print(f"\n解析 {parsed} 个文件，失败 {failed}")
    print(f"价格 {prices} 行 -> {price_path}")
    print(f"席位 {seats} 行 -> {seat_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
