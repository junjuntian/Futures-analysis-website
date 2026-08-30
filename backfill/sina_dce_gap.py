"""补大商所 2025–2026 的行情缺口。

交易所自己的年度文件只发到 2024，官网接口对所有客户端 412，东财不保留退市合约。
新浪保留退市合约到约 2018-09，且逐合约一次返回全部日线，所以这两年只要按合约取，
约七十个请求就够——不是按天。

**与交易所口径的差异，两条，都必须如实落库：**

1. 新浪的**成交量与持仓量是单边**，交易所是双边，正好差一倍。以 JM2501 2024-01-16
   对照交易所年度文件实测：价格五项完全相同，成交量 20 对 40、持仓 16 对 32。
   价格一致会让人以为整行都一致，所以这一条不核对就发现不了。落库标 `single`。
2. 新浪**不给成交额**。该列留空，不推算——由它推出的点值校验在这两年就跑不了，
   这是缺口的真实形状，不该用一个编出来的数掩盖。
"""

import argparse
import csv
import json
import re
import time
from pathlib import Path

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
)
ENDPOINT = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
    "var%20_{sym}=/InnerFuturesNewService.getDailyKLine?symbol={sym}"
)
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
# 新浪对每秒多少次没有公开说明；今天早些时候约 120 次无间隔请求就被 456 拒了，
# 所以按两秒走。七十来个合约总共两分多钟，没有赶时间的理由。
PACE = 2.0


def daily(symbol):
    response = requests.get(
        ENDPOINT.format(sym=symbol),
        headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
        timeout=40,
    )
    response.raise_for_status()
    body = response.text
    payload = body[body.find("=") + 1 :].strip().rstrip(";")
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        found = re.search(r"\[(.*)\]", body, re.DOTALL)
        return json.loads("[" + found.group(1) + "]") if found else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-date", default="2025-01-01")
    ap.add_argument("--to-date", default="2026-12-31")
    ap.add_argument("--out", default="/opt/futures-platform/load/price_dce_gap.csv")
    # DEC-158:铁矿石回填要拉 2019-2025(新浪只留 2019 起),年份跟着起止日期走。
    ap.add_argument("--varieties", default="JM,JD,LH")
    args = ap.parse_args()

    y0 = int(args.from_date[2:4])
    y1 = int(args.to_date[2:4]) + 2  # 远月合约挂到起始年 +1~2 年
    candidates = [
        f"{variety}{year}{month:02d}"
        for variety in [v.strip().upper() for v in args.varieties.split(",") if v.strip()]
        for year in range(y0, y1 + 1)
        for month in range(1, 13)
    ]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    kept = listed = 0
    with Path(args.out).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for symbol in candidates:
            time.sleep(PACE)
            try:
                bars = daily(symbol)
            except Exception as error:  # noqa: BLE001 - named, then next contract
                print(f"FAIL {symbol}: {type(error).__name__}", flush=True)
                continue
            if not bars:
                # 从未上市的候选，不是错误：合约集合是推出来的，本就会overshoot。
                continue
            listed += 1
            rows = 0
            for bar in bars:
                day = str(bar.get("d") or "")
                if not (args.from_date <= day <= args.to_date):
                    continue
                close = str(bar.get("c") or "")
                settlement = str(bar.get("s") or "")
                if not close and not settlement:
                    continue
                writer.writerow(
                    [
                        "DCE",
                        "".join(c for c in symbol if c.isalpha()),  # 单字码品种(I1601)不能切前两位,dce_to_csv 同款坑
                        symbol,
                        day,
                        bar.get("o") or "",
                        bar.get("h") or "",
                        bar.get("l") or "",
                        close,
                        settlement,
                        "",
                        bar.get("v") or "",
                        # 见文件头：新浪按单边，交易所按双边。
                        "single",
                        # 新浪不给成交额。
                        "",
                        bar.get("p") or "",
                        "",
                        "sina",
                    ]
                )
                rows += 1
                kept += 1
            print(f"  {symbol}: {len(bars)} 天，落在区间 {rows}", flush=True)
    print(f"\n候选 {len(candidates)}，实际上市 {listed}，写出 {kept} 行 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
