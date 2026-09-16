"""外盘原油主力连续行情:布伦特(OIL)与 WTI(CL),取自 akshare 的新浪源。

**为什么要这张数据**:库里此前只有国内品种,而运营者实际交易的是内外盘联动。
2026-09-15 估 fu2701 对 Brent 的 beta,只能拿两个时点硬算,当场估错两次
(0.55 与 3.85,实际约 1.08,是运营者拿盘面数据纠正的)。**这是结构性盲区。**

**为什么是这个源**:2026-09-16 在 qh 的 collector 镜像里逐个实测 ——

- `ak.futures_global_hist_em`(东财):`RemoteDisconnected`,连不通;
- `ak.futures_foreign_hist`(走新浪):**可用**,且与运营者盘面精确对得上 ——
  2026-09-15 布伦特收 108.71 / 最低 105.10、WTI 收 105.92,
  与当晚行情软件读数一致。

symbol 沿用上游代码不自造:`OIL` = 布伦特、`CL` = WTI
(`futures_foreign_commodity_subscribe_exchange_symbol()` 里的写法)。

**上游两个空洞,必须落成 NULL 不是 0**(DEC-073:0 不是价格,是缺失):

- `OIL` 的 `position` 恒 0 —— 主连不给持仓量;
- `CL` 的 `volume` 恒 0、`settlement` 恒 0 —— 主连不给成交量与结算价。

把 0 当数据灌进去,日后算相关性时会把「没有成交量」读成「成交量为零」,
而且**不报任何错**。所以这里一律 `0 -> 空`,由装载脚本写成 NULL。

**取的是主力连续,不是真实合约**——它是上游拼接出来的合成序列,换月处会跳。
因此落的是独立表 `overseas_price_daily`,不进 `price_history`
(理由见迁移 202609160001)。做相关性/beta 够用;**不要拿它算跨期价差**。
"""

import argparse
import csv
import sys
from datetime import date

# 列序必须与 load-overseas.sql 的 stage 表一一对应 —— `\copy` 按位置匹配,
# 改一边必须同批改另一边(席位契约扩列时踩过,DEC-066)。
FIELDS = [
    "symbol",
    "trade_date",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "settlement_price",
    "volume",
    "open_interest",
]

# 白名单与迁移里的 CHECK 同值,改一处必须同批改另一处。
SYMBOLS = {"OIL": "布伦特原油", "CL": "WTI 原油"}


def _num(value: object) -> str:
    """数字转字符串;**0 与不可解析一律给空**,让装载端写成 NULL。

    这里不区分「上游没这列」和「上游写了 0」——对这两个主连品种,
    实测两者都只出现在 volume / position / settlement 上,含义都是「没有」。
    """
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number == 0:
        return ""
    return repr(number)


def fetch_symbol(
    symbol: str, since: date | None
) -> tuple[list[dict[str, str]], int, int]:
    """抓一个品种。返回(可用行, 整行被拒数, 只丢开盘价的行数)。"""
    import akshare as ak

    frame = ak.futures_foreign_hist(symbol=symbol)
    rows: list[dict[str, str]] = []
    rejected = 0
    demoted = 0
    for record in frame.to_dict("records"):
        raw_date = record.get("date")
        if raw_date is None:
            rejected += 1
            continue
        trade_date = raw_date.date() if hasattr(raw_date, "date") else raw_date
        if since is not None and trade_date < since:
            continue

        close = record.get("close")
        high = record.get("high")
        low = record.get("low")

        # 到货检查,被拒的行连原始值一起打日志 —— 源什么时候出过问题,要看得见
        # (沿用 sina-dce-daily.py 的纪律)。
        try:
            close_value = float(close)
        except (TypeError, ValueError):
            close_value = 0.0
        if close_value <= 0:
            rejected += 1
            print(
                f"OVERSEAS_ROW_REJECTED symbol={symbol} date={trade_date} "
                f"reason=close_not_positive raw={record}",
                file=sys.stderr,
            )
            continue
        # OHLC 自洽性。**这条是实测抓出来的,不是照着教科书加的**:2026-09-16
        # 首次试跑,CL 当天返回 open=105.48 / high=100.61 / low=99.36 —— 开盘价
        # 比最高价还高。同一轮里 CL 的 09-15 收盘也从 105.92 变成 105.48。
        # 原因是**采集时外盘仍在交易**(布伦特北京时间 08:00~次日 06:00,
        # 采集轮次在 16:00~17:30),当日行是盘中快照,上游对它的 high/low
        # 更新有滞后。收盘后的行会被后续轮次 upsert 覆盖修正,
        # 但不自洽的那一版不能先灌进去 —— 算 beta 时它就是个假的跳空。
        try:
            values = {
                "open": float(record["open"])
                if record.get("open") is not None
                else None,
                "high": float(high) if high is not None else None,
                "low": float(low) if low is not None else None,
            }
        except (TypeError, ValueError, KeyError):
            values = {"open": None, "high": None, "low": None}
        #
        # **分级处理,不是一律丢弃**:首版把 open 不自洽的行整条拒了,全量试跑
        # 丢掉 71 行,逐条看下来**它们的 close 全是好的** —— 比如 CL 2026-09-14
        # open=99.99 低于 low=100.53,但 close=101.86 端端正正落在区间内。
        # 这张表的用途是算相关性与 beta,**吃的是 close**;为了一个坏掉的 open
        # 丢掉当天的 close,是本末倒置。
        #   · 区间本身坏了(high < low)或 close 不在区间内 → 整行拒绝,那是真脏;
        #   · 只有 open 出界 → **置空 open,其余照留**,并记一条日志。
        hi, lo = values["high"], values["low"]
        open_value = values["open"]
        if hi is not None and lo is not None:
            if hi < lo or not (lo <= close_value <= hi):
                rejected += 1
                print(
                    f"OVERSEAS_ROW_REJECTED symbol={symbol} date={trade_date} "
                    f"reason=close_outside_range raw={record}",
                    file=sys.stderr,
                )
                continue
            if open_value is not None and not (lo <= open_value <= hi):
                demoted += 1
                print(
                    f"OVERSEAS_OPEN_DROPPED symbol={symbol} date={trade_date} "
                    f"open={open_value} range=[{lo},{hi}] —— 只丢开盘价,close 保留",
                    file=sys.stderr,
                )
                open_value = None

        rows.append(
            {
                "symbol": symbol,
                "trade_date": str(trade_date),
                "open_price": _num(open_value),
                "high_price": _num(high),
                "low_price": _num(low),
                "close_price": repr(close_value),
                "settlement_price": _num(record.get("settlement")),
                "volume": _num(record.get("volume")),
                "open_interest": _num(record.get("position")),
            }
        )
    return rows, rejected, demoted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--since",
        help="YYYY-MM-DD,只取该日及以后;缺省取上游给的全部历史("
        "OIL 回到 2016-09、CL 回到 1996-09)。日常采集给一个近日期即可,"
        "全量 upsert 也是幂等的,只是慢一点。",
    )
    args = parser.parse_args()

    since = date.fromisoformat(args.since) if args.since else None

    rows: list[dict[str, str]] = []
    failures = 0
    rejected_total = 0
    demoted_total = 0
    for symbol in SYMBOLS:
        try:
            got, rejected, demoted = fetch_symbol(symbol, since)
        except Exception as error:  # noqa: BLE001 —— 逐品种隔离,一个失败不毁整轮
            failures += 1
            print(
                f"OVERSEAS_SYMBOL_FAILED {symbol} {type(error).__name__}: {error}",
                file=sys.stderr,
            )
            continue
        rejected_total += rejected
        demoted_total += demoted
        rows.extend(got)
        print(
            f"OVERSEAS_SYMBOL_OK {symbol} rows={len(got)} "
            f"rejected={rejected} open_dropped={demoted}"
        )

    if not rows:
        # 不写空 CSV:装载脚本会把空文件当成「今天一行都没有」照单全收。
        print(f"OVERSEAS_EMPTY failures={failures}", file=sys.stderr)
        return 1

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(
        f"OVERSEAS_OK rows={len(rows)} symbols={len(SYMBOLS) - failures}"
        f" rejected={rejected_total} open_dropped={demoted_total}"
        f" failures={failures}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
