"""Fetch raw daily files from CZCE, SHFE and INE, exactly as published.

Nothing is parsed, normalised or loaded here. The files land on disk under their
own names so the next step can look at what actually arrived and design one
table from the evidence, rather than discovering mid-load that a column is
missing -- which is what the previous order cost.

Trading days are not guessed: a date that did not trade simply has no file, and
the exchange saying 404 is the same claim as a calendar saying holiday. Those
are recorded as misses and never retried in the same run.

Usage: fetch_exchange.py {czce|shfe|ine} FROM_DATE TO_DATE
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path("/opt/futures-platform/exchange-raw")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
)
PACE = float(os.environ.get("FETCH_PACE", "2.0"))
# 并发只在**显式开**的时候生效,默认 1 —— 逐日顺序、每次隔 PACE,与从前逐字节一致。
# 为什么需要它:INE 要补 2018 年至今约 2200 个交易日,单线程 2 秒一拍要跑十几个小时;
# 开 8 线程约七八分钟。**只对静态文件端点用**(交易所的 .dat/.txt 是 CDN 上的死文件),
# 别拿它去压带查询参数的接口。
WORKERS = max(1, int(os.environ.get("FETCH_WORKERS", "1")))
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


def ine_urls(day: date) -> list[tuple[str, str]]:
    """上期能源(INE)。路径与 SHFE 同构,换个域名而已。

    **只取行情**:能源中心对原油**从不公布逐会员持仓排名**(DEC-158 已实证,
    2026-09-07 复核仍然如此)—— `pm{stamp}.dat` 里只有 lu/nr/bc/ec,
    自 SC 2018-03-26 上市起一天都没有过 sc。所以这里不取 seats:
    取回来的是别的品种,只会污染 SC 的席位口径。
    """
    stamp = day.strftime("%Y%m%d")
    return [("market", f"https://www.ine.cn/data/tradedata/future/dailydata/kx{stamp}.dat")]


SOURCES = {
    "czce": (czce_urls, "txt"),
    "shfe": (shfe_urls, "dat"),
    "ine": (ine_urls, "dat"),
}


def fetch_one(which: str, suffix: str, kind: str, url: str, day: date) -> str:
    """取一个文件。回 "got" / "missed" / "failed",调用方只管计数。"""
    target = ROOT / which / kind / f"{day.isoformat()}.{suffix}"
    if target.exists():
        return "skip"
    target.parent.mkdir(parents=True, exist_ok=True)
    time.sleep(PACE)
    try:
        response = requests.get(url, headers={"User-Agent": UA}, timeout=45)
    except Exception as error:  # noqa: BLE001 - reported, then next date
        print(f"FAIL {which} {kind} {day} {type(error).__name__}", flush=True)
        return "failed"
    if response.status_code == 404:
        return "missed"
    if response.status_code != 200 or not response.content:
        print(f"FAIL {which} {kind} {day} http={response.status_code}", flush=True)
        return "failed"
    target.write_bytes(response.content)
    return "got"


def main() -> int:
    which = sys.argv[1]
    start = date.fromisoformat(sys.argv[2])
    end = date.fromisoformat(sys.argv[3])
    build, suffix = SOURCES[which]
    tally = {"got": 0, "missed": 0, "failed": 0, "skip": 0}

    jobs = []
    day = start
    while day <= end:
        if day.weekday() < 5:  # the exchanges publish nothing on weekends
            for kind, url in build(day):
                jobs.append((kind, url, day))
        day += timedelta(days=1)

    if WORKERS == 1:
        for kind, url, when in jobs:
            tally[fetch_one(which, suffix, kind, url, when)] += 1
            if when.day == 1 and kind == jobs[0][0]:
                print(
                    f"  ...{when} 已取 {tally['got']} 缺 {tally['missed']} "
                    f"失败 {tally['failed']}",
                    flush=True,
                )
    else:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [
                pool.submit(fetch_one, which, suffix, kind, url, when)
                for kind, url, when in jobs
            ]
            for done, future in enumerate(as_completed(futures), 1):
                tally[future.result()] += 1
                if done % 200 == 0:
                    print(
                        f"  ...{done}/{len(jobs)} 已取 {tally['got']} "
                        f"缺 {tally['missed']} 失败 {tally['failed']}",
                        flush=True,
                    )
    got, missed, failed = tally["got"], tally["missed"], tally["failed"]
    print(
        f"{which} {start}..{end}: 取到 {got}, 非交易日 {missed}, 失败 {failed}",
        flush=True,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
