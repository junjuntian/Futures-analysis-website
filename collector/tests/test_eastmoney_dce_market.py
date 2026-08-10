from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from futures_collector import sources as sources_module
from futures_collector.runner import CollectionRunner
from futures_collector.sources import (
    EASTMONEY_DCE_SOURCE_CODE,
    SOURCES,
    AkshareAdapter,
    DatasetCompletenessError,
    _attributable_settlement,
    _eastmoney_scaled,
)

DAY = date(2026, 8, 7)
NEXT = date(2026, 8, 10)


def bars(**closes):
    return {
        date.fromisoformat(day): {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": "10",
            "turnover": "100",
        }
        for day, close in closes.items()
    }


class FakeEastmoney:
    """Stands in for the three endpoints, recording what was asked of them."""

    def __init__(self, varieties=None, klines=None, quotes=None):
        self.varieties = varieties if varieties is not None else [("JM", "10", "焦煤")]
        self.klines = klines or {}
        self.quotes = quotes or {}
        self.requests = []

    def install(self, monkeypatch):
        monkeypatch.setattr(sources_module, "official_requests_only", lambda d: _noop())

        def fake_json(_self, url, params):
            self.requests.append((url, dict(params)))
            if url == sources_module.EASTMONEY_CONTRACT_TABLE:
                msgid = str(params["msgid"])
                if "_" not in msgid:
                    return [
                        {"vcode": code, "vtype": vtype, "vname": name}
                        for code, vtype, name in self.varieties
                    ]
                vtype = msgid.split("_", 1)[1]
                name = next(n for _, t, n in self.varieties if t == vtype)
                code = next(c for c, t, _ in self.varieties if t == vtype)
                return [
                    {"code": contract.lower(), "name": name, "vcode": code.lower()}
                    for contract in self.klines
                    if contract.upper().startswith(code)
                ] + [{"code": code.lower(), "name": name}]  # the continuous pseudo-code
            contract = str(params["secid"]).split(".", 1)[1].upper()
            if url == sources_module.EASTMONEY_KLINE_ENDPOINT:
                lines = [
                    f"{d.isoformat()},{b['open']},{b['close']},{b['high']},{b['low']},"
                    f"{b['volume']},{b['turnover']}"
                    for d, b in sorted(self.klines.get(contract, {}).items())
                ]
                return {"data": {"klines": lines}}
            return {"data": self.quotes.get(contract, {})}

        monkeypatch.setattr(AkshareAdapter, "_eastmoney_json", fake_json)


def _noop():
    import contextlib

    @contextlib.contextmanager
    def ctx():
        yield

    return ctx()


def test_the_field_list_keeps_its_commas(monkeypatch) -> None:
    # The request goes out shaped like the upstream's own: `fields1=f1,f2,f3`,
    # not percent-encoded. The endpoint accepts either -- this pins the shape,
    # it does not paper over a rejection.
    sent = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {}

    def fake_get(url, params=None, headers=None, timeout=None):
        sent["url"] = url
        sent["params"] = params
        return Response()

    monkeypatch.setattr(sources_module.requests, "get", fake_get)
    AkshareAdapter()._eastmoney_json(
        sources_module.EASTMONEY_KLINE_ENDPOINT,
        {"secid": "114.jm2609", "fields1": "f1,f2,f3"},
    )
    assert isinstance(sent["params"], str), "a dict would let requests encode the commas"
    assert "fields1=f1,f2,f3" in sent["params"]
    assert "%2C" not in sent["params"]
    # The dot in a secid must survive too.
    assert "secid=114.jm2609" in sent["params"]


def test_a_connection_abort_is_retried_for_that_request_alone(monkeypatch) -> None:
    # The candle endpoint answers a too-frequent request by closing the
    # connection with no status line, so it cannot be told apart from a dead
    # host except by trying again. Retrying the one request matters: letting the
    # runner retry the dataset would re-crawl all thirty contracts because one
    # of them stumbled.
    monkeypatch.setattr(sources_module, "EASTMONEY_MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(sources_module, "EASTMONEY_RETRY_BACKOFF_SECONDS", 0)
    attempts = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    def flaky(url, params=None, headers=None, timeout=None):
        attempts.append(url)
        if len(attempts) < 3:
            raise sources_module.requests.ConnectionError("Connection aborted.")
        return Response()

    monkeypatch.setattr(sources_module.requests, "get", flaky)
    assert AkshareAdapter()._eastmoney_json("https://push2his.eastmoney.com/x", {}) == {"ok": True}
    assert len(attempts) == 3


def test_a_host_the_allowlist_refuses_is_not_retried(monkeypatch) -> None:
    # Retrying a policy refusal cannot change it, and would bury the reason
    # under three more identical failures.
    monkeypatch.setattr(sources_module, "EASTMONEY_MIN_REQUEST_INTERVAL_SECONDS", 0)
    attempts = []

    def refused(url, params=None, headers=None, timeout=None):
        attempts.append(url)
        raise sources_module.OutboundPolicyError("outbound host is not in the exchange whitelist")

    monkeypatch.setattr(sources_module.requests, "get", refused)
    with pytest.raises(sources_module.OutboundPolicyError):
        AkshareAdapter()._eastmoney_json("https://evil.example/x", {})
    assert len(attempts) == 1


def test_a_price_is_put_back_on_its_own_scale() -> None:
    # 焦煤 quotes to one decimal, so 12555 is 1255.5; 沪铜 quotes to none, so
    # 108020 is 108020. Reading both as integers is right for one and wrong by a
    # factor of ten for the other -- and looks entirely normal either way.
    assert _eastmoney_scaled(12555, 1) == "1255.5"
    assert _eastmoney_scaled(108020, 0) == "108020"
    assert _eastmoney_scaled(None, 1) == ""
    assert _eastmoney_scaled("-", 1) == ""
    # A missing decimal count is not an excuse to assume zero.
    assert _eastmoney_scaled(12555, None) == ""


def test_settlement_is_attributed_only_when_two_readings_agree() -> None:
    quote = {"settlement": "1255.5", "previous_close": "1267.5"}
    recent = bars(**{"2026-08-06": "1240.0", "2026-08-07": "1267.5", "2026-08-10": "1274.5"})

    # 08-07 is the session the quote is reporting: it is one of the last two on
    # record and its close is the close the quote gives.
    assert _attributable_settlement(DAY, recent, quote) == "1255.5"

    # 08-06 is further back. Its settlement is not the one in this quote, and
    # writing it there would be wrong in every number computed from it.
    assert _attributable_settlement(date(2026, 8, 6), recent, quote) is None

    # The date is recent enough but the closes disagree, so the quote is
    # reporting some other session.
    disagreeing = bars(**{"2026-08-07": "1200.0", "2026-08-10": "1274.5"})
    assert _attributable_settlement(DAY, disagreeing, quote) is None

    # Nothing to attribute.
    assert _attributable_settlement(DAY, recent, {}) is None


def test_a_day_is_assembled_from_the_candles_and_the_quote(monkeypatch) -> None:
    fake = FakeEastmoney(
        klines={
            "JM2609": bars(**{"2026-08-07": "1267.5", "2026-08-10": "1274.5"}),
        },
        quotes={"JM2609": {"f59": 1, "f60": 12675, "f130": 12555}},
    )
    fake.install(monkeypatch)
    frame = AkshareAdapter().eastmoney_dce_market(DAY, frozenset({"JM"}))
    row = frame.iloc[0]
    assert row["symbol"] == "JM2609"
    assert row["date"] == "2026-08-07"
    assert row["close"] == "1267.5"
    # The settlement the whole source exists for.
    assert row["settle"] == "1255.5"


def test_continuous_pseudo_codes_never_become_contracts(monkeypatch) -> None:
    # The contract table also carries jm / jmm / jms, which name no contract the
    # exchange ever listed. Admitting them would invent instruments.
    fake = FakeEastmoney(
        klines={"JM2609": bars(**{"2026-08-07": "1267.5", "2026-08-10": "1274.5"})},
        quotes={"JM2609": {"f59": 1, "f60": 12675, "f130": 12555}},
    )
    fake.install(monkeypatch)
    catalog = AkshareAdapter().eastmoney_dce_catalog(DAY, frozenset({"JM"}))
    assert list(catalog["合约"]) == ["JM2609"]


def test_a_day_with_no_attributable_settlement_anywhere_is_refused(monkeypatch) -> None:
    # One contract failing the proof is ordinary. Every contract failing it means
    # the run is not one session behind the quote, so the whole day would land
    # with no settlement at all -- and seat cost is computed from settlement.
    fake = FakeEastmoney(
        klines={"JM2609": bars(**{"2026-08-07": "1267.5", "2026-08-10": "1274.5"})},
        quotes={"JM2609": {"f59": 1, "f60": 99999, "f130": 12555}},
    )
    fake.install(monkeypatch)
    with pytest.raises(DatasetCompletenessError):
        AkshareAdapter().eastmoney_dce_market(DAY, frozenset({"JM"}))


def test_a_contract_that_did_not_trade_is_absent_not_zero(monkeypatch) -> None:
    fake = FakeEastmoney(
        klines={
            "JM2609": bars(**{"2026-08-07": "1267.5", "2026-08-10": "1274.5"}),
            # Listed, but no candle for the date being collected.
            "JM2612": bars(**{"2026-08-10": "1300.0"}),
        },
        quotes={
            "JM2609": {"f59": 1, "f60": 12675, "f130": 12555},
            "JM2612": {"f59": 1, "f60": 13000, "f130": 12990},
        },
    )
    fake.install(monkeypatch)
    frame = AkshareAdapter().eastmoney_dce_market(DAY, frozenset({"JM"}))
    assert list(frame["symbol"]) == ["JM2609"]


def test_a_variety_is_narrowed_before_any_contract_is_fetched(monkeypatch) -> None:
    # Every contract costs two requests, so narrowing after the crawl would pay
    # for varieties nobody asked for.
    fake = FakeEastmoney(
        varieties=[("JM", "10", "焦煤"), ("JD", "14", "鸡蛋")],
        klines={
            "JM2609": bars(**{"2026-08-07": "1267.5", "2026-08-10": "1274.5"}),
            "JD2609": bars(**{"2026-08-07": "3400.0", "2026-08-10": "3410.0"}),
        },
        quotes={"JM2609": {"f59": 1, "f60": 12675, "f130": 12555}},
    )
    fake.install(monkeypatch)
    AkshareAdapter().eastmoney_dce_market(DAY, frozenset({"JM"}))
    fetched = {
        params["secid"]
        for url, params in fake.requests
        if url != sources_module.EASTMONEY_CONTRACT_TABLE
    }
    assert fetched == {"114.jm2609"}


def test_the_quote_source_carries_no_seats(monkeypatch) -> None:
    runner = CollectionRunner(AkshareAdapter(), SimpleNamespace(), retry_delay_seconds=0)
    with pytest.raises(ValueError, match="no seat rankings"):
        runner._collect(SOURCES["DCE"], DAY, "seats", datetime.now(UTC))


def test_the_source_reaches_only_eastmoney_hosts() -> None:
    source = SOURCES["DCE"]
    assert source.source_code == EASTMONEY_DCE_SOURCE_CODE
    assert all(host.endswith(".eastmoney.com") for host in source.domains)
    # The exchange's own hosts are gone: they answer 412 to every client, and
    # nothing should be able to reach them from here by accident.
    assert not any("dce.com.cn" in host for host in source.domains)
    # The delayed-quote host belongs in the allowlist because requests from
    # outside the mainland are redirected to it mid-call.
    assert "push2delay.eastmoney.com" in source.domains
