# -*- coding: utf-8 -*-
"""上海国际能源中心 SC 原油日行情直灌 CSV(DEC-158,2026-08-30 运营者要求)。

**SC 只有行情没有席位**:能源中心对原油不公布逐会员持仓排名(东财 2026-08-28
全市场 1.5 万行持仓榜里 INE 只有 nr/lu/bc,自 SC 上市起就没发过),所以 SC 进不了
席位页 —— 它在平台上的消费方是套利分析页(品种下拉、自由价差、K 线)。

行情走新浪通用日线端点(与 sina-dce-daily.py 同一个),SC 的结算价实测有真值
(2026-08-28 SC2610 结算 592.3)。合约清单按月推:SC 挂牌连续月,近 2 个月到
未来 36 个月逐个试,新浪对不存在的合约回空串,空响应不算失败。
输出列与 sina-dce-daily.py 一字不差,装载复用 load-dce-daily.sql
(product_instrument_scope 过滤,只有 SC 进库)。
"""

import argparse
import csv
import json
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
)
SINA_ENDPOINT = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
    "var%20_{sym}=/InnerFuturesNewService.getDailyKLine?symbol={sym}"
)
HEADERS = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}
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
PACE = 2.0  # 与 sina-dce-daily.py 同:新浪 456 拒过 120 次无间隔请求


def candidate_contracts():
    """SC 近 2 个月到未来 36 个月的月合约。不存在的合约新浪回空,试了不亏。"""
    now = datetime.now(UTC).date()
    out = []
    for offset in range(-2, 37):
        month = now.month + offset
        year = now.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        out.append(f"SC{year % 100:02d}{month:02d}")
    return out


def daily(symbol):
    response = requests.get(SINA_ENDPOINT.format(sym=symbol), headers=HEADERS, timeout=40)
    response.raise_for_status()
    body = response.text
    payload = body[body.find("=") + 1 :].strip().rstrip(";")
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        # JSONP 外层是圆括号,strip 掉等号后剩 "([...])";与 sina-dce-daily.py
        # 同一个兜底:正则把方括号段抠出来。漏了这层就是整段静默空数据。
        found = re.search(r"\[(.*)\]", body, re.DOTALL)
        return json.loads("[" + found.group(1) + "]") if found else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--out", default="/opt/futures-platform/load/price_ine_daily.csv")
    args = parser.parse_args()

    since = (datetime.now(UTC).date() - timedelta(days=args.days)).isoformat()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    kept = failed = 0
    with Path(args.out).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for symbol in candidate_contracts():
            time.sleep(PACE)
            try:
                bars = daily(symbol)
            except Exception as error:  # noqa: BLE001 - 报出来,继续下一个合约
                print(f"FAIL {symbol}: {type(error).__name__}", file=sys.stderr, flush=True)
                failed += 1
                continue
            for bar in bars or []:
                day = str(bar.get("d") or "")
                if day < since:
                    continue
                close = bar.get("c")
                volume = bar.get("v")
                # 无成交日的收盘价是 0,进库会画出砸到零的假 K 线(sina-dce-daily
                # 的 usable 同一条纪律,这里只留有效行)。
                if not close or float(close) <= 0 or not volume or float(volume) <= 0:
                    continue
                writer.writerow([
                    "INE",
                    "SC",
                    symbol,
                    day,
                    bar.get("o") or "",
                    bar.get("h") or "",
                    bar.get("l") or "",
                    close,
                    bar.get("s") or "",
                    "",
                    volume,
                    "single",
                    "",
                    bar.get("p") or "",
                    "",
                    "sina",
                ])
                kept += 1
    print(f"INE kept={kept} failed={failed}", flush=True)
    return 0 if kept else 1


if __name__ == "__main__":
    raise SystemExit(main())
