from __future__ import annotations

import contextlib
import ipaddress
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import akshare
import requests


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


class AkshareAdapter:
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


def _validate_public_host(host: str, allowed_domains: frozenset[str]) -> None:
    normalized = host.rstrip(".").lower()
    if normalized not in allowed_domains:
        raise ValueError("outbound host is not in the exchange whitelist")
    addresses = {item[4][0] for item in socket.getaddrinfo(normalized, None)}
    if not addresses:
        raise ValueError("exchange host did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("exchange host resolved to a non-public address")


@contextlib.contextmanager
def official_requests_only(allowed_domains: frozenset[str]) -> Iterator[None]:
    original = requests.sessions.Session.request

    def guarded(session: requests.Session, method: str, url: str, *args: Any, **kwargs: Any):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid exchange URL")
        _validate_public_host(parsed.hostname, allowed_domains)
        response = original(session, method, url, *args, **kwargs)
        final = urlsplit(response.url)
        if not final.hostname:
            raise ValueError("invalid exchange redirect")
        _validate_public_host(final.hostname, allowed_domains)
        return response

    requests.sessions.Session.request = guarded
    try:
        yield
    finally:
        requests.sessions.Session.request = original
