from __future__ import annotations

import contextlib
import ipaddress
import logging
import re
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlsplit

import akshare
import pandas as pd
import requests

LOG = logging.getLogger("futures_collector.sources")
MAX_REDIRECTS = 5
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_DNS_GUARD_LOCK = threading.RLock()


@dataclass(frozen=True)
class ExchangeSource:
    code: str
    source_code: str
    name: str
    domains: frozenset[str]
    catalog_function: str
    market_function: str
    seats_function: str
    catalog_takes_date: bool


SOURCES: dict[str, ExchangeSource] = {
    "DCE": ExchangeSource(
        "DCE",
        "akshare_dce_official",
        "大连商品交易所",
        frozenset({"www.dce.com.cn", "portal.dce.com.cn"}),
        "futures_contract_info_dce",
        "get_dce_daily",
        "futures_dce_position_rank",
        False,
    ),
    "SHFE": ExchangeSource(
        "SHFE",
        "akshare_shfe_official",
        "上海期货交易所",
        frozenset({"www.shfe.com.cn", "tsite.shfe.com.cn"}),
        "futures_contract_info_shfe",
        "get_shfe_daily",
        "get_shfe_rank_table",
        True,
    ),
    "CZCE": ExchangeSource(
        "CZCE",
        "akshare_czce_official",
        "郑州商品交易所",
        frozenset({"www.czce.com.cn"}),
        "futures_contract_info_czce",
        "get_czce_daily",
        "get_rank_table_czce",
        True,
    ),
    "GFEX": ExchangeSource(
        "GFEX",
        "akshare_gfex_official",
        "广州期货交易所",
        frozenset({"www.gfex.com.cn"}),
        "futures_contract_info_gfex",
        "get_gfex_daily",
        "futures_gfex_position_rank",
        False,
    ),
    "CFFEX": ExchangeSource(
        "CFFEX",
        "akshare_cffex_official",
        "中国金融期货交易所",
        frozenset({"www.cffex.com.cn"}),
        "futures_contract_info_cffex",
        "get_cffex_daily",
        "get_cffex_rank_table",
        True,
    ),
}

DCE_FALLBACK_SOURCE = ExchangeSource(
    "DCE",
    "akshare_sina_dce_fallback",
    "大连商品交易所",
    frozenset(
        {
            "vip.stock.finance.sina.com.cn",
            "finance.sina.com.cn",
            "stock2.finance.sina.com.cn",
        }
    ),
    "sina_dce_catalog",
    "futures_zh_daily_sina",
    "futures_hold_pos_sina",
    False,
)


class OutboundPolicyError(ValueError):
    pass


class DatasetCompletenessError(ValueError):
    def __init__(self, dataset: str, skipped_count: int, expected_count: int) -> None:
        self.dataset = dataset
        self.skipped_count = skipped_count
        self.expected_count = expected_count
        super().__init__(f"{dataset} incomplete")


class AkshareAdapter:
    def __init__(self) -> None:
        self._dce_catalog_cache: dict[date, pd.DataFrame] = {}
        self._dce_market_cache: dict[date, pd.DataFrame] = {}

    def catalog(self, source: ExchangeSource, collection_date: date) -> Any:
        function = getattr(akshare, source.catalog_function)
        with official_requests_only(source.domains):
            if source.catalog_takes_date:
                return function(date=collection_date.strftime("%Y%m%d"))
            return function()

    def market(self, source: ExchangeSource, collection_date: date) -> Any:
        function = getattr(akshare, source.market_function)
        with official_requests_only(source.domains):
            return function(date=collection_date.strftime("%Y%m%d"))

    def seats(self, source: ExchangeSource, collection_date: date) -> Any:
        function = getattr(akshare, source.seats_function)
        with official_requests_only(source.domains):
            return function(date=collection_date.strftime("%Y%m%d"))

    def fallback_catalog(self, source: ExchangeSource, collection_date: date) -> pd.DataFrame:
        self._require_dce(source)
        return self._dce_catalog(collection_date).copy()

    def fallback_market(self, source: ExchangeSource, collection_date: date) -> pd.DataFrame:
        self._require_dce(source)
        cached = self._dce_market_cache.get(collection_date)
        if cached is not None:
            return cached.copy()
        catalog = self._dce_catalog(collection_date)
        contracts = catalog["合约"].drop_duplicates().tolist()
        frames: list[pd.DataFrame] = []
        skipped_count = 0
        with official_requests_only(DCE_FALLBACK_SOURCE.domains):
            for contract in contracts:
                try:
                    frame = akshare.futures_zh_daily_sina(symbol=contract)
                except OutboundPolicyError:
                    raise
                except Exception:
                    skipped_count += 1
                    LOG.warning(
                        "dce_fallback_market_contract_skipped contract=%s skipped_count=%d",
                        contract,
                        skipped_count,
                    )
                    continue
                if frame is None or frame.empty or "date" not in frame.columns:
                    skipped_count += 1
                    LOG.warning(
                        "dce_fallback_market_contract_skipped contract=%s skipped_count=%d",
                        contract,
                        skipped_count,
                    )
                    continue
                dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
                selected = frame[dates == collection_date].copy()
                if selected.empty:
                    skipped_count += 1
                    LOG.warning(
                        "dce_fallback_market_contract_skipped contract=%s skipped_count=%d",
                        contract,
                        skipped_count,
                    )
                    continue
                selected["symbol"] = contract
                frames.append(selected)
        if skipped_count:
            LOG.error(
                "dce_fallback_dataset_incomplete dataset=market skipped_count=%d "
                "expected_contracts=%d",
                skipped_count,
                len(contracts),
            )
            raise DatasetCompletenessError("market", skipped_count, len(contracts))
        if not frames:
            raise DatasetCompletenessError("market", len(contracts), len(contracts))
        result = pd.concat(frames, ignore_index=True)
        self._dce_market_cache[collection_date] = result
        return result.copy()

    def fallback_seats(
        self, source: ExchangeSource, collection_date: date
    ) -> dict[str, pd.DataFrame]:
        self._require_dce(source)
        catalog = self._dce_catalog(collection_date)
        contracts = catalog["合约"].drop_duplicates().tolist()
        tables: dict[str, pd.DataFrame] = {}
        skipped_count = 0
        kinds = (
            ("成交量", "vol_party_name", "vol"),
            ("多单持仓", "long_party_name", "long_open_interest"),
            ("空单持仓", "short_party_name", "short_open_interest"),
        )
        with official_requests_only(DCE_FALLBACK_SOURCE.domains):
            for contract in contracts:
                contract_frames: list[pd.DataFrame] = []
                for kind, party_field, value_field in kinds:
                    try:
                        frame = akshare.futures_hold_pos_sina(
                            symbol=kind,
                            contract=contract,
                            date=collection_date.strftime("%Y%m%d"),
                        )
                    except OutboundPolicyError:
                        raise
                    except Exception:
                        skipped_count += 1
                        LOG.warning(
                            "dce_fallback_seat_contract_skipped contract=%s rank_type=%s "
                            "skipped_count=%d",
                            contract,
                            kind,
                            skipped_count,
                        )
                        continue
                    normalized = _normalize_sina_seat_table(
                        frame, contract, party_field, value_field
                    )
                    if normalized.empty:
                        skipped_count += 1
                        LOG.warning(
                            "dce_fallback_seat_contract_skipped contract=%s rank_type=%s "
                            "skipped_count=%d",
                            contract,
                            kind,
                            skipped_count,
                        )
                        continue
                    contract_frames.append(normalized)
                if contract_frames:
                    tables[contract] = pd.concat(contract_frames, ignore_index=True)
        if skipped_count:
            LOG.error(
                "dce_fallback_dataset_incomplete dataset=seats skipped_count=%d "
                "expected_requests=%d",
                skipped_count,
                len(contracts) * len(kinds),
            )
            raise DatasetCompletenessError("seats", skipped_count, len(contracts) * len(kinds))
        if not tables:
            raise DatasetCompletenessError(
                "seats", len(contracts) * len(kinds), len(contracts) * len(kinds)
            )
        return tables

    def _dce_catalog(self, collection_date: date) -> pd.DataFrame:
        cached = self._dce_catalog_cache.get(collection_date)
        if cached is not None:
            return cached
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        with official_requests_only(DCE_FALLBACK_SOURCE.domains):
            marks = akshare.futures_symbol_mark()
            if marks is None or marks.empty or not {"exchange", "symbol"}.issubset(marks.columns):
                raise ValueError("Sina DCE instrument response is invalid")
            instruments = marks[marks["exchange"] == "大连商品交易所"]
            for _, instrument in instruments.iterrows():
                instrument_name = str(instrument["symbol"]).strip()
                if not instrument_name:
                    continue
                try:
                    contracts = akshare.futures_zh_realtime(symbol=instrument_name)
                except OutboundPolicyError:
                    raise
                except Exception:
                    LOG.warning("dce_fallback_instrument_skipped instrument=%s", instrument_name)
                    continue
                if contracts is None or contracts.empty or "symbol" not in contracts.columns:
                    continue
                for raw_contract in contracts["symbol"].tolist():
                    contract = str(raw_contract).strip().upper()
                    if not re.fullmatch(r"[A-Z]+\d{3,4}", contract) or contract in seen:
                        continue
                    seen.add(contract)
                    detail: dict[str, str] = {}
                    try:
                        detail_frame = akshare.futures_contract_detail(symbol=contract)
                        detail = _detail_map(detail_frame)
                    except OutboundPolicyError:
                        raise
                    except Exception:
                        LOG.warning("dce_fallback_contract_detail_missing contract=%s", contract)
                    rows.append(
                        {
                            "品种名称": instrument_name,
                            "合约": contract,
                            "交易单位": _numeric_value(detail.get("交易单位", "")),
                            "最小变动价位": _numeric_value(detail.get("最小变动价位", "")),
                            "开始交易日": detail.get("上市日期", detail.get("上市日", "")),
                            "最后交易日": detail.get("最后交易日", ""),
                        }
                    )
        if not rows:
            raise ValueError("DCE fallback catalog response is empty")
        frame = pd.DataFrame(rows)
        self._dce_catalog_cache[collection_date] = frame
        return frame

    @staticmethod
    def _require_dce(source: ExchangeSource) -> None:
        if source.code != "DCE":
            raise ValueError("fallback is only authorized for DCE")


def _detail_map(frame: pd.DataFrame) -> dict[str, str]:
    if frame is None or frame.empty or not {"item", "value"}.issubset(frame.columns):
        return {}
    return {
        str(row["item"]).strip(): str(row["value"]).strip()
        for _, row in frame.iterrows()
        if pd.notna(row["item"]) and pd.notna(row["value"])
    }


def _numeric_value(value: str) -> str:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", ""))
    return match.group(0) if match else ""


def _normalize_sina_seat_table(
    frame: pd.DataFrame,
    contract: str,
    party_field: str,
    value_field: str,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    rank_column = _find_column(frame, "名次")
    party_column = _find_column(frame, "会员简称", "会员名称")
    value_names = {
        "vol": ("成交量",),
        "long_open_interest": ("多单持仓", "持买单量"),
        "short_open_interest": ("空单持仓", "持卖单量"),
    }[value_field]
    value_column = _find_column(frame, *value_names)
    if not rank_column or not party_column or not value_column:
        return pd.DataFrame()
    result = pd.DataFrame(
        {
            "symbol": contract,
            "rank": frame[rank_column],
            party_field: frame[party_column],
            value_field: frame[value_column],
        }
    )
    return result.dropna(subset=["rank", party_field, value_field])


def _find_column(frame: pd.DataFrame, *names: str) -> Any | None:
    for column in frame.columns:
        label = str(column).strip()
        if label in names:
            return column
    return None


def _resolve_public_host(
    host: str,
    port: int,
    allowed_domains: frozenset[str],
    *,
    resolver: Any | None = None,
) -> frozenset[str]:
    normalized = host.rstrip(".").lower()
    if normalized not in allowed_domains:
        raise OutboundPolicyError("outbound host is not in the exchange whitelist")
    resolver = resolver or socket.getaddrinfo
    try:
        answers = resolver(normalized, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise OutboundPolicyError("exchange host did not resolve") from error
    addresses = frozenset(item[4][0] for item in answers)
    if not addresses:
        raise OutboundPolicyError("exchange host did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise OutboundPolicyError("exchange host resolved to a non-public address")
    return addresses


def _validated_url(url: str, allowed_domains: frozenset[str]) -> tuple[str, int, frozenset[str]]:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise OutboundPolicyError("invalid exchange URL")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise OutboundPolicyError("invalid exchange URL") from error
    normalized = parsed.hostname.rstrip(".").lower()
    return normalized, port, _resolve_public_host(normalized, port, allowed_domains)


def _request_with_dns_fence(
    original: Any,
    session: requests.Session,
    method: str,
    url: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    allowed_domains: frozenset[str],
) -> requests.Response:
    host, port, approved_addresses = _validated_url(url, allowed_domains)
    original_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(query_host: str, query_port: Any, *dns_args: Any, **dns_kwargs: Any):
        answers = original_getaddrinfo(query_host, query_port, *dns_args, **dns_kwargs)
        normalized_query = str(query_host).rstrip(".").lower()
        if normalized_query == host:
            actual_addresses = frozenset(item[4][0] for item in answers)
            if actual_addresses != approved_addresses:
                raise OutboundPolicyError("exchange host DNS answer changed before connection")
            for address in actual_addresses:
                if not ipaddress.ip_address(address).is_global:
                    raise OutboundPolicyError(
                        "exchange host resolved to a non-public address before connection"
                    )
        return answers

    # Re-resolve immediately before handing control to urllib3, then keep the
    # same guard around every resolver call made while the socket is opened.
    # The lock prevents another request thread from observing the temporary
    # process-wide socket resolver hook.
    with _DNS_GUARD_LOCK:
        confirmed_addresses = _resolve_public_host(
            host,
            port,
            allowed_domains,
            resolver=original_getaddrinfo,
        )
        if confirmed_addresses != approved_addresses:
            raise OutboundPolicyError("exchange host DNS answer changed before connection")
        socket.getaddrinfo = guarded_getaddrinfo
        try:
            return original(session, method, url, *args, **kwargs)
        finally:
            socket.getaddrinfo = original_getaddrinfo


def _redirect_method(status_code: int, method: str) -> str:
    normalized = method.upper()
    if status_code == 303 and normalized != "HEAD":
        return "GET"
    if status_code in {301, 302} and normalized == "POST":
        return "GET"
    return normalized


@contextlib.contextmanager
def official_requests_only(allowed_domains: frozenset[str]) -> Iterator[None]:
    original = requests.sessions.Session.request

    def guarded(session: requests.Session, method: str, url: str, *args: Any, **kwargs: Any):
        request_kwargs = dict(kwargs)
        request_kwargs["allow_redirects"] = False
        current_url = url
        current_method = method.upper()
        history: list[requests.Response] = []
        for redirect_count in range(MAX_REDIRECTS + 1):
            response = _request_with_dns_fence(
                original,
                session,
                current_method,
                current_url,
                args,
                request_kwargs,
                allowed_domains,
            )
            location = response.headers.get("location")
            if response.status_code not in REDIRECT_STATUS_CODES or not location:
                response.history = history
                return response
            if redirect_count == MAX_REDIRECTS:
                response.close()
                raise OutboundPolicyError("exchange redirect limit exceeded")
            next_url = urljoin(current_url, location)
            # Validate the next hop before even preparing its request. Its DNS
            # result is checked again immediately before the socket is opened.
            _validated_url(next_url, allowed_domains)
            history.append(response)
            response.close()
            next_method = _redirect_method(response.status_code, current_method)
            if next_method == "GET" and current_method != "GET":
                for field in ("data", "json", "files"):
                    request_kwargs.pop(field, None)
                headers = dict(request_kwargs.get("headers") or {})
                for field in ("Content-Length", "Content-Type", "Transfer-Encoding"):
                    headers.pop(field, None)
                    headers.pop(field.lower(), None)
                request_kwargs["headers"] = headers
            current_method = next_method
            current_url = next_url
        raise OutboundPolicyError("exchange redirect limit exceeded")

    requests.sessions.Session.request = guarded
    try:
        yield
    finally:
        requests.sessions.Session.request = original
