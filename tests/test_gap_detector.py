"""P1-06b — the gap detector reports exactly the sessions with no snapshot.

The first version of this check was never tested, and it shows: it measured the
wrong clock, looked only at the newest release, and could not deliver an alert.
Each of those gets a test here, plus the negative control — a detector that has
never reported a gap may be incapable of reporting one.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from asetpay_snapshotter.gaps import find_gaps, main, sessions_from_tags
from asetpay_snapshotter.market_calendar import read_holidays, trading_sessions

ROOT = Path(__file__).resolve().parents[1]
HOLIDAYS = read_holidays(ROOT / "market_holidays.txt")
SINCE = date(2026, 9, 1)


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _tags(start: date, end: date) -> list[str]:
    return [f"snapshot-{s}" for s in trading_sessions(start, end, HOLIDAYS)]


# ------------------------------------------------------------- the incident


def test_the_false_alarm_of_2026_09_23_is_not_raised():
    """The run that crashed on 2026-09-23 at 17:20 UTC claimed "age: 4 days".

    Every session from 09-01 to 09-22 had a release; 09-22's had been published
    nine hours earlier. The detector read `createdAt` — the date of the commit
    the tag points at, 09-19 — and called it four days stale. This one reads
    tag names only, and has no way to be misled by a commit date.
    """
    tags = _tags(SINCE, date(2026, 9, 22))
    assert find_gaps(tags, SINCE, _utc("2026-09-23T17:20:00"), HOLIDAYS) == []


# ----------------------------------------------------------- the blind spot


def test_a_hole_behind_a_fresh_release_is_reported():
    """The old check looked only at the newest release, so a middle day lost
    during a backfill was invisible as long as the next night landed."""
    tags = [t for t in _tags(SINCE, date(2026, 9, 18)) if t != "snapshot-2026-09-16"]
    now = _utc("2026-09-19T12:30:00")
    assert find_gaps(tags, SINCE, now, HOLIDAYS) == [date(2026, 9, 16)]


@pytest.mark.parametrize(
    "dropped", trading_sessions(SINCE, date(2026, 9, 23), HOLIDAYS), ids=str
)
def test_negative_control_every_single_missing_session_is_caught(dropped):
    """Remove any one session from a complete history; exactly that one comes
    back. If this passes with nothing dropped too, the detector is blind."""
    tags = [t for t in _tags(SINCE, date(2026, 9, 23)) if t != f"snapshot-{dropped}"]
    assert find_gaps(tags, SINCE, _utc("2026-09-24T12:30:00"), HOLIDAYS) == [dropped]


def test_several_holes_come_back_oldest_first():
    tags = [
        t
        for t in _tags(SINCE, date(2026, 9, 23))
        if t not in {"snapshot-2026-09-21", "snapshot-2026-09-02"}
    ]
    got = find_gaps(tags, SINCE, _utc("2026-09-24T12:30:00"), HOLIDAYS)
    assert got == [date(2026, 9, 2), date(2026, 9, 21)]


def test_no_releases_at_all_is_every_session_missing():
    """Why --since is an explicit constant: inferred from the oldest tag, an
    empty listing would have no start date and report nothing."""
    got = find_gaps([], SINCE, _utc("2026-09-24T12:30:00"), HOLIDAYS)
    assert got == trading_sessions(SINCE, date(2026, 9, 23), HOLIDAYS)


# ------------------------------------------------ closed days are not gaps


def test_weekends_and_labor_day_are_never_gaps():
    """2026-09-07 is Labor Day. The Tuesday run that tried to capture it was the
    one red snapshot run since automation went live; it must not be a gap."""
    tags = _tags(SINCE, date(2026, 9, 23))
    assert "snapshot-2026-09-07" not in tags
    got = find_gaps(tags, SINCE, _utc("2026-09-24T12:30:00"), HOLIDAYS)
    assert got == []


# ------------------------------------------------------------ when it's due


@pytest.mark.parametrize(
    ("now", "reported"),
    [
        ("2026-09-24T03:05:00", False),  # capture still running
        ("2026-09-24T11:59:00", False),  # inside the delay allowance
        ("2026-09-24T12:00:00", True),  # due
        ("2026-09-24T12:30:00", True),  # the scheduled check
    ],
)
def test_a_session_becomes_due_at_noon_utc_the_next_day(now, reported):
    """Captured 03:00 UTC on S+1, due from 12:00 — nine hours of slack for
    GitHub's delayed schedules, and still before the 12:30 check, so a failed
    capture is reported the same day."""
    tags = _tags(SINCE, date(2026, 9, 22))  # 09-23 not captured
    got = find_gaps(tags, SINCE, _utc(now), HOLIDAYS)
    assert (date(2026, 9, 23) in got) is reported


def test_friday_is_due_on_saturday_not_monday():
    tags = _tags(SINCE, date(2026, 9, 17))  # 09-18 (Fri) not captured
    assert find_gaps(tags, SINCE, _utc("2026-09-19T12:30:00"), HOLIDAYS) == [date(2026, 9, 18)]


def test_nothing_is_due_before_the_first_session_has_had_its_chance():
    assert find_gaps([], SINCE, _utc("2026-09-02T11:00:00"), HOLIDAYS) == []


# -------------------------------------------------------------- tag parsing


def test_non_snapshot_tags_are_ignored():
    assert sessions_from_tags(["v1.0", "snapshot-2026-09-01", "release-x"]) == {
        date(2026, 9, 1)
    }


@pytest.mark.parametrize("bad", ["snapshot-2026-9-1", "snapshot-2026-09-01-fix", "snapshot-"])
def test_a_malformed_snapshot_tag_is_refused_not_skipped(bad):
    """Skipping it would send someone to backfill a day that is captured under
    a typo — or hide a day captured under the wrong name."""
    with pytest.raises(SystemExit, match="does not parse"):
        sessions_from_tags([bad])


# --------------------------------------------------------------------- CLI


def _run(monkeypatch, tmp_path, tags, *extra):
    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps([{"tagName": t} for t in tags]))
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "gaps",
            "--releases",
            str(releases),
            "--since",
            "2026-09-01",
            "--holidays-file",
            str(ROOT / "market_holidays.txt"),
            "--now",
            "2026-09-24T12:30:00",
            *extra,
        ],
    )
    assert main() == 0
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


def test_cli_reports_to_github_output(monkeypatch, tmp_path):
    tags = [t for t in _tags(SINCE, date(2026, 9, 23)) if t != "snapshot-2026-09-10"]
    got = _run(monkeypatch, tmp_path, tags, "--limit", "1000")
    assert got == {"missing": "2026-09-10", "count": "1", "due": "16"}


def test_cli_healthy_history_reports_zero(monkeypatch, tmp_path):
    got = _run(monkeypatch, tmp_path, _tags(SINCE, date(2026, 9, 23)), "--limit", "1000")
    assert got["count"] == "0"
    assert got["missing"] == ""


def test_the_drill_drops_exactly_one_session(monkeypatch, tmp_path):
    got = _run(
        monkeypatch,
        tmp_path,
        _tags(SINCE, date(2026, 9, 23)),
        "--limit",
        "1000",
        "--drop-session",
        "2026-09-16",
    )
    assert got["missing"] == "2026-09-16"


def test_a_drill_on_a_session_never_captured_is_refused(monkeypatch, tmp_path):
    """Dropping a day that has no release changes nothing, so the drill would
    "pass" without having tested the alert at all."""
    with pytest.raises(SystemExit, match="does not exist"):
        _run(
            monkeypatch,
            tmp_path,
            _tags(SINCE, date(2026, 9, 23)),
            "--limit",
            "1000",
            "--drop-session",
            "2026-09-07",
        )


def test_a_listing_that_hits_the_limit_is_refused(monkeypatch, tmp_path):
    """`gh release list` truncates silently at --limit. A truncated listing
    reports false gaps — or, sorted the wrong way, hides real ones."""
    tags = _tags(SINCE, date(2026, 9, 23))
    with pytest.raises(SystemExit, match="truncated"):
        _run(monkeypatch, tmp_path, tags, "--limit", str(len(tags)))
