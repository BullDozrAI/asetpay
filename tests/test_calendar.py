"""The market calendar, and the snapshotter's behaviour on a closed day."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from asetpay_snapshotter.market_calendar import (
    covered_years,
    is_trading_day,
    read_holidays,
    trading_sessions,
)

ROOT = Path(__file__).resolve().parents[1]
HOLIDAYS_FILE = ROOT / "market_holidays.txt"


# ------------------------------------------------------------- the file itself


def test_the_holiday_file_parses():
    """Read by the nightly job and the gap detector. A malformed line must fail
    here, not at 03:00 UTC."""
    assert read_holidays(HOLIDAYS_FILE)


def test_every_holiday_is_a_weekday():
    """The file holds OBSERVED dates. A weekend date is a typo — the real
    closure is on the adjacent weekday, which would then be reported as a gap
    and attempted as a capture."""
    weekend = [d for d in read_holidays(HOLIDAYS_FILE) if d.weekday() >= 5]
    assert not weekend, f"holidays on a weekend (enter the observed date): {weekend}"


def test_the_calendar_covers_next_year():
    """The expiry guard. Fails on 1 January if next year is missing — a year's
    warning before the snapshotter would start refusing to run."""
    need = date.today().year + 1
    years = covered_years(read_holidays(HOLIDAYS_FILE))
    assert need in years, (
        f"market_holidays.txt covers {years.start}-{years.stop - 1}; add {need} "
        "from https://www.nyse.com/markets/hours-calendars"
    )


def test_every_covered_year_has_its_fixed_holidays():
    """A year that is listed but half-empty passes the coverage check while
    marking real closures as sessions. Thanksgiving and Labor Day always fall
    on a weekday, so every covered year must have both."""
    holidays = read_holidays(HOLIDAYS_FILE)
    for year in covered_years(holidays):
        names = {n for d, n in holidays.items() if d.year == year}
        assert "Labor Day" in names, f"{year} has no Labor Day"
        assert "Thanksgiving Day" in names, f"{year} has no Thanksgiving"


# --------------------------------------------------------------- the logic


HOLIDAYS = read_holidays(HOLIDAYS_FILE)


def test_labor_day_2026_is_closed_and_the_days_around_it_are_open():
    assert not is_trading_day(date(2026, 9, 7), HOLIDAYS)
    assert is_trading_day(date(2026, 9, 4), HOLIDAYS)
    assert is_trading_day(date(2026, 9, 8), HOLIDAYS)


def test_weekends_are_closed():
    assert not is_trading_day(date(2026, 9, 19), HOLIDAYS)
    assert not is_trading_day(date(2026, 9, 20), HOLIDAYS)


def test_early_closes_are_still_sessions():
    """The day after Thanksgiving closes at 1pm but trades, and gets a snapshot."""
    assert is_trading_day(date(2026, 11, 27), HOLIDAYS)


def test_a_year_outside_the_calendar_is_refused_not_assumed_open():
    with pytest.raises(SystemExit, match="outside the holiday calendar"):
        is_trading_day(date(2031, 3, 3), HOLIDAYS)


def test_trading_sessions_is_inclusive_and_skips_closures():
    got = trading_sessions(date(2026, 9, 3), date(2026, 9, 9), HOLIDAYS)
    assert got == [date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9)]


def test_a_missing_holiday_file_is_an_error(tmp_path):
    """Unlike delistings: no file would mark every holiday as a session."""
    with pytest.raises(SystemExit, match="not found"):
        read_holidays(tmp_path / "nope.txt")


@pytest.mark.parametrize("line", ["2026-09-07", "2026-09-07,", "not-a-date,Labor Day"])
def test_a_malformed_holiday_line_is_refused(tmp_path, line):
    bad = tmp_path / "h.txt"
    bad.write_text(line + "\n")
    with pytest.raises((SystemExit, ValueError)):
        read_holidays(bad)


# ------------------------------------------- the snapshotter on a closed day


def _capture_main(monkeypatch, tmp_path, *extra):
    universe = tmp_path / "universe.txt"
    universe.write_text("AAPL\nMSFT\n")
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "capture",
            "--out",
            str(tmp_path / "out"),
            "--symbols-file",
            str(universe),
            "--holidays-file",
            str(HOLIDAYS_FILE),
            *extra,
        ],
    )
    from asetpay_snapshotter import capture as cap

    return cap.main()


def test_the_scheduled_run_after_a_holiday_is_green_and_publishes_nothing(
    monkeypatch, tmp_path, capsys
):
    """The 2026-09-08 run tried to capture Labor Day and went red. Now it exits
    cleanly and writes no manifest — which is what snapshot.yml gates publishing
    on. It must NOT fall back to Friday: that session already has a release."""
    monkeypatch.setattr(
        "asetpay_snapshotter.capture.previous_session", lambda: date(2026, 9, 7)
    )
    assert _capture_main(monkeypatch, tmp_path) == 0
    assert not (tmp_path / "out" / "_manifest.json").exists()
    assert "market closed on 2026-09-07 (Labor Day)" in capsys.readouterr().out


def test_the_scheduled_run_on_a_session_still_captures(monkeypatch, tmp_path):
    """The other half: the holiday path must not swallow a real session."""
    monkeypatch.setattr(
        "asetpay_snapshotter.capture.previous_session", lambda: date(2026, 9, 8)
    )
    # Alpaca with no credentials returns an empty frame but still writes the
    # manifest — enough to prove the capture path ran. No network.
    monkeypatch.setenv("PRICE_SOURCE", "alpaca")
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    _capture_main(monkeypatch, tmp_path)
    assert (tmp_path / "out" / "_manifest.json").exists()


def test_backfilling_a_holiday_by_hand_fails_loudly(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="market closed on 2026-09-07"):
        _capture_main(monkeypatch, tmp_path, "--session", "2026-09-07")
