import logging

import httpx

from futures_collector import cli
from futures_collector.api import PlatformRequestError


def test_startup_failure_logs_only_exception_type(monkeypatch, caplog) -> None:
    def fail_to_load_credentials():
        raise PermissionError("must-not-appear")

    monkeypatch.setattr(cli, "load_credentials", fail_to_load_credentials)

    with caplog.at_level(logging.ERROR):
        result = cli.main(["--date", "2026-07-31", "--exchange", "DCE", "--dataset", "market"])

    assert result == 1
    assert "collector_start_failed error=PermissionError" in caplog.text
    assert "must-not-appear" not in caplog.text


def test_startup_platform_failure_logs_only_safe_stage_status_and_code(monkeypatch, caplog) -> None:
    class FailingPlatformClient:
        def __init__(self, _credentials) -> None:
            pass

        def __enter__(self):
            response = httpx.Response(
                401,
                json={"data": {"code": "unauthorized"}, "password": "must-not-appear"},
            )
            raise PlatformRequestError("login", response)

        def __exit__(self, *_args) -> None:
            pass

    monkeypatch.setattr(cli, "load_credentials", lambda: object())
    monkeypatch.setattr(cli, "PlatformClient", FailingPlatformClient)

    with caplog.at_level(logging.ERROR):
        result = cli.main(["--date", "2026-07-31", "--exchange", "DCE", "--dataset", "market"])

    assert result == 1
    assert "collector_start_failed error=login:401:unauthorized" in caplog.text
    assert "must-not-appear" not in caplog.text
