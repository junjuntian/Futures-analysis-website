from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import akshare
import pandas as pd
import requests

LOG = logging.getLogger("futures_collector.sources")
MAX_REDIRECTS = 5
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
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


EASTMONEY_DCE_SOURCE_CODE = "eastmoney_dce_market"
# DCE's own endpoints have answered HTTP 412 to every client since 2026-08-02
# (`DEC-041`) and the Sina fallback chosen then covers only 44% of the contracts
# the exchange lists -- 105 of 186 on 2026-08-07 had no history at Sina at all,
# including the 生猪 contracts this platform needs. A fallback that can never be
# complete is worse than no fallback, so `DEC-045` removes both and reads DCE
# from Eastmoney instead.
#
# `DEC-041` rejected Eastmoney for market data because it "lacks the settlement
# price". That was true of `futures_hist_em`, which is a candlestick endpoint --
# candlesticks carry no settlement anywhere. The quote snapshot does carry it,
# in field f130, verified against the SHFE official settlement for 2026-08-07 on
# cu2609/cu2610/cu2612/cu2703, all four equal.
EASTMONEY_DCE_DOMAINS = frozenset(
    {
        "futsse-static.eastmoney.com",
        "push2.eastmoney.com",
        # Requests from outside the mainland are redirected here, so the
        # delayed-quote host is part of the same call, not an alternative to it.
        "push2delay.eastmoney.com",
        "push2his.eastmoney.com",
    }
)

SOURCES: dict[str, ExchangeSource] = {
    "DCE": ExchangeSource(
        "DCE",
        EASTMONEY_DCE_SOURCE_CODE,
        "大连商品交易所",
        EASTMONEY_DCE_DOMAINS,
        # Nothing here goes through akshare: it has no Eastmoney futures quote
        # function, and its DCE functions all target the WAF-blocked endpoints.
        "",
        "",
        "",
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

# Eastmoney publishes the same member-level 龙虎榜 the exchanges do, but as one
# report covering every market, refreshed earlier than the exchange files. It is
# a seats-only source: `RPT_FUTU_DAILYPOSITION` carries no settlement price and
# no contract list, so it can never stand in for market data or a catalog.
EASTMONEY_CONTRACT_TABLE = "https://futsse-static.eastmoney.com/redis"
EASTMONEY_QUOTE_ENDPOINT = "https://push2.eastmoney.com/api/qt/stock/get"
EASTMONEY_KLINE_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# The candlestick endpoint resets the connection without this token; the quote
# endpoint does not need it. It is a public constant embedded in Eastmoney's own
# page scripts, not a credential.
EASTMONEY_UT = "fa5fd1943c7b386f172d6893dbfba10b"
EASTMONEY_DCE_MARKET_ID = "114"
# Prices arrive as integers scaled by the instrument's own decimal count, which
# the quote reports in f59: 焦煤 is 1, so 12555 means 1255.5, while 沪铜 is 0 and
# 108020 means 108020. Ignoring it would be wrong by a factor of ten on some
# varieties and right on others, which is the kind of error that looks fine.
EASTMONEY_QUOTE_FIELDS = "f57,f58,f59,f43,f44,f45,f46,f47,f48,f60,f112,f130"
EASTMONEY_KLINE_FIELDS_1 = "f1,f2,f3,f4,f5,f6"
EASTMONEY_KLINE_FIELDS_2 = "f51,f52,f53,f54,f55,f56,f57"
# How many candles to ask for. A daily collection needs the last few; the limit
# exists so a malformed date cannot ask for a contract's entire life every run.
EASTMONEY_KLINE_LIMIT = 40
# One request per contract for the quote plus one for the candles. Our three DCE
# varieties list about thirty contracts, so a day costs roughly sixty requests --
# against the 186-per-day, half-empty Sina crawl this replaces.
#
# The candle endpoint answers too-frequent requests by closing the connection
# with no status line, which arrives as a transport error and looks exactly like
# the host being down. Measured on the production VPS: at 0.2s every run died
# within a few contracts; at 1.5-2s, five consecutive requests succeeded and one
# in six still aborted. So it is paced at a second and retried, rather than paced
# alone -- pacing that made the aborts rare would still lose a whole day to one.
EASTMONEY_MIN_REQUEST_INTERVAL_SECONDS = 1.0
EASTMONEY_REQUEST_ATTEMPTS = 4
EASTMONEY_RETRY_BACKOFF_SECONDS = 2.0

# The exchange's own annual history files, which its site offers for every year
# back to 2006 and which carry the settlement price the aggregators do not. They
# are the authoritative record for the years no live endpoint will serve: DCE's
# API has answered 412 to every client since 2026-08-02, Sina keeps delisted
# contracts only back to about 2018-09, and Eastmoney keeps none at all.
#
# The files are not fetched here. The site's WAF refuses scripted clients and
# writing code to defeat that is out of the question, so they were fetched once
# through the operator's browser at their instruction and are read from disk.
# That is sound for this data in a way it would not be for daily collection:
# history does not change, so a one-time capture stays correct forever.
DCE_HISTORY_SOURCE_CODE = "dce_official_history"
DCE_HISTORY_DIR_ENV = "FUTURES_DCE_HISTORY_DIR"
# Columns as the exchange names them, mapped to what the normalizer already
# reads. Renaming here keeps the normalizer free of a per-source special case.
DCE_HISTORY_COLUMNS = {
    "合约名称": "合约",
    "交易日期": "交易日期",
    "收盘价": "今收盘",
    "结算价": "今结算",
    "商品名称": "品种名称",
}

DCE_HISTORY_SOURCE = ExchangeSource(
    "DCE",
    DCE_HISTORY_SOURCE_CODE,
    "大连商品交易所",
    # No domains: this source never opens a socket.
    frozenset(),
    "",
    "",
    "",
    False,
)

EASTMONEY_SEATS_SOURCE_CODE = "eastmoney_seats_fallback"
EASTMONEY_SEATS_DOMAINS = frozenset({"datacenter-web.eastmoney.com"})
EASTMONEY_SEATS_ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EASTMONEY_SEATS_REPORT = "RPT_FUTU_DAILYPOSITION"
# The report serves six markets. INE is deliberately absent: it is not one of
# the five exchanges this platform catalogues, and admitting its contracts would
# invent instruments no catalog dataset ever declared.
EASTMONEY_TRADE_MARKET_CODES = {
    "069001005": "SHFE",
    "069001007": "DCE",
    "069001008": "CZCE",
    "069001009": "CFFEX",
    "069001021": "GFEX",
}
# The endpoint silently caps pageSize at 500 however much is asked for, and a
# full trading day is roughly 10k rows, so the page budget is the real guard
# against an unbounded crawl.
EASTMONEY_PAGE_SIZE = 500
EASTMONEY_MAX_PAGES = 80
# The report uses 9999 in a rank column to mean "this member is not ranked for
# that measure", not "rank 9999".
EASTMONEY_UNRANKED = 9999
EASTMONEY_RANK_KINDS = (
    ("VOLUME_RANK", "VOLUME", "vol_party_name", "vol"),
    ("LP_RANK", "LONG_POSITION", "long_party_name", "long_open_interest"),
    ("SP_RANK", "SHORT_POSITION", "short_party_name", "short_open_interest"),
)


def eastmoney_seats_source(exchange_code: str) -> ExchangeSource:
    official = SOURCES[exchange_code]
    # The akshare function names are empty on purpose: nothing about this source
    # goes through akshare, which has no Eastmoney futures seat function at all.
    return ExchangeSource(
        exchange_code,
        EASTMONEY_SEATS_SOURCE_CODE,
        official.name,
        EASTMONEY_SEATS_DOMAINS,
        "",
        "",
        "",
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
        self._eastmoney_seat_cache: dict[date, list[dict[str, Any]]] = {}
        # Eastmoney's DCE variety table, and one entry per contract for the two
        # readings a market row needs, so a contract crosses the network once
        # for its candles and once for its quote however many datasets ask.
        self._eastmoney_dce_varieties: dict[str, tuple[str, str]] | None = None
        self._eastmoney_kline_cache: dict[str, dict[date, dict[str, str]]] = {}
        self._eastmoney_quote_cache: dict[str, dict[str, str]] = {}
        self._eastmoney_next_request_at = 0.0
        # One year of one variety per entry, parsed once and shared by the
        # catalog and market datasets across every date in that year.
        self._dce_history_cache: dict[tuple[str, int], pd.DataFrame] = {}

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

    def eastmoney_seats(
        self,
        source: ExchangeSource,
        collection_date: date,
        varieties: frozenset[str] | None = None,
    ) -> dict[str, pd.DataFrame]:
        if source.source_code != EASTMONEY_SEATS_SOURCE_CODE:
            raise ValueError("eastmoney_seats requires the Eastmoney seat source")
        rows = self._eastmoney_daily_position(collection_date)
        selected = [
            row
            for row in rows
            if EASTMONEY_TRADE_MARKET_CODES.get(str(row.get("TRADE_MARKET_CODE") or ""))
            == source.code
        ]
        if varieties is not None:
            selected = [
                row
                for row in selected
                if _contract_instrument(str(row.get("SECURITY_CODE") or "")) in varieties
            ]
        if not selected:
            raise ValueError("Eastmoney seat response has no rows for this exchange")
        return _pivot_eastmoney_seats(selected)

    def _eastmoney_daily_position(self, collection_date: date) -> list[dict[str, Any]]:
        cached = self._eastmoney_seat_cache.get(collection_date)
        if cached is not None:
            return cached
        rows: list[dict[str, Any]] = []
        with official_requests_only(EASTMONEY_SEATS_DOMAINS):
            page = 1
            while page <= EASTMONEY_MAX_PAGES:
                response = requests.get(
                    EASTMONEY_SEATS_ENDPOINT,
                    params={
                        "reportName": EASTMONEY_SEATS_REPORT,
                        "columns": "ALL",
                        # TYPE 0 is the 成交持仓龙虎榜 the exchanges publish; the
                        # report multiplexes nine other tables onto the same
                        # report name.
                        "filter": (f"(TRADE_DATE='{collection_date.isoformat()}')(TYPE=\"0\")"),
                        "sortTypes": 1,
                        "sortColumns": "SECURITY_CODE",
                        "pageNumber": page,
                        "pageSize": EASTMONEY_PAGE_SIZE,
                        "source": "WEB",
                        "client": "WEB",
                    },
                    headers={"Referer": "https://data.eastmoney.com/"},
                    # The outbound guard supplies a default, but relying on it
                    # means a future call made outside that guard would hang
                    # forever with nothing to say so.
                    timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Eastmoney seat response is not an object")
                # 9201 is the report's own "no rows" code and arrives with
                # success=false, so it has to be told apart from a real failure.
                if payload.get("code") == 9201:
                    break
                result = payload.get("result")
                if not payload.get("success") or not isinstance(result, dict):
                    raise ValueError("Eastmoney seat response was not successful")
                page_rows = result.get("data")
                if not isinstance(page_rows, list):
                    raise ValueError("Eastmoney seat response has no data array")
                rows.extend(row for row in page_rows if isinstance(row, dict))
                total_pages = result.get("pages")
                if not isinstance(total_pages, int) or page >= total_pages:
                    break
                page += 1
            else:
                # Falling out of the loop means the report claims more pages
                # than the budget allows. Refuse rather than submit a silently
                # truncated day.
                raise ValueError("Eastmoney seat response exceeded the page budget")
        self._eastmoney_seat_cache[collection_date] = rows
        return rows

    def eastmoney_dce_catalog(
        self, collection_date: date, varieties: frozenset[str] | None
    ) -> pd.DataFrame:
        """The contracts DCE lists, as Eastmoney's own contract table gives them.

        Read rather than derived. The Sina path this replaces had to guess the
        listed set from the date, because Sina's endpoints take no date, and it
        overshot on purpose: every guess that was never listed cost a request to
        find out. Eastmoney publishes the list, so there is nothing to guess.
        """
        rows = [
            {"品种名称": name, "合约": contract}
            for contract, name in self._eastmoney_dce_contracts(varieties)
        ]
        if not rows:
            raise ValueError("Eastmoney lists no DCE contracts for the requested varieties")
        return pd.DataFrame(rows)

    def eastmoney_dce_market(
        self, collection_date: date, varieties: frozenset[str] | None
    ) -> pd.DataFrame:
        """One row per contract that traded on the date, settlement included.

        Two readings per contract, because neither alone is a complete day: the
        candles carry a dated open/high/low/close/volume for any past session but
        no settlement, and the quote carries the settlement of the most recent
        completed session without saying which session that was.

        Attributing that settlement therefore has to be proved, not assumed. A
        settlement written against the wrong day is invisible in the data and
        wrong in every number computed from it, so an unproved one is left out.
        """
        cache_key = (collection_date, varieties)
        cached = self._dce_market_cache.get(cache_key)
        if cached is not None:
            return cached.copy()
        contracts = self._eastmoney_dce_contracts(varieties)
        rows: list[dict[str, Any]] = []
        without_settlement: list[str] = []
        with official_requests_only(EASTMONEY_DCE_DOMAINS):
            for contract, _ in contracts:
                bars = self._eastmoney_klines(contract)
                bar = bars.get(collection_date)
                if bar is None:
                    # Not listed yet, or it did not trade. Either way the
                    # exchange published no row for it that day.
                    continue
                quote = self._eastmoney_quote(contract)
                settlement = _attributable_settlement(collection_date, bars, quote)
                if settlement is None:
                    without_settlement.append(contract)
                rows.append(
                    {
                        "symbol": contract,
                        "date": collection_date.isoformat(),
                        "open": bar["open"],
                        "high": bar["high"],
                        "low": bar["low"],
                        "close": bar["close"],
                        "settle": "" if settlement is None else settlement,
                        "volume": bar["volume"],
                        "turnover": bar["turnover"],
                    }
                )
        if not rows:
            raise ValueError(f"Eastmoney published no DCE rows for {collection_date.isoformat()}")
        if len(without_settlement) == len(rows):
            # Every contract failing the same proof is not thirty coincidences:
            # it means this run is not one session behind the quote, so the day
            # would land with no settlement anywhere. Seat cost is computed from
            # settlement, so that is a failed collection, not a partial one.
            raise DatasetCompletenessError("market", len(rows), len(rows))
        if without_settlement:
            LOG.error(
                "eastmoney_dce_settlement_unattributable count=%d of=%d contracts=%s",
                len(without_settlement),
                len(rows),
                ",".join(without_settlement[:10]),
            )
        frame = pd.DataFrame(rows)
        self._dce_market_cache[cache_key] = frame
        return frame.copy()

    def _eastmoney_dce_contracts(self, varieties: frozenset[str] | None) -> list[tuple[str, str]]:
        """(contract code, variety name) pairs, one request per variety."""
        if self._eastmoney_dce_varieties is None:
            with official_requests_only(EASTMONEY_DCE_DOMAINS):
                payload = self._eastmoney_json(
                    EASTMONEY_CONTRACT_TABLE, {"msgid": EASTMONEY_DCE_MARKET_ID}
                )
            if not isinstance(payload, list) or not payload:
                raise ValueError("Eastmoney DCE variety table is empty")
            self._eastmoney_dce_varieties = {
                str(item.get("vcode") or "").upper(): (
                    str(item.get("vtype") or ""),
                    str(item.get("vname") or ""),
                )
                for item in payload
                if isinstance(item, dict) and item.get("vcode") and item.get("vtype")
            }
        known = self._eastmoney_dce_varieties
        wanted = sorted(known if varieties is None else set(varieties) & set(known))
        pairs: list[tuple[str, str]] = []
        with official_requests_only(EASTMONEY_DCE_DOMAINS):
            for symbol in wanted:
                vtype, vname = known[symbol]
                payload = self._eastmoney_json(
                    EASTMONEY_CONTRACT_TABLE,
                    {"msgid": f"{EASTMONEY_DCE_MARKET_ID}_{vtype}"},
                )
                if not isinstance(payload, list):
                    raise ValueError(f"Eastmoney contract list for {symbol} is not an array")
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    code = str(item.get("code") or "").strip().lower()
                    # The table also carries continuous pseudo-codes -- jm, jmm,
                    # jms -- which name no contract the exchange ever listed.
                    if not re.fullmatch(r"[a-z]+\d{3,4}", code):
                        continue
                    pairs.append((code.upper(), vname or symbol))
        return pairs

    def _eastmoney_klines(self, contract: str) -> dict[date, dict[str, str]]:
        cached = self._eastmoney_kline_cache.get(contract)
        if cached is not None:
            return cached
        payload = self._eastmoney_json(
            EASTMONEY_KLINE_ENDPOINT,
            {
                "secid": f"{EASTMONEY_DCE_MARKET_ID}.{contract.lower()}",
                # Without this the endpoint resets the connection rather than
                # answering. It is a constant from Eastmoney's own page scripts.
                "ut": EASTMONEY_UT,
                "klt": 101,
                "fqt": 1,
                "beg": 0,
                "end": "20500101",
                "lmt": EASTMONEY_KLINE_LIMIT,
                "fields1": EASTMONEY_KLINE_FIELDS_1,
                "fields2": EASTMONEY_KLINE_FIELDS_2,
            },
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        bars: dict[date, dict[str, str]] = {}
        for line in (data or {}).get("klines") or []:
            parts = str(line).split(",")
            if len(parts) < 7:
                continue
            try:
                bar_date = date.fromisoformat(parts[0])
            except ValueError:
                continue
            bars[bar_date] = {
                "open": parts[1],
                "close": parts[2],
                "high": parts[3],
                "low": parts[4],
                "volume": parts[5],
                "turnover": parts[6],
            }
        self._eastmoney_kline_cache[contract] = bars
        return bars

    def _eastmoney_quote(self, contract: str) -> dict[str, str]:
        cached = self._eastmoney_quote_cache.get(contract)
        if cached is not None:
            return cached
        payload = self._eastmoney_json(
            EASTMONEY_QUOTE_ENDPOINT,
            {
                "secid": f"{EASTMONEY_DCE_MARKET_ID}.{contract.lower()}",
                "fields": EASTMONEY_QUOTE_FIELDS,
            },
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        quote: dict[str, str] = {}
        if isinstance(data, dict):
            quote = {
                "settlement": _eastmoney_scaled(data.get("f130"), data.get("f59")),
                "previous_close": _eastmoney_scaled(data.get("f60"), data.get("f59")),
            }
        self._eastmoney_quote_cache[contract] = quote
        return quote

    def _eastmoney_json(self, url: str, params: dict[str, Any]) -> Any:
        """One paced request, retried through the endpoint's connection aborts.

        The abort is not a status code -- the connection closes with no status
        line -- so it cannot be told apart from an unreachable host by anything
        except trying again. Retrying here rather than letting the runner retry
        the dataset matters: one flaky contract out of thirty would otherwise
        re-crawl all thirty.
        """
        last_error: Exception | None = None
        for attempt in range(1, EASTMONEY_REQUEST_ATTEMPTS + 1):
            now = time.monotonic()
            if now < self._eastmoney_next_request_at:
                time.sleep(self._eastmoney_next_request_at - now)
            self._eastmoney_next_request_at = (
                time.monotonic() + EASTMONEY_MIN_REQUEST_INTERVAL_SECONDS
            )
            try:
                response = requests.get(
                    url,
                    # Pre-encoded so the field lists keep their literal commas,
                    # which is the shape Eastmoney's own page scripts send.
                    params=_eastmoney_query(params),
                    headers={
                        "Referer": "https://quote.eastmoney.com/",
                        "User-Agent": "Mozilla/5.0",
                    },
                    timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                return response.json()
            except OutboundPolicyError:
                # The allowlist refused the host. Retrying cannot change that,
                # and retrying a policy failure would obscure it.
                raise
            except requests.RequestException as error:
                last_error = error
                LOG.warning(
                    "eastmoney_request_retry attempt=%d of=%d error=%s",
                    attempt,
                    EASTMONEY_REQUEST_ATTEMPTS,
                    type(error).__name__,
                )
                if attempt < EASTMONEY_REQUEST_ATTEMPTS:
                    time.sleep(EASTMONEY_RETRY_BACKOFF_SECONDS * attempt)
        raise last_error if last_error else RuntimeError("eastmoney request loop fell through")

    def dce_history_frame(
        self, collection_date: date, varieties: frozenset[str] | None
    ) -> pd.DataFrame:
        """Rows for one date, read from the exchange's annual files on disk."""
        directory = os.environ.get(DCE_HISTORY_DIR_ENV)
        if not directory:
            raise ValueError(f"{DCE_HISTORY_DIR_ENV} is not set")
        root = Path(directory)
        wanted = sorted(varieties) if varieties is not None else None
        if not wanted:
            # Without a selection there is no way to know which files to open;
            # the directory holds one per variety and year.
            raise ValueError("the DCE history source requires a variety selection")
        frames: list[pd.DataFrame] = []
        missing: list[str] = []
        for symbol in wanted:
            key = (symbol.upper(), collection_date.year)
            frame = self._dce_history_cache.get(key)
            if frame is None:
                path = root / f"{symbol.lower()}_{collection_date.year}.xlsx"
                if not path.is_file():
                    missing.append(path.name)
                    continue
                frame = pd.read_excel(path, dtype=str)
                frame = frame.rename(columns=DCE_HISTORY_COLUMNS)
                self._dce_history_cache[key] = frame
            frames.append(frame)
        if not frames:
            raise ValueError(
                f"no DCE history file for {collection_date.isoformat()}: missing {missing}"
            )
        return pd.concat(frames, ignore_index=True)


def _eastmoney_query(params: dict[str, Any]) -> str:
    """A query string built the way Eastmoney's own page scripts build one.

    The field list goes over the wire as `f1,f2,f3`; handed a dict, requests
    percent-encodes the commas. Measured against the live endpoint, it accepts
    both, so this is not load-bearing -- it is here to keep the request the same
    shape as the upstream's own, which is one less thing to differ later.
    """
    return urlencode({key: str(value) for key, value in params.items()}, safe=",.")


def _eastmoney_scaled(value: Any, decimals: Any) -> str:
    """Eastmoney's integer price, put back on its own scale.

    Prices arrive as integers scaled by the instrument's decimal count, which the
    quote reports alongside them: 焦煤 is 1, so 12555 means 1255.5, while 沪铜 is
    0 and 108020 means 108020. Ignoring the scale would be right on some
    varieties and wrong by a factor of ten on others, which is the kind of error
    that looks perfectly normal in the data.
    """
    if value is None or value == "-" or not isinstance(decimals, int):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number == 0:
        return ""
    scaled = number / (10**decimals)
    return f"{scaled:.{decimals}f}" if decimals > 0 else str(int(scaled))


def _attributable_settlement(
    collection_date: date,
    bars: dict[date, dict[str, str]],
    quote: dict[str, str],
) -> str | None:
    """The quote's settlement, but only when it provably belongs to this date.

    The quote reports the settlement and close of the session before the current
    one, without naming it. So the date being collected has to be shown to be
    that session: it must be one of the last two on record, and its candle close
    must equal the close the quote reports. Two independent readings agreeing is
    what makes the attribution safe; either one alone is a guess.
    """
    settlement = quote.get("settlement")
    previous_close = quote.get("previous_close")
    if not settlement or not previous_close:
        return None
    if collection_date not in sorted(bars)[-2:]:
        return None
    if bars[collection_date]["close"] != previous_close:
        return None
    return settlement


def _contract_instrument(contract: str) -> str:
    match = re.match(r"([A-Za-z]+)", contract.strip())
    return match.group(1).upper() if match else ""


def _pivot_eastmoney_seats(rows: list[dict[str, Any]]) -> dict[str, pd.DataFrame]:
    """Turn Eastmoney's member-centric rows back into the exchange's rank table.

    Eastmoney emits one row per (contract, member) carrying that member's rank
    in all three measures at once. The exchanges — and therefore this
    platform's `seat_positions_v1` shape — publish one row per rank, whose
    volume, long and short columns each name a different member. Rebuild that
    layout so the existing normalizer needs no special case, and so a member
    unranked in one measure (9999) simply leaves that column empty rather than
    inventing a rank.
    """
    by_contract: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        contract = str(row.get("SECURITY_CODE") or "").upper().replace(" ", "")
        if contract:
            by_contract.setdefault(contract, []).append(row)
    tables: dict[str, pd.DataFrame] = {}
    for contract, members in by_contract.items():
        ranked: dict[int, dict[str, Any]] = {}
        for rank_field, value_field, name_column, value_column in EASTMONEY_RANK_KINDS:
            for member in members:
                rank = member.get(rank_field)
                value = member.get(value_field)
                seat_name = member.get("MEMBER_NAME_ABBR")
                if not isinstance(rank, int) or rank == EASTMONEY_UNRANKED:
                    continue
                if value is None or not seat_name:
                    continue
                slot = ranked.setdefault(rank, {"rank": rank, "symbol": contract})
                slot[name_column] = seat_name
                slot[value_column] = value
        if ranked:
            tables[contract] = pd.DataFrame([ranked[rank] for rank in sorted(ranked)])
    if not tables:
        raise ValueError("Eastmoney seat response has no ranked contracts")
    return tables


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
        if request_kwargs.get("timeout") is None:
            request_kwargs["timeout"] = DEFAULT_REQUEST_TIMEOUT_SECONDS
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
