"""P1-09 — the on-disk shape of the point-in-time store.

    root/
      prices/knowledge_date=2019-01-01/data.parquet
      fundamentals/knowledge_date=2019-02-15/data.parquet
      identifiers/data.parquet

`knowledge_date` is a DIRECTORY, not just a column. That is the whole point:
DuckDB's hive partitioning lets `WHERE knowledge_date <= '2023-06-15'` prune
every later directory without opening it, so an as-of query reads only what was
knowable — and a query costs what the history you asked about costs, not what
the history you own costs. A single flat file makes lookahead a filter you must
remember to apply; this layout makes the later data physically absent from the
scan.

Identifiers are deliberately NOT partitioned this way. They are interval-
versioned on EVENT time (`valid_from`, `valid_to`) — when a ticker meant a
company — which is a different axis from when we learned it. Making them
properly bitemporal is P1-16. Until then they are a single file and the
knowledge-time guarantee does not extend to them; `resolve_ticker` says so.

Writing is append-only. A correction is a new file under a later
knowledge_date, never an edit to an existing one — the same rule the
snapshotter follows for releases.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# Tables partitioned by knowledge_date, and the columns identifying one fact
# within them. Those identity columns are what `as_of` deduplicates on: among
# the rows knowable at a given time, the newest statement about each fact wins.
PARTITIONED: dict[str, tuple[str, ...]] = {
    "prices": ("asset_id", "event_date"),
    "fundamentals": ("asset_id", "period_end", "metric"),
}

UNPARTITIONED: tuple[str, ...] = ("identifiers", "delistings")


def write_partitioned(frame: pd.DataFrame, root: Path, table: str) -> int:
    """Write one table into the knowledge_date-partitioned layout.

    Returns the number of partitions written. Existing partitions are left
    alone rather than overwritten — a snapshot is immutable once landed, and a
    writer that silently replaces one destroys the only record of what was
    believed on that date.
    """
    root = Path(root)
    if table not in PARTITIONED:
        out = root / table
        out.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(out / "data.parquet", index=False)
        return 1

    written = 0
    for kdate, part in frame.groupby("knowledge_date", sort=True):
        out = root / table / f"knowledge_date={kdate}"
        if (out / "data.parquet").exists():
            continue
        out.mkdir(parents=True, exist_ok=True)
        # The partition column lives in the path; keeping it in the file too
        # would let the two disagree, and the path is what DuckDB prunes on.
        part.drop(columns=["knowledge_date"]).to_parquet(out / "data.parquet", index=False)
        written += 1
    return written


def build_from_fixture(fixture_dir: Path, root: Path) -> dict[str, int]:
    """Materialise the synthetic fixture into the real store's layout.

    The fixture is the only dataset where the right answer is written down, so
    the real store is developed against it and the planted restatements become
    the acceptance test for `as_of`. When live snapshots replace it, nothing
    about the store changes — only where the frames come from.
    """
    fixture_dir, root = Path(fixture_dir), Path(root)
    counts: dict[str, int] = {}
    for table in (*PARTITIONED, *UNPARTITIONED):
        src = fixture_dir / f"{table}.parquet"
        if not src.exists():
            continue
        counts[table] = write_partitioned(pd.read_parquet(src), root, table)

    # Building nothing is not building. Returning {} here reads as success to a
    # caller and to a human watching `make store` scroll past — and the store
    # that results is empty, so every as-of query answers "nothing was knowable"
    # rather than failing. Refuse instead.
    if not any(t in counts for t in PARTITIONED):
        raise SystemExit(
            f"no fixture tables found under {fixture_dir}. Generate one first: make fixture"
        )
    return counts
