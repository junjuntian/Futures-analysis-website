import importlib.util
import signal
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "collector" / "cffex-daily.py"


def load_module():
    spec = importlib.util.spec_from_file_location("cffex_daily", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="POSIX signal alarm required")
def test_official_request_is_bounded_and_cancels_alarm(monkeypatch):
    module = load_module()
    alarms = []
    received = []
    monkeypatch.setattr(module.signal, "alarm", alarms.append)
    monkeypatch.setattr(
        module.akshare,
        "get_cffex_daily",
        lambda *, date: received.append(date) or "frame",
    )

    result = module.fetch_daily(date(2026, 10, 29))

    assert result == "frame"
    assert received == ["20261029"]
    assert alarms == [module.REQUEST_TIMEOUT_SECONDS, 0]


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="POSIX signal alarm required")
def test_alarm_is_cancelled_when_official_request_fails(monkeypatch):
    module = load_module()
    alarms = []

    def fail_request(**kwargs):
        raise TimeoutError("request timed out")

    monkeypatch.setattr(module.signal, "alarm", alarms.append)
    monkeypatch.setattr(module.akshare, "get_cffex_daily", fail_request)

    with pytest.raises(TimeoutError, match="request timed out"):
        module.fetch_daily(date(2026, 10, 29))

    assert alarms == [module.REQUEST_TIMEOUT_SECONDS, 0]
