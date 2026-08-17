from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

from futures_collector.sources import ExchangeSource

CATALOG_FIELDS = [
    "exchange_code",
    "exchange_name",
    "timezone",
    "instrument_code",
    "instrument_name",
    "currency_code",
    "contract_multiplier",
    "price_tick",
    "contract_code",
    "delivery_month",
    "listed_at",
    "expires_at",
    "source_record_ref",
]
CALENDAR_FIELDS = [
    "exchange_code",
    "calendar_version",
    "effective_from",
    "trade_date",
    "is_trading_day",
    "day_session_json",
    "night_session_json",
    "source_record_ref",
]
MARKET_FIELDS = [
    "exchange_code",
    "contract_code",
    "trade_date",
    "session_type",
    "observed_at",
    "granularity",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "settlement_price",
    "volume",
    "turnover",
    "currency_code",
    "calendar_version",
    "revision_no",
    "source_record_ref",
]
SEAT_FIELDS = [
    "exchange_code",
    "contract_code",
    "trade_date",
    "seat_name",
    "rank_type",
    "rank",
    "volume",
    "long_position",
    "short_position",
    "change",
    "source_record_ref",
]
DATASET_FIELDS = {
    "futures_catalog_v1": CATALOG_FIELDS,
    "trading_calendar_v1": CALENDAR_FIELDS,
    "daily_market_prices_v1": MARKET_FIELDS,
    "seat_positions_v1": SEAT_FIELDS,
}


def normalize_catalog(
    source: ExchangeSource, collection_date: date, frame: pd.DataFrame
) -> list[dict[str, str]]:
    if frame is None or frame.empty:
        raise ValueError("catalog response is empty")
    rows: list[dict[str, str]] = []
    for _, raw in frame.iterrows():
        contract = _pick(raw, "合约代码", "合约", "contract", "symbol")
        if not contract:
            continue
        contract = contract.upper().replace(" ", "")
        instrument = _pick(raw, "产品代码", "品种代码", "variety") or _instrument(contract)
        instrument = instrument.upper()
        name = _pick(raw, "产品名称", "品种名称", "品种") or instrument
        rows.append(
            {
                "exchange_code": source.code,
                "exchange_name": source.name,
                "timezone": "Asia/Shanghai",
                "instrument_code": instrument,
                "instrument_name": name,
                "currency_code": (
                    _pick(raw, "交易币种ISO编码", "结算币种ISO编码") or "CNY"
                ).upper(),
                "contract_multiplier": _decimal(_pick(raw, "交易单位")),
                "price_tick": _decimal(_pick(raw, "最小变动价位", "最小变动单位")),
                "contract_code": contract,
                "delivery_month": _delivery_month(contract, collection_date),
                "listed_at": _iso_date(_pick(raw, "上市日", "开始交易日", "第一交易日")),
                "expires_at": _iso_date(
                    _pick(
                        raw,
                        "到期日",
                        "最后交易日",
                        "最后交易日待国家公布2025年节假日安排后进行调整",
                    )
                ),
                "source_record_ref": f"{source.code}:{contract}:{collection_date.isoformat()}",
            }
        )
    if not rows:
        raise ValueError("catalog response has no contracts")
    return rows


def normalize_calendar(source: ExchangeSource, collection_date: date) -> list[dict[str, str]]:
    version = calendar_version(source, collection_date)
    return [
        {
            "exchange_code": source.code,
            "calendar_version": version,
            "effective_from": collection_date.isoformat(),
            "trade_date": collection_date.isoformat(),
            "is_trading_day": "true",
            "day_session_json": "{}",
            "night_session_json": "{}",
            "source_record_ref": f"{source.code}:calendar:{collection_date.isoformat()}",
        }
    ]


def normalize_market(
    source: ExchangeSource,
    collection_date: date,
    frame: pd.DataFrame,
    observed_at: datetime,
) -> list[dict[str, str]]:
    if frame is None or frame.empty:
        raise ValueError("market response is empty")
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    observed_at_text = observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    version = calendar_version(source, collection_date)
    rows: list[dict[str, str]] = []
    for _, raw in frame.iterrows():
        contract = _pick(raw, "symbol", "合约代码", "合约")
        trade_date = _iso_date(_pick(raw, "date", "交易日期"))
        if not contract or trade_date != collection_date.isoformat():
            continue
        contract = contract.upper().replace(" ", "")
        close = _decimal(_pick(raw, "close", "今收盘", "收盘价"))
        settlement = _decimal(_pick(raw, "settle", "今结算", "结算价"))
        if not close and not settlement:
            continue
        # A contract that did not trade is written with zeros in all four range
        # fields by the exchange's own files. That says nothing traded, not
        # that it traded at zero, so the range is recorded as absent.
        opening = _decimal(_pick(raw, "open", "今开盘", "开盘价"))
        high = _decimal(_pick(raw, "high", "最高", "最高价"))
        low = _decimal(_pick(raw, "low", "最低", "最低价"))
        if _all_zero(opening, high, low):
            opening = high = low = ""
        rows.append(
            {
                "exchange_code": source.code,
                "contract_code": contract,
                "trade_date": trade_date,
                "session_type": "daily",
                "observed_at": observed_at_text,
                "granularity": "1d",
                "open_price": opening,
                "high_price": high,
                "low_price": low,
                "close_price": close,
                "settlement_price": settlement,
                # Carried so the price multiplier can be checked against the
                # exchange's own arithmetic: turnover / (volume x settlement)
                # is the multiplier, and a mismatch means a contract spec
                # changed or the wrong multiplier is on file. That check caught
                # eggs being off by a factor of two.
                "volume": _decimal(_pick(raw, "volume", "成交量")),
                "turnover": _decimal(_pick(raw, "turnover", "成交额")),
                "currency_code": "CNY",
                "calendar_version": version,
                "revision_no": "1",
                "source_record_ref": f"{source.code}:{contract}:{trade_date}:daily",
            }
        )
    if not rows:
        raise ValueError("market response has no rows for the requested date")
    return rows


def normalize_seats(
    source: ExchangeSource, collection_date: date, tables: Any
) -> list[dict[str, str]]:
    if not isinstance(tables, dict) or not tables:
        raise ValueError("seat response is empty")
    rows: list[dict[str, str]] = []
    for key, frame in tables.items():
        if frame is None or frame.empty:
            continue
        for _, raw in frame.iterrows():
            contract = (_pick(raw, "symbol", "合约") or str(key)).upper().replace(" ", "")
            rank = _integer(_pick(raw, "rank", "名次"))
            if not contract or not rank:
                continue
            for rank_type, name_field, value_field, target, change_field in [
                ("volume", "vol_party_name", "vol", "volume", "vol_chg"),
                (
                    "long",
                    "long_party_name",
                    "long_open_interest",
                    "long_position",
                    "long_open_interest_chg",
                ),
                (
                    "short",
                    "short_party_name",
                    "short_open_interest",
                    "short_position",
                    "short_open_interest_chg",
                ),
            ]:
                seat_name = _pick(raw, name_field)
                value = _integer(_pick(raw, value_field), allow_zero=True)
                if not seat_name or value == "":
                    continue
                row = {
                    "exchange_code": source.code,
                    "contract_code": contract,
                    "trade_date": collection_date.isoformat(),
                    "seat_name": seat_name,
                    "rank_type": rank_type,
                    "rank": rank,
                    "volume": "",
                    "long_position": "",
                    "short_position": "",
                    # 增减可负可零,与持仓不同一套校验;源不给就留空——空是
                    # 「不知道」,拿前后两天自己相减凑数在会员进出前二十那天
                    # 必然与交易所口径对不上(load-seats-direct.sql 同一条纪律)。
                    "change": _signed_integer(_pick(raw, change_field)),
                    "source_record_ref": (
                        f"{source.code}:{contract}:{collection_date.isoformat()}:{rank_type}:{rank}"
                    ),
                }
                row[target] = value
                rows.append(row)
    if not rows:
        raise ValueError("seat response has no ranked contracts")
    return rows


def calendar_version(source: ExchangeSource, collection_date: date) -> str:
    return f"akshare-v1:{source.code}:{collection_date.isoformat()}"


def _pick(row: pd.Series, *names: str) -> str:
    for name in names:
        if name in row.index:
            value = row[name]
            if pd.notna(value):
                text = str(value).strip()
                if text and text not in {"-", "--", "nan", "None"}:
                    return text
    return ""


def row_instrument(row: dict[str, str]) -> str | None:
    """The variety a normalized row belongs to, or None if it belongs to none.

    Catalog rows carry the instrument outright; market and seat rows only carry
    a contract code, whose letter prefix is the variety. Calendar rows describe
    a trading day rather than an instrument and so belong to every variety.
    """
    instrument = (row.get("instrument_code") or "").strip().upper()
    if instrument:
        return instrument
    contract = (row.get("contract_code") or "").strip().upper()
    if not contract:
        return None
    try:
        return _instrument(contract)
    except ValueError:
        return None


def filter_rows_by_variety(
    rows: list[dict[str, str]], varieties: frozenset[str] | None
) -> list[dict[str, str]]:
    """Keep only the rows belonging to the requested varieties.

    `None` means no narrowing at all, which is not the same as an empty set:
    an empty set would silently discard everything.
    """
    if varieties is None:
        return rows
    kept = []
    for row in rows:
        instrument = row_instrument(row)
        if instrument is None or instrument in varieties:
            kept.append(row)
    return kept


def _instrument(contract: str) -> str:
    match = re.match(r"([A-Z]+)", contract.upper())
    if not match:
        raise ValueError("contract code has no instrument prefix")
    return match.group(1)


def _delivery_month(contract: str, collection_date: date) -> str:
    digits = re.sub(r"^[A-Z]+", "", contract.upper())
    if len(digits) >= 4:
        year = 2000 + int(digits[-4:-2])
        month = int(digits[-2:])
    elif len(digits) == 3:
        year_digit = int(digits[0])
        candidates = [
            year
            for year in range(collection_date.year - 5, collection_date.year + 6)
            if year % 10 == year_digit
        ]
        year = min(candidates, key=lambda value: abs(value - collection_date.year))
        month = int(digits[-2:])
    else:
        return ""
    return f"{year:04d}-{month:02d}" if 1 <= month <= 12 else ""


def _iso_date(value: str) -> str:
    if not value:
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else parsed.date().isoformat()


def _decimal(value: str) -> str:
    if not value:
        return ""
    try:
        number = Decimal(_leading_number(value))
    except (InvalidOperation, ValueError):
        return ""
    if not number.is_finite():
        return ""
    return format(number, "f")


def _all_zero(*values: str) -> bool:
    if not any(values):
        return False
    try:
        return all(Decimal(value) == 0 for value in values if value)
    except InvalidOperation:
        return False


def _leading_number(value: str) -> str:
    """The number at the front of a value, dropping any unit that follows.

    CZCE writes contract parameters with their units attached — `10吨/手`,
    `1.00元/吨` — so a plain Decimal parse rejects the whole field and the
    multiplier lands empty. Without it a position's profit cannot be computed
    at all, which is why this is worth reading rather than discarding.

    Only a leading number is accepted. A value that starts with anything else
    is still rejected, so this widens what parses without inventing a number
    out of prose.
    """
    text = value.replace(",", "").strip()
    match = re.match(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        raise ValueError("value does not start with a number")
    return match.group(0)


def _signed_integer(value: str) -> str:
    """增减量:整数,允许负与零。持仓走 `_integer`(拒负),别混用。"""
    decimal = _decimal(value)
    if not decimal:
        return ""
    number = Decimal(decimal)
    if number != number.to_integral_value():
        return ""
    return str(int(number))


def _integer(value: str, *, allow_zero: bool = False) -> str:
    decimal = _decimal(value)
    if not decimal:
        return ""
    number = Decimal(decimal)
    if number != number.to_integral_value() or number < 0 or (number == 0 and not allow_zero):
        return ""
    return str(int(number))
