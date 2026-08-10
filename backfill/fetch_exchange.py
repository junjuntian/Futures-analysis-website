"""Fetch raw daily files from CZCE and SHFE, exactly as published.

Nothing is parsed, normalised or loaded here. The files land on disk under their
own names so the next step can look at what actually arrived and design one
table from the evidence, rather than discovering mid-load that a column is
missing -- which is what the previous order cost.

Trading days are not guessed: a date that did not trade simply has no file, and
the exchange saying 404 is the same claim as a calendar saying holiday. Those
are recorded as misses and never retried in the same run.

Usage: fetch_exchange.py {czce|shfe} FROM_DATE TO_DATE
"""

import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path("/opt/futures-platform/exchange-raw")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
)
PACE = float(os.environ.get("FETCH_PACE", "2.0"))
# CZCE moved its files on this date; both eras answer, so both are used.
CZCE_ERA = date(2015, 11, 11)


def czce_urls(day: date) -> list[tuple[str, str]]:
    stamp = day.strftime("%Y%m%d")
    year = day.strftime("%Y")
    if day <= CZCE_ERA:
        base = f"https://www.czce.com.cn/cn/exchange/{year}"
        return [
            ("market", f"{base}/datadaily/{stamp}.txt"),
            ("seats", f"{base}/datatradeholding/{stamp}.txt"),
        ]
    base = f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{stamp}"
    return [
        ("market", f"{base}/FutureDataDaily.txt"),
        # .htm at the same path is refused with 412; .txt is served.
        ("seats", f"{base}/FutureDataHolding.txt"),
    ]


def shfe_urls(day: date) -> list[tuple[str, str]]:
    stamp = day.strftime("%Y%m%d")
    base = "https://www.shfe.com.cn/data/tradedata/future/dailydata"
    return [("market", f"{base}/kx{stamp}.dat"), ("seats", f"{base}/pm{stamp}.dat")]


SOURCES = {"czce": (czce_urls, "txt"), "shfe": (shfe_urls, "dat")}


def main() -> int:
    which = sys.argv[1]
    start = date.fromisoformat(sys.argv[2])
    end = date.fromisoformat(sys.argv[3])
    build, suffix = SOURCES[which]
    got = missed = failed = 0
    day = start
    while day <= end:
        if day.weekday() < 5:  # the exchanges publish nothing on weekends
            for kind, url in build(day):
                target = ROOT / which / kind / f"{day.isoformat()}.{suffix}"
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                time.sleep(PACE)
                try:
                    response = requests.get(url, headers={"User-Agent": UA}, timeout=45)
                except Exception as error:  # noqa: BLE001 - reported, then next date
                    failed += 1
                    print(
                        f"FAIL {which} {kind} {day} {type(error).__name__}", flush=True
                    )
                    continue
                if response.status_code == 404:
                    missed += 1
                    continue
                if response.status_code != 200 or not response.content:
                    failed += 1
                    print(
                        f"FAIL {which} {kind} {day} http={response.status_code}",
                        flush=True,
                    )
                    continue
                target.write_bytes(response.content)
                got += 1
        if day.day == 1:
            print(f"  ...{day} 已取 {got} 缺 {missed} 失败 {failed}", flush=True)
        day += timedelta(days=1)
    print(
        f"{which} {start}..{end}: 取到 {got}, 非交易日 {missed}, 失败 {failed}",
        flush=True,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
