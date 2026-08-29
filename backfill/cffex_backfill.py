# -*- coding: utf-8 -*-
"""中金所 IH 全量历史回填(DEC-158 追加,2026-08-30 运营者:先把 IH 采到上市起,
要做 IH 的跟随策略;并要求找官网打包文件,别逐日重请求跑太久触发风控)。

**两条直链,都在生产 VPS 实测过(2015 与 2026 格式一致)**:
  - 席位: http://www.cffex.com.cn/sj/ccpm/YYYYMM/DD/IH_1.csv
    一天一个 ~3KB 文件 = 当天 IH 全部合约三榜逐会员(前面还有一段「会员类别」
    汇总表,**akshare 同一个 URL**——之前只看文件头误判老年份没有逐会员段)。
  - 行情: http://www.cffex.com.cn/sj/historysj/YYYYMM/zip/YYYYMM.zip
    一月一个 zip,内含逐日全品种行情(开高低收/结算/前结算/持仓/持仓变化)。

相比 akshare 逐日拉全市场:请求减半、单请求从整市场缩到 3KB,总时长 ~35 分钟。
输出与装载约定同前:席位 11 列走 load-seats-direct.sql(expect_date=批内最新日),
行情 16 列走 load-dce-daily.sql。按年分片,断了重跑当年。

跑法: python cffex_backfill.py --year 2015 --out-dir /tmp/load
"""

import argparse
import csv
import io
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import requests

# 生产 VPS 实测:cffex.com.cn 的 AAAA 记录是黑洞,requests 每个请求先等 IPv6
# 连接超时(~12 秒)才回落 IPv4——curl 有并行探测所以只要 0.7 秒。一天一个请求
# 的回填被拖成 9 小时。强制只解析 IPv4,回到 0.7 秒/请求。
import socket
_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, *args, **kwargs):
    return _getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)


socket.getaddrinfo = _ipv4_only

LISTED = date(2015, 4, 16)  # IH 上市日
PACE = 0.6
SEAT_URL = "http://www.cffex.com.cn/sj/ccpm/{ym}/{dd}/IH_1.csv"
ZIP_URL = "http://www.cffex.com.cn/sj/historysj/{ym}/zip/{ym}.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

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


def fetch(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    if response.status_code != 200:
        return None
    return response.content


def seat_rows(day: date):
    """解析当日 IH_1.csv 的**排名段**(跳过开头的会员类别汇总段)。"""
    raw = fetch(SEAT_URL.format(ym=day.strftime("%Y%m"), dd=day.strftime("%d")))
    if raw is None:
        return
    text = raw.decode("gbk", "ignore")
    if "排名" not in text:
        return  # 302 落到错误页之类,不是数据
    in_rank = False
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) >= 3 and cells[2] == "排名":
            in_rank = True
            continue
        if not in_rank or len(cells) < 12 or not cells[0].isdigit():
            continue
        d = f"{cells[0][:4]}-{cells[0][4:6]}-{cells[0][6:8]}"
        contract = cells[1].upper()
        rank = cells[2]
        for rank_type, name, value, change in (
            ("volume", cells[3], cells[4], cells[5]),
            ("long", cells[6], cells[7], cells[8]),
            ("short", cells[9], cells[10], cells[11]),
        ):
            if not name or not value:
                continue
            yield [
                "CFFEX", contract, d, name, rank_type, rank, value,
                value if rank_type == "long" else "",
                value if rank_type == "short" else "",
                change or "", "",
            ]


def price_rows_for_month(ym: str):
    """月度 zip → 逐日全品种行情,只留 IH。"""
    raw = fetch(ZIP_URL.format(ym=ym))
    if raw is None:
        return
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        for member in sorted(bundle.namelist()):
            stem = Path(member).stem  # YYYYMMDD_1
            day = stem.split("_")[0]
            if len(day) != 8 or not day.isdigit():
                continue
            d = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
            text = bundle.read(member).decode("gbk", "ignore")
            for line in text.splitlines()[1:]:
                cells = [c.strip() for c in line.split(",")]
                if len(cells) < 11 or not cells[0].upper().startswith("IH"):
                    continue
                contract = cells[0].upper()
                # 列:合约,开,高,低,量,额,持仓,持仓变化,收,结算,前结算
                yield [
                    "CFFEX", "IH", contract, d,
                    cells[1], cells[2], cells[3], cells[8],
                    cells[9], cells[10], cells[4], "single", "",
                    cells[6], cells[7], "cffex_official",
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

    seat_kept = price_kept = missed = 0
    with (out / f"seats-IH-{args.year}.csv").open("w", newline="", encoding="utf-8") as sh:
        sw = csv.writer(sh)
        sw.writerow(SEAT_COLUMNS)
        day = start
        while day <= end:
            if day.weekday() < 5:
                time.sleep(PACE)
                try:
                    n = 0
                    for row in seat_rows(day):
                        sw.writerow(row)
                        n += 1
                    seat_kept += n
                    if n == 0:
                        missed += 1  # 节假日或缺文件,如实计数
                except Exception as error:  # noqa: BLE001
                    missed += 1
                    print(f"SEAT_FAIL {day}: {type(error).__name__}", file=sys.stderr, flush=True)
            day += timedelta(days=1)

    months = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        months.append(cursor.strftime("%Y%m"))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    with (out / f"price-IH-{args.year}.csv").open("w", newline="", encoding="utf-8") as ph:
        pw = csv.writer(ph)
        pw.writerow(PRICE_COLUMNS)
        for ym in months:
            time.sleep(PACE)
            try:
                for row in price_rows_for_month(ym):
                    pw.writerow(row)
                    price_kept += 1
            except Exception as error:  # noqa: BLE001
                print(f"PRICE_FAIL {ym}: {type(error).__name__}", file=sys.stderr, flush=True)

    print(f"{args.year}: seats={seat_kept} price={price_kept} empty_days={missed}", flush=True)
    return 0 if seat_kept or price_kept else 1


if __name__ == "__main__":
    raise SystemExit(main())
