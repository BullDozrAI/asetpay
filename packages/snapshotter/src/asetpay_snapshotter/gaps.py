"""P1-06b — which trading sessions have no snapshot.

The first version of this check asked "how old is the newest release?" and was
wrong three ways at once:

  WRONG CLOCK  it read the release's `createdAt`, which GitHub sets to the date
               of the COMMIT the tag points at, not when the release was
               published. Every snapshot is tagged on main's HEAD, so it was
               really measuring "how long since anyone pushed to main" and
               raised a false alarm whenever main was quiet for three days.

  ONLY THE TIP a hole in the middle — a backfill day cancelled by the old global
               concurrency group — was invisible behind a fresh newest release.

  NO DELIVERY  (in the workflow) the issue label never existed, so every alarm
               crashed before it could open an issue.

This version reads no timestamps from GitHub at all. A snapshot's tag NAMES its
session (`snapshot-2026-09-23` is the 23 September session), so the question
becomes set arithmetic: every session since the first one of record that is
due by now, minus every session that has a tag. What is left is exactly the
list of days to backfill, and it stays on that list until someone does.

Pure logic here; the workflow does the GitHub I/O and hands this a JSON file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from asetpay_snapshotter.market_calendar import read_holidays, trading_sessions

TAG_PREFIX = "snapshot-"
_TAG = re.compile(r"^snapshot-(\d{4}-\d{2}-\d{2})$")

# Session S is captured at 03:00 UTC on S+1 (snapshot.yml). It counts as DUE
# from 12:00 UTC on S+1: nine hours of slack for GitHub's delayed schedules, and
# still before this check runs at 12:30, so a failed capture is reported the
# same day rather than the next.
DUE_AFTER = timedelta(days=1, hours=12)


def sessions_from_tags(tags: Iterable[str]) -> set[date]:
    """Sessions that have a snapshot release.

    Tags that are not snapshots are ignored. A tag that LOOKS like a snapshot
    but does not parse is an error: skipping it would report its session as
    missing and send someone to backfill a day that is already captured under
    a typo — or, worse, hide a day that was captured under the wrong name.
    """
    out: set[date] = set()
    for tag in tags:
        if not tag.startswith(TAG_PREFIX):
            continue
        m = _TAG.match(tag)
        if not m:
            raise SystemExit(f"release tag {tag!r} looks like a snapshot but does not parse")
        out.add(date.fromisoformat(m.group(1)))
    return out


def due_sessions(since: date, now: datetime, holidays: dict[date, str]) -> list[date]:
    """Every session from `since` whose snapshot should exist by `now`."""
    last_due = (now - DUE_AFTER).date()
    if last_due < since:
        return []
    return trading_sessions(since, last_due, holidays)


def find_gaps(
    tags: Iterable[str], since: date, now: datetime, holidays: dict[date, str]
) -> list[date]:
    """Sessions that are due and have no snapshot, oldest first."""
    present = sessions_from_tags(tags)
    return [s for s in due_sessions(since, now, holidays) if s not in present]


def _append_to_env_file(var: str, text: str) -> None:
    """Append to the file GitHub names in `var`; a no-op outside Actions."""
    path = os.environ.get(var)
    if path:
        with Path(path).open("a", encoding="utf-8") as f:
            f.write(text)


def main() -> int:
    p = argparse.ArgumentParser(description="Report trading sessions with no snapshot")
    p.add_argument(
        "--releases",
        type=Path,
        required=True,
        help="output of `gh release list --json tagName`",
    )
    p.add_argument(
        "--since",
        type=date.fromisoformat,
        required=True,
        help="first session of record; an explicit constant, never inferred from "
        "the oldest tag, or losing every release would report no gaps",
    )
    p.add_argument(
        "--limit",
        type=int,
        required=True,
        help="the --limit given to `gh release list`; reaching it means the "
        "listing may be truncated, which is refused",
    )
    p.add_argument("--holidays-file", type=Path, default=Path("market_holidays.txt"))
    p.add_argument(
        "--now",
        type=datetime.fromisoformat,
        default=None,
        help="evaluate as of this UTC instant; defaults to now",
    )
    p.add_argument(
        "--drop-session",
        type=date.fromisoformat,
        default=None,
        help="DRILL: treat this captured session as missing, to prove the alert fires",
    )
    a = p.parse_args()

    # utf-8-sig: Windows PowerShell writes a BOM, and a local run is how this
    # gets checked by hand.
    releases = json.loads(a.releases.read_text(encoding="utf-8-sig"))
    if len(releases) >= a.limit:
        raise SystemExit(
            f"release listing returned {len(releases)} entries, the --limit of "
            f"{a.limit}. It may be truncated, and a truncated list reports false "
            "gaps. Raise the limit in gap-detector.yml."
        )
    tags = [r["tagName"] for r in releases]

    if a.drop_session is not None:
        dropped = f"{TAG_PREFIX}{a.drop_session.isoformat()}"
        if dropped not in tags:
            raise SystemExit(
                f"drill asked to drop {dropped}, which does not exist. Dropping a "
                "session that was never captured proves nothing about the alert."
            )
        tags = [t for t in tags if t != dropped]
        print(f"DRILL: pretending {a.drop_session} has no snapshot")

    now = a.now or datetime.now(tz=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    holidays = read_holidays(a.holidays_file)
    due = due_sessions(a.since, now, holidays)
    missing = find_gaps(tags, a.since, now, holidays)

    print(f"{len(due)} sessions due since {a.since}; {len(missing)} missing")
    for s in missing:
        print(f"  missing: {s}")

    _append_to_env_file(
        "GITHUB_OUTPUT",
        f"missing={' '.join(s.isoformat() for s in missing)}\n"
        f"count={len(missing)}\n"
        f"due={len(due)}\n",
    )
    if missing:
        rows = "\n".join(
            f"| {s} | `gh workflow run snapshot.yml -f session={s}` |" for s in missing
        )
        summary = (
            f"### {len(missing)} of {len(due)} sessions since {a.since} have no snapshot\n\n"
            f"| session | backfill |\n|---|---|\n{rows}\n"
        )
    else:
        summary = f"All {len(due)} sessions since {a.since} have a snapshot.\n"
    _append_to_env_file("GITHUB_STEP_SUMMARY", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
