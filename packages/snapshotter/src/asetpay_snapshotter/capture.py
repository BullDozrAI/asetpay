"""P1-05 / P1-08 — the daily snapshot.

Fifty lines of real work, and the only genuinely time-sensitive thing in the
project: on the free data path, history not captured today cannot be bought back
at any price. This runs before anything consumes it, and it runs tonight.

Two properties matter more than the fetching:

  IMMUTABLE   a snapshot is written once and never edited. A correction is a new
              snapshot with a later knowledge_date, never an overwrite. That is
              what makes the point-in-time store possible downstream.

  MANIFESTED  every capture records provider, parameters, row count, checksum,
              fetch time and schema version. Without the manifest you cannot tell
              a day with genuinely no data from a day the fetch silently failed —
              and those need very different responses.

The checksum is computed over canonically sorted, canonically typed rows so that
re-running on identical input produces an identical digest. That determinism is
tested (P1-08) because without it `features_hash` downstream becomes unstable,
which presents as a data bug and takes a week to find.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Manifest:
    """What was captured, from where, and proof it is intact."""

    snapshot_id: str
    knowledge_date: str
    provider: str
    parameters: dict[str, object]
    row_count: int
    checksum: str
    fetched_at: str
    schema_version: int

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True))


def canonical_checksum(df: pd.DataFrame) -> str:
    """A digest that depends only on the CONTENT, not on incidental ordering.

    Sorting the columns and rows, and forcing a fixed float repr, is what makes
    two runs over the same input agree. Without it, dict iteration order and
    BLAS-dependent float formatting make the digest wobble between runs.
    """
    if df.empty:
        return hashlib.sha256(b"").hexdigest()
    d = df.reindex(sorted(df.columns), axis=1)
    d = d.sort_values(by=list(d.columns), kind="mergesort").reset_index(drop=True)
    payload = d.to_csv(index=False, float_format="%.10g").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def previous_session(today: date | None = None) -> date:
    """The most recent weekday strictly before `today`.

    A real implementation consults the exchange calendar; weekdays are enough
    until the universe includes a market with different holidays, at which point
    this becomes a genuine bug and should be replaced rather than patched.
    """
    d = today or datetime.now(tz=UTC).date()
    d = date.fromordinal(d.toordinal() - 1)
    while d.weekday() >= 5:
        d = date.fromordinal(d.toordinal() - 1)
    return d


def fetch_prices(knowledge_date: date, symbols: list[str]) -> pd.DataFrame:
    """Fetch one session of daily bars.

    Wired to Alpaca because a free paper account gives both the price feed and
    the Phase 4 broker in one signup (P1-14b).

    Returns an EMPTY frame rather than raising when credentials are missing, so
    the pipeline can be exercised end to end before the account exists. The
    workflow refuses to publish an empty snapshot, so this cannot quietly become
    a run of blank releases.
    """
    key = os.environ.get("ALPACA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET_KEY")
    if not key or not secret:
        print("ALPACA credentials absent — emitting an empty frame (see P1-14b)")
        return pd.DataFrame(
            columns=["event_date", "symbol", "open", "high", "low", "close", "volume"]
        )

    import httpx  # imported lazily so the module loads without the dependency

    day = knowledge_date.isoformat()
    rows: list[dict[str, object]] = []
    with httpx.Client(timeout=60.0) as client:
        # Chunked to stay inside URL limits; the free tier is generous on rate.
        for i in range(0, len(symbols), 200):
            chunk = symbols[i : i + 200]
            r = client.get(
                "https://data.alpaca.markets/v2/stocks/bars",
                params={
                    "symbols": ",".join(chunk),
                    "timeframe": "1Day",
                    "start": day,
                    "end": day,
                    "adjustment": "raw",  # adjustments are applied downstream,
                    # point-in-time, NOT baked in here — a bar adjusted with
                    # today's split factors is not what was observable then.
                    "feed": "iex",
                },
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            )
            r.raise_for_status()
            for sym, bars in (r.json().get("bars") or {}).items():
                for b in bars:
                    rows.append(
                        {
                            "event_date": b["t"][:10],
                            "symbol": sym,
                            "open": b["o"],
                            "high": b["h"],
                            "low": b["l"],
                            "close": b["c"],
                            "volume": b["v"],
                        }
                    )
    return pd.DataFrame(rows)


def capture(out_dir: Path, knowledge_date: date, symbols: list[str]) -> Manifest:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = datetime.now(tz=UTC)
    df = fetch_prices(knowledge_date, symbols)

    # The knowledge_date is stamped on every row. It is the axis the whole
    # point-in-time store is partitioned by, so it is never inferred later.
    df = df.copy()
    df["knowledge_date"] = knowledge_date.isoformat()
    df.to_parquet(out_dir / "prices.parquet", index=False)

    m = Manifest(
        snapshot_id=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        knowledge_date=knowledge_date.isoformat(),
        provider="alpaca.v2.stocks.bars",
        parameters={"timeframe": "1Day", "feed": "iex", "n_symbols": len(symbols)},
        row_count=len(df),
        checksum=f"sha256:{canonical_checksum(df)}",
        fetched_at=started.isoformat(),
        schema_version=SCHEMA_VERSION,
    )
    m.write(out_dir / "_manifest.json")
    print(f"captured {m.row_count} rows for {m.knowledge_date} -> {out_dir}")
    return m


def main() -> int:
    p = argparse.ArgumentParser(description="Daily immutable market-data capture")
    p.add_argument("--out", type=Path, default=Path("out"))
    p.add_argument("--knowledge-date", type=date.fromisoformat, default=None)
    p.add_argument(
        "--symbols-file",
        type=Path,
        default=Path("universe.txt"),
        help="one symbol per line; the working universe until P1-15 lands",
    )
    a = p.parse_args()

    symbols = (
        [s.strip() for s in a.symbols_file.read_text().split() if s.strip()]
        if a.symbols_file.exists()
        else ["AAPL", "MSFT", "SPY"]
    )
    capture(a.out, a.knowledge_date or previous_session(), symbols)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
