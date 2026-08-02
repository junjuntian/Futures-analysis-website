import logging

from futures_collector import cli


def test_startup_failure_logs_only_exception_type(monkeypatch, caplog) -> None:
    def fail_to_load_credentials():
        raise PermissionError("must-not-appear")

    monkeypatch.setattr(cli, "load_credentials", fail_to_load_credentials)

    with caplog.at_level(logging.ERROR):
        result = cli.main(["--date", "2026-07-31", "--exchange", "DCE", "--dataset", "market"])

    assert result == 1
    assert "collector_start_failed error=PermissionError" in caplog.text
    assert "must-not-appear" not in caplog.text
