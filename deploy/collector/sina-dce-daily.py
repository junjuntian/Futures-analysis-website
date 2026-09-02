"""大商所价格的每日增量，取自新浪。

为什么是新浪：交易所官网对所有客户端 412（含首页），akshare 的大商所接口打的也是
同一个站因而拿回挑战页，东财的行情与 K 线端点 2026-08-10 起第一个请求就被断开。
新浪是当天唯一还应答的源，且对焦煤/鸡蛋/生猪当前在市的 29 个合约全覆盖、更新到当日。

**这不是引入一个新源**：`price_history` 里大商所 2025-01-02 之后的行情本来就是新浪
的（回填时补的缺口，11,262 行）。这个脚本只是让那条已经在用的路每天继续走。

与交易所口径的两条差异，回填时已实测并如实落库，这里沿用：

1. 新浪的成交量与持仓量是**单边**，交易所是双边，正好差一倍——以 2024 年 843 个
   重叠日对照年度文件，持仓量比值恰好 2.0000，无一例外。落库标 `single`。
2. 新浪**不给成交额**，该列留空，不推算。

**到货检查**（本文件相对回填脚本新增的部分）。同一批对照里 843 天有 837 天与交易所
完全相同，剩下 6 天分两类，都可检出：

- 2024-09-25 一天，新浪把六个合约的结算价全写成 `0.0`。0 不是价格，是缺失——
  当成价格灌进去，席位持仓成本会按零结算价算，而且不报任何错。
- 三个当日只成交 1–2 手的合约日，收盘价与交易所差最多 60 点。这类合约不会是套利页
  的腿，但没有理由把明显不自洽的行放进来。

被拒的行**连原始值一起打进日志**，不是安静跳过：源什么时候出过问题，要看得见。
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
# 东财这个接口在行情端点全断之后仍然正常应答，且 rtime 是当天。用它拿在市合约清单，
# 比自己按月份推一遍再靠请求失败去试要少几十次请求，也不会漏掉新挂的合约。
EASTMONEY_CATALOG = "https://futsse-static.eastmoney.com/redis?msgid=114"
EASTMONEY_VARIETY = "https://futsse-static.eastmoney.com/redis?msgid=114_{vtype}"
# 2026-08-28 加铁矿石 I(运营者要求)。新浪日线端点实测有全历史,东财品种表 vtype=13。
WANT = ("JM", "JD", "LH", "I")
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
# 新浪对每秒多少次没有公开说明；回填时约 120 次无间隔请求被 456 拒过，所以按两秒走。
PACE = 2.0
HEADERS = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}


def get_json(url):
    response = requests.get(url, headers=HEADERS, timeout=40)
    response.raise_for_status()
    return response.json()


def daily(symbol):
    response = requests.get(
        SINA_ENDPOINT.format(sym=symbol), headers=HEADERS, timeout=40
    )
    response.raise_for_status()
    body = response.text
    payload = body[body.find("=") + 1 :].strip().rstrip(";")
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        found = re.search(r"\[(.*)\]", body, re.DOTALL)
        return json.loads("[" + found.group(1) + "]") if found else []


def listed_contracts():
    """在市合约清单。东财不可用时退回按月份推，宁可多试几十次也不要漏掉合约。"""
    try:
        varieties = {
            str(item.get("vcode") or "").upper(): str(item.get("vtype") or "")
            for item in get_json(EASTMONEY_CATALOG)
            if isinstance(item, dict)
        }
        contracts = []
        for symbol in WANT:
            vtype = varieties.get(symbol)
            if not vtype:
                raise ValueError(f"东财品种表里没有 {symbol}")
            for item in get_json(EASTMONEY_VARIETY.format(vtype=vtype)):
                code = str(item.get("code") or "").upper()
                if re.fullmatch(r"[A-Z]{1,2}[0-9]{4}", code):
                    contracts.append(code)
        if contracts:
            print(f"东财给出在市合约 {len(contracts)} 个", flush=True)
            return sorted(set(contracts))
    except Exception as error:  # noqa: BLE001 - 退路在下面，如实说一声
        print(f"东财合约表取不到（{type(error).__name__}），改为按月份推", flush=True)

    now = datetime.now(UTC).date()
    return sorted(
        f"{variety}{year % 100:02d}{month:02d}"
        for variety in WANT
        for year in (now.year, now.year + 1, now.year + 2)
        for month in range(1, 13)
    )


def variety_of(symbol):
    """合约代码里的品种字母。

    **不能写 `symbol[:2]`** —— 铁矿石的代码是单字母 `I`,`"I2601"[:2]` 得到
    `"I2"`:既不在 `WANT` 里(整个品种被静默跳过),写进 instrument 列也是错的。
    2026-09-02 查出来的:`WANT` 里 2026-08-30 就加了 `"I"`,而日更**从来没有采到
    过一行铁矿石行情**,数据一直停在回填那天(08-28)。零产出守卫也照不到它 ——
    JM/JD/LH 三个双字母品种每天照常写出几十行,`kept` 从来不是 0。

    认不出来的返回空串,调用方据此跳过,不要让一个畸形代码写出一行畸形数据。
    """
    match = re.fullmatch(r"([A-Z]{1,2})[0-9]{4}", symbol.upper())
    return match.group(1) if match else ""


def usable(bar, contract, day):
    """这一行自不自洽。不自洽就别放进来，并说清楚是哪一条不过。"""
    def number(key):
        raw = bar.get(key)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    close, settlement = number("c"), number("s")
    low, high, volume = number("l"), number("h"), number("v")

    # 0 不是价格，是缺失。2024-09-25 新浪把六个合约的结算价全写成 0 就是这么被抓住的。
    if close is not None and close <= 0:
        return f"收盘价为 {close}"
    if settlement is not None and settlement <= 0:
        return f"结算价为 {settlement}"
    if close is None and settlement is None:
        return "收盘价与结算价都没有"
    # 收盘必须落在当日区间内，否则这一行自相矛盾。
    if close is not None and low is not None and high is not None and not (low <= close <= high):
        return f"收盘价 {close} 不在区间 [{low}, {high}] 内"
    if volume is not None and volume <= 0:
        return f"成交量为 {volume}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--out", default="/opt/futures-platform/load/price_dce_daily.csv")
    args = parser.parse_args()

    since = (datetime.now(UTC).date() - timedelta(days=args.days)).isoformat()
    contracts = listed_contracts()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    kept = rejected = failed = 0
    with Path(args.out).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for symbol in contracts:
            variety = variety_of(symbol)
            if variety not in WANT:
                continue
            time.sleep(PACE)
            try:
                bars = daily(symbol)
            except Exception as error:  # noqa: BLE001 - 报出来，继续下一个合约
                print(f"FAIL {symbol}: {type(error).__name__}", file=sys.stderr, flush=True)
                failed += 1
                continue
            for bar in bars or []:
                day = str(bar.get("d") or "")
                if day < since:
                    continue
                reason = usable(bar, symbol, day)
                if reason:
                    # 连原始值一起打出来：以后回头查「那天为什么没数据」时，
                    # 只写一句「跳过」是查不下去的。
                    print(
                        f"REJECT {symbol} {day} {reason} raw={json.dumps(bar, ensure_ascii=False)}",
                        file=sys.stderr,
                        flush=True,
                    )
                    rejected += 1
                    continue
                writer.writerow([
                    "DCE",
                    variety,
                    symbol,
                    day,
                    bar.get("o") or "",
                    bar.get("h") or "",
                    bar.get("l") or "",
                    bar.get("c") or "",
                    bar.get("s") or "",
                    "",  # 新浪不给前结算
                    bar.get("v") or "",
                    "single",  # 见文件头：新浪单边，交易所双边
                    "",  # 新浪不给成交额
                    bar.get("p") or "",
                    "",
                    "sina",
                ])
                kept += 1

    print(
        f"合约 {len(contracts)} 个，自 {since} 起写出 {kept} 行，"
        f"拒绝 {rejected} 行，失败 {failed} 个合约 -> {args.out}"
    )
    # 零产出必须是失败。原来这里无条件 return 0：上游全挂、一行没写出来，
    # loader 拿着只有表头的 CSV「成功」跑完，日更看起来一切正常，数据却停在
    # 昨天——正是「从来没自动跑过」家族里最难发现的那种静默。
    # 部分合约失败仍返回 0：新浪偶发单合约抽风，upsert 到手的那部分好过全丢，
    # 失败数已经打在日志里，连续出现自然会被看到。
    if kept == 0:
        print("SINA_DCE_ZERO_OUTPUT 一行未写出，判失败", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
