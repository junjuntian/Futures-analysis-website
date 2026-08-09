import pytest


@pytest.fixture(autouse=True)
def no_upstream_pacing(monkeypatch):
    """Remove the Sina request floor for every test.

    The floor is real behaviour and has its own tests, which set it back. Paying
    it everywhere else would make the suite sleep for its whole runtime — it
    already crept from under a second to eleven — and a slow suite is a suite
    that stops being run.
    """
    monkeypatch.setenv("FUTURES_SINA_MIN_INTERVAL_SECONDS", "0")
