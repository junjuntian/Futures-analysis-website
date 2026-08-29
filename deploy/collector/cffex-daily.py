# -*- coding: utf-8 -*-
"""中金所日行情直灌 CSV(DEC-158,2026-08-30 运营者要求加上证50 IH)。

为什么不走新浪:新浪的通用日线端点对中金所合约**结算价恒为 0**(2026-08-30 实测
IH2609/IH2612 整段 s="0.000"),而盯市盈亏按 DEC-073 必须用结算价。akshare 的
`get_cffex_daily` 读的是交易所官网逐日文件,结算价、前结算齐全,与 collector 里
CFFEX 官方源(`akshare_cffex_official`)同源。

输出列与 sina-dce-daily.py 一字不差,装载复用 load-dce-daily.sql(它按
product_instrument_scope 过滤品种,所以这里带回全部中金所品种也只有 IH 进库)。
turnover 故意留空:akshare 转出来的单位没核实过,写错单位比不写更糟。
volume_basis=single:中金所自 2020 起单边计量。
"""

import argparse
import csv
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import akshare

WANT = ("IH",)
COLUMNS = [
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
# 交易所官网文件,一天一个请求;akshare 内部自己会失败重试,这里只按秒歇一下。
PACE = 1.0


def instrument_of(symbol: str) -> str:
    return "".join(c for c in symbol if c.isalpha()).upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--out", default="/opt/futures-platform/load/price_cffex_daily.csv")
    args = parser.parse_args()

    today = datetime.now(UTC).date()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    kept = failed = 0
    with Path(args.out).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for back in range(args.days):
            day = today - timedelta(days=back)
            if day.weekday() >= 5:  # 周末必无文件,请求也是白请求
                continue
            time.sleep(PACE)
            try:
                frame = akshare.get_cffex_daily(date=day.strftime("%Y%m%d"))
            except Exception as error:  # noqa: BLE001 - 节假日没有文件,如实报继续
                print(f"FAIL {day}: {type(error).__name__}", file=sys.stderr, flush=True)
                failed += 1
                continue
            if frame is None or frame.empty:
                continue
            for _, bar in frame.iterrows():
                symbol = str(bar.get("symbol") or "").upper().replace(" ", "")
                if instrument_of(symbol) not in WANT:
                    continue
                writer.writerow([
                    "CFFEX",
                    instrument_of(symbol),
                    symbol,
                    day.isoformat(),
                    bar.get("open") or "",
                    bar.get("high") or "",
                    bar.get("low") or "",
                    bar.get("close") or "",
                    bar.get("settle") or "",
                    bar.get("pre_settle") or "",
                    bar.get("volume") or "",
                    "single",
                    "",
                    bar.get("open_interest") or "",
                    "",
                    "cffex_official",
                ])
                kept += 1
    print(f"CFFEX kept={kept} failed_days={failed}", flush=True)
    return 0 if kept else 1


if __name__ == "__main__":
    raise SystemExit(main())
