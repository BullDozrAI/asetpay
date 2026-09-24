"""Which days the market was open — shared by the snapshotter and the gap detector.

Two jobs ask the same question and must get the same answer. The snapshotter
asks "was yesterday a session, or is there nothing to capture?" The gap detector
asks "which sessions should have a snapshot by now?" If they used different
calendars, a holiday would be either a failed capture or a reported gap — and a
detector that cries wolf on every holiday gets muted, which is worse than none.

The calendar is a checked-in file rather than a library for the same reason
`universe_delisted.txt` is: the list is short, it changes once a year, and a
diff of it is a readable record of what the system believed.

FAIL CLOSED
-----------
A date outside the years the file covers is refused, not assumed open. Assuming
open would, on 1 January of an unlisted year, make the snapshotter try to
capture a closed market and make the detector report every holiday as a gap —
both loud, but loud for the wrong reason, and the reflexive 2am fix is to delete
the check. Refusing names the actual problem: the calendar needs another year.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path


def read_holidays(path: Path) -> dict[date, str]:
    """date -> holiday name, from `market_holidays.txt`.

    Unlike `read_delistings`, a missing file is an error. No delistings is a
    legitimate state; no holidays is not — it would silently mark every holiday
    as a trading day.
    """
    if not path.exists():
        raise SystemExit(
            f"holiday calendar not found: {path}\n"
            "Refusing to guess which days the market was open."
        )
    out: dict[date, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [f.strip() for f in line.split(",", 1)]
        if len(parts) < 2 or not parts[1]:
            raise SystemExit(f"malformed line in {path}: {line!r}")
        out[date.fromisoformat(parts[0])] = parts[1]
    if not out:
        raise SystemExit(f"holiday calendar is empty: {path}")
    return out


def covered_years(holidays: dict[date, str]) -> range:
    """The span of years the calendar speaks for, inclusive."""
    years = {d.year for d in holidays}
    return range(min(years), max(years) + 1)


def is_trading_day(d: date, holidays: dict[date, str]) -> bool:
    """A weekday that is not a full-day closure. Raises outside the calendar."""
    if d.year not in covered_years(holidays):
        raise SystemExit(
            f"{d} is outside the holiday calendar "
            f"({min(covered_years(holidays))}-{max(covered_years(holidays))}). "
            "Add that year to market_holidays.txt rather than assuming it is open."
        )
    return d.weekday() < 5 and d not in holidays


def trading_sessions(start: date, end: date, holidays: dict[date, str]) -> list[date]:
    """Every session from `start` to `end`, both inclusive."""
    out: list[date] = []
    d = start
    while d <= end:
        if is_trading_day(d, holidays):
            out.append(d)
        d += timedelta(days=1)
    return out


def previous_session(today: date | None = None) -> date:
    """The most recent weekday strictly before `today` — the CANDIDATE session.

    Deliberately does not step over holidays. The scheduled capture asks about
    exactly one day, the one that just ended; if that day was a holiday the
    right answer is "nothing to capture", not "capture the session before it",
    which already has a release and would trip the overwrite guard. Whether the
    candidate was open is `is_trading_day`'s question.
    """
    d = today or datetime.now(tz=UTC).date()
    d = date.fromordinal(d.toordinal() - 1)
    while d.weekday() >= 5:
        d = date.fromordinal(d.toordinal() - 1)
    return d
