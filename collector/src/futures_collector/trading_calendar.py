from __future__ import annotations

from datetime import date, timedelta

# Versioned 2026 futures-market closures. Weekends are always closed even when
# the general public-holiday calendar designates a compensating workday.
# Source: State Council 2026 holiday notice and SHFE 2026 market closure notice.
CALENDAR_VERSION = "cn-futures-2026-v1"
COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2026, 12, 31)
MARKET_CLOSURES = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 2, 16),
        date(2026, 2, 17),
        date(2026, 2, 18),
        date(2026, 2, 19),
        date(2026, 2, 20),
        date(2026, 2, 23),
        date(2026, 4, 6),
        date(2026, 5, 1),
        date(2026, 5, 4),
        date(2026, 5, 5),
        date(2026, 6, 19),
        date(2026, 9, 25),
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
    }
)


def latest_trading_date(as_of: date) -> date:
    if not COVERAGE_START <= as_of <= COVERAGE_END:
        raise ValueError(f"controlled trading calendar has no coverage for {as_of.year}")
    candidate = as_of
    while candidate >= COVERAGE_START:
        if candidate.weekday() < 5 and candidate not in MARKET_CLOSURES:
            return candidate
        candidate -= timedelta(days=1)
    raise ValueError("controlled trading calendar has no prior trading date")
