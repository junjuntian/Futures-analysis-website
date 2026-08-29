# -*- coding: utf-8 -*-
"""中金所 IH 全量历史回填(DEC-158 追加,2026-08-30 运营者:先把 IH 采到上市起,
后续要做 IH 的跟随策略)。

读交易所官网逐日文件(akshare get_cffex_rank_table / get_cffex_daily,与日更
同源同口径),按**年**分片写两个 CSV:
  - seats-IH-<year>.csv  11 列,与 collector 的 SEAT_FIELDS 逐一对应
    (装载走 load-seats-direct.sql,expect_date 传批内最新交易日 —— 那道守卫
    校验的是 max(trade_date),防装错文件,不挡多日批量);
  - price-IH-<year>.csv  16 列,与 sina-dce-daily.py 同构(装载走 load-dce-daily.sql)。

跑法(collector 容器内,按年跑,断了重跑当年那片):
  python cffex_backfill.py --year 2015 --out-dir /tmp/load
IH 2015-04-16 上市;当年从上市日起。节假日官网无文件,akshare 抛错,按日跳过。
"""

import argparse
import csv
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import akshare

LISTED = date(2015, 4, 16)  # IH 上市日
PACE = 1.0

SEAT_COLUMNS = [
    "exchange_code", "contract_code", "trade_date", "seat_name", "rank_type",
    "rank", "volume", "long_position", "short_position", "change",
    "source_record_ref",
]
PRICE_COLUMNS = [
    "exchange", "instrument", "contract", "trade_date", "open_price",
    "high_price", "low_price", "close_price", "settlement_price",
    "prev_settlement_price", "volume", "volume_basis", "turnover",
    "open_interest", "open_interest_change", "source",
]


def instrument_of(symbol: str) -> str:
    return "".join(c for c in symbol if c.isalpha()).upper()


def seat_rows(day: date):
    tables = akshare.get_cffex_rank_table(date=day.strftime("%Y%m%d"))
    if not isinstance(tables, dict):
        return
    for key, frame in tables.items():
        contract = str(key).upper().replace(" ", "")
        if instrument_of(contract) != "IH" or frame is None or frame.empty:
            continue
        for _, raw in frame.iterrows():
            rank = raw.get("rank")
            if rank is None:
                continue
            for rank_type, name_f, value_f, change_f in (
                ("volume", "vol_party_name", "vol", "vol_chg"),
                ("long", "long_party_name", "long_open_interest", "long_open_interest_chg"),
                ("short", "short_party_name", "short_open_interest", "short_open_interest_chg"),
            ):
                name = str(raw.get(name_f) or "").strip()
                value = raw.get(value_f)
                if not name or name == "nan" or value is None:
                    continue
                yield [
                    "CFFEX", contract, day.isoformat(), name, rank_type,
                    int(rank), value, value if rank_type == "long" else "",
                    value if rank_type == "short" else "",
                    raw.get(change_f) if raw.get(change_f) is not None else "",
                    "",
                ]


def price_rows(day: date):
    frame = akshare.get_cffex_daily(date=day.strftime("%Y%m%d"))
    if frame is None or frame.empty:
        return
    for _, bar in frame.iterrows():
        symbol = str(bar.get("symbol") or "").upper().replace(" ", "")
        if instrument_of(symbol) != "IH":
            continue
        yield [
            "CFFEX", "IH", symbol, day.isoformat(),
            bar.get("open") or "", bar.get("high") or "", bar.get("low") or "",
            bar.get("close") or "", bar.get("settle") or "", bar.get("pre_settle") or "",
            bar.get("volume") or "", "single", "",
            bar.get("open_interest") or "", "", "cffex_official",
        ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--out-dir", default="/tmp/load")
    args = parser.parse_args()

    start = max(date(args.year, 1, 1), LISTED)
    end = min(date(args.year, 12, 31), date.today())
    if start > end:
        print(f"{args.year}: 无可采区间", flush=True)
        return 1
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    seats_path = out / f"seats-IH-{args.year}.csv"
    price_path = out / f"price-IH-{args.year}.csv"

    seat_kept = price_kept = skipped = 0
    with seats_path.open("w", newline="", encoding="utf-8") as sh, \
         price_path.open("w", newline="", encoding="utf-8") as ph:
        sw, pw = csv.writer(sh), csv.writer(ph)
        sw.writerow(SEAT_COLUMNS)
        pw.writerow(PRICE_COLUMNS)
        day = start
        while day <= end:
            if day.weekday() < 5:
                time.sleep(PACE)
                try:
                    for row in seat_rows(day):
                        sw.writerow(row)
                        seat_kept += 1
                    for row in price_rows(day):
                        pw.writerow(row)
                        price_kept += 1
                except Exception as error:  # noqa: BLE001 - 节假日无文件,逐日跳过
                    skipped += 1
                    if skipped <= 5 or skipped % 20 == 0:
                        print(f"SKIP {day}: {type(error).__name__}", file=sys.stderr, flush=True)
            day += timedelta(days=1)
    print(f"{args.year}: seats={seat_kept} price={price_kept} skipped_days={skipped}", flush=True)
    return 0 if seat_kept or price_kept else 1


if __name__ == "__main__":
    raise SystemExit(main())
