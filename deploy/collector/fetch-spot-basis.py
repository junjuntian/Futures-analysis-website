#!/usr/bin/env python3
"""现货价与基差采集(DEC-074)。生意社数据,经 akshare `futures_spot_price`。

写出 CSV 给 `load-spot-basis.sql` 装载。跑在 collector 镜像里(它自带 akshare)。

用法:
    python fetch-spot-basis.py --out /tmp/load/spot_basis.csv [--date 20260818]
                               [--since 20260801]   # 回看窗,逐日补齐

设计取舍:
- **不进 collector 的 ExchangeSource 契约**:生意社不是交易所,一次返回全品种、
  不按交易所分,硬塞进那套抽象要为它开一堆例外。照 `run-official-seats.sh` 的
  先例做独立脚本——但**脚本与 cron 都进仓库、随发布包下发**(旧教训:服务器上
  手工装的脚本换机器会丢)。
- **只取监控的六个品种**:其余 40 多个品种当前没人看,存了也是死数据。
  要扩就改 WANT。
- **失败不写空文件**:采集失败只留 `.failed` 标记。写一个只有表头的空 CSV 会被
  装载脚本当成「今天一行都没有」照单全收(采集侧的老教训,run-collector.sh
  里有同款注释)。
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta

import akshare as ak

# 监控中的六个品种。AP 在源里全程没有现货报价(2026-08-18 探针:2018 年至今
# 一天都没有),留在列表里无害——取不到就不写行,不编。
WANT = {"JD", "JM", "LH", "AP", "FG", "SA"}

FIELDS = [
    "trade_date",
    "instrument",
    "spot_price",
    "near_contract",
    "near_price",
    "near_basis",
    "near_basis_rate",
    "dominant_contract",
    "dominant_price",
    "dominant_basis",
    "dominant_basis_rate",
]


def normalize_contract(raw: str) -> str:
    """郑商所在源里是三位年月(FG609),库里是四位(FG2609)。

    三位年月只给了年份个位,补世纪必须**按交易日锚定**而不是按品种上市年——
    后者正是郑商所世纪判错事故的根因(PITFALLS 四)。这里的取法:源返回的
    合约必然是当前挂牌的,年份个位对应「今年或未来 9 年内」,取使
    `2000+YY >= 当前年 - 1` 的最小解。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    head = "".join(ch for ch in raw if ch.isalpha()).upper()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 4:
        return f"{head}{digits}"
    if len(digits) == 3:
        this_year = date.today().year
        decade, unit = divmod(this_year % 100, 10)
        yy = decade * 10 + int(digits[0])
        if yy < this_year % 100 - 1:  # 个位小于今年 → 下一个十年
            yy += 10
        return f"{head}{yy:02d}{digits[1:]}"
    return f"{head}{digits}"


def num(value) -> str:
    """空值写空串,由装载侧转 NULL。0 视为「没有报价」,不是价格(DEC-073)。"""
    try:
        if value is None:
            return ""
        text = str(value).strip()
        if text in {"", "nan", "None", "--"}:
            return ""
        parsed = float(text)
        if parsed != parsed:  # NaN
            return ""
        return text
    except (TypeError, ValueError):
        return ""


def fetch_day(day: date) -> list[dict[str, str]]:
    frame = ak.futures_spot_price(day.strftime("%Y%m%d"))
    if frame is None or frame.empty or "symbol" not in frame.columns:
        return []  # 非交易日:akshare 自己会 warn,这里静默返回
    rows: list[dict[str, str]] = []
    for _, row in frame.iterrows():
        instrument = str(row.get("symbol", "")).strip().upper()
        if instrument not in WANT:
            continue
        spot = num(row.get("spot_price"))
        if not spot or float(spot) <= 0:
            continue  # 没有现货报价的品种直接不写行
        rows.append(
            {
                "trade_date": day.isoformat(),
                "instrument": instrument,
                "spot_price": spot,
                "near_contract": normalize_contract(str(row.get("near_contract", ""))),
                "near_price": num(row.get("near_contract_price")),
                "near_basis": num(row.get("near_basis")),
                "near_basis_rate": num(row.get("near_basis_rate")),
                "dominant_contract": normalize_contract(
                    str(row.get("dominant_contract", ""))
                ),
                "dominant_price": num(row.get("dominant_contract_price")),
                "dominant_basis": num(row.get("dom_basis")),
                "dominant_basis_rate": num(row.get("dom_basis_rate")),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--date", help="YYYYMMDD,缺省为今天")
    parser.add_argument("--since", help="YYYYMMDD,从该日逐日采到 --date")
    args = parser.parse_args()

    end = datetime.strptime(args.date, "%Y%m%d").date() if args.date else date.today()
    start = datetime.strptime(args.since, "%Y%m%d").date() if args.since else end

    rows: list[dict[str, str]] = []
    failures = 0
    day = start
    while day <= end:
        if day.weekday() < 5:  # 周末源上没有数据,不必请求
            try:
                rows.extend(fetch_day(day))
            except Exception as error:  # noqa: BLE001 —— 逐日隔离,一天失败不毁整轮
                failures += 1
                print(f"SPOT_BASIS_DAY_FAILED {day} {type(error).__name__}: {error}",
                      file=sys.stderr)
        day += timedelta(days=1)

    if not rows:
        # 不写空 CSV:装载脚本会把空文件当成「今天一行都没有」照单全收。
        print(f"SPOT_BASIS_EMPTY start={start} end={end} failures={failures}",
              file=sys.stderr)
        return 1 if failures else 0

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"SPOT_BASIS_OK rows={len(rows)} days={start}..{end} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
