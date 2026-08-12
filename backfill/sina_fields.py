"""新浪给大商所的到底是哪些字段，以及它的数字和交易所官方文件对不对得上。

「新浪有这个合约」和「新浪给的数对」是两件事。回填时用的是交易所年度文件，
两边在同一天同一合约上应当逐个相等；不等就说明其中一个源是错的，那比没有数据更糟。

只读，逐个合约一次请求。
"""

import csv
import time

import akshare

CONTRACTS = ["JD2502", "JD2503", "JM2502", "JM2506", "LH2507", "JD2504"]
OUT = "/tmp/out/sina_dce.csv"


def main() -> int:
    first = akshare.futures_zh_daily_sina(symbol=CONTRACTS[0])
    print("新浪返回的列：", list(first.columns))
    print("最后一行：")
    print(first.tail(1).to_string())

    rows = 0
    with open(OUT, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["contract", "trade_date", "open", "high", "low", "close", "volume", "hold", "settle"]
        )
        for index, contract in enumerate(CONTRACTS):
            if index:
                time.sleep(1.0)
            frame = akshare.futures_zh_daily_sina(symbol=contract)
            for _, row in frame.iterrows():
                writer.writerow(
                    [
                        contract,
                        str(row["date"]),
                        row.get("open"),
                        row.get("high"),
                        row.get("low"),
                        row.get("close"),
                        row.get("volume"),
                        row.get("hold"),
                        row.get("settle"),
                    ]
                )
                rows += 1
            print(f"{contract} {len(frame)} 行", flush=True)
    print(f"写出 {rows} 行到 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
