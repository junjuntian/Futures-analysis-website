from __future__ import annotations

import re
from datetime import date
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
    "close_price",
    "settlement_price",
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
    source: ExchangeSource, collection_date: date, frame: pd.DataFrame
) -> list[dict[str, str]]:
    if frame is None or frame.empty:
        raise ValueError("market response is empty")
    observed_at = f"{collection_date.isoformat()}T13:30:00Z"
    version = calendar_version(source, collection_date)
    rows: list[dict[str, str]] = []
    for _, raw in frame.iterrows():
        contract = _pick(raw, "symbol", "合约代码", "合约")
        trade_date = _iso_date(_pick(raw, "date", "交易日期"))
        if not contract or trade_date != collection_date.isoformat():
            continue
        contract = contract.upper().replace(" ", "")
        close = _decimal(_pick(raw, "close", "今收盘"))
        settlement = _decimal(_pick(raw, "settle", "今结算"))
        if not close and not settlement:
            continue
        rows.append(
            {
                "exchange_code": source.code,
                "contract_code": contract,
                "trade_date": trade_date,
                "session_type": "daily",
                "observed_at": observed_at,
                "granularity": "1d",
                "close_price": close,
                "settlement_price": settlement,
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
            for rank_type, name_field, value_field, target in [
                ("volume", "vol_party_name", "vol", "volume"),
                ("long", "long_party_name", "long_open_interest", "long_position"),
                ("short", "short_party_name", "short_open_interest", "short_position"),
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
        number = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return ""
    if not number.is_finite():
        return ""
    return format(number, "f")


def _integer(value: str, *, allow_zero: bool = False) -> str:
    decimal = _decimal(value)
    if not decimal:
        return ""
    number = Decimal(decimal)
    if number != number.to_integral_value() or number < 0 or (number == 0 and not allow_zero):
        return ""
    return str(int(number))
