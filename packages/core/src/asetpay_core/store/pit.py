"""P1-09 — the real point-in-time store, over Parquet, via DuckDB.

`FixtureStore` proved the semantics on data small enough to hold in memory.
This is the same semantics expressed as SQL over a partitioned layout, so it
keeps working once the history no longer fits in RAM.

The as-of rule, unchanged from the fixture and from the Protocol docstring:

    1. discard everything with knowledge_date > the instant being asked about
    2. among what remains, keep the LATEST statement about each fact

Step 2 is the QUALIFY clause. Restatements fall out of it for free: the
original and the revision are two rows differing only in knowledge_date, and
the window function picks whichever was current at the time. Nothing in the
query knows what a restatement is.

Why this is the ONLY module allowed to read raw files
-----------------------------------------------------
`.importlinter` forbids `duckdb` from `asetpay_core.evaluation` and
`asetpay_core.signals`. One chokepoint means one place lookahead can enter and
one place to audit — enforced by CI rather than by remembering. If a second
module ever needs to read Parquet, that is the moment to ask why, not to relax
the rule.

Why step 1 is not enough on its own
-----------------------------------
Filtering on knowledge_date alone returns BOTH versions of a restated figure —
the original and the revision — and whichever row comes back first looks correct
in a spot check. The bug shows up only as a backtest that is mildly,
inexplicably too good. `test_pit_store.py` therefore asserts the VALUE, not the
row count, and `test_no_lookahead.py` compares against a store from which the
later data is physically absent rather than merely filtered out.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from asetpay_contracts import AssetId
from asetpay_core.store.layout import PARTITIONED

# The fixture, and the live store built from it, hold exactly one universe.
# Named here rather than spelled inline so the KeyError below and any future
# universe table disagree loudly rather than silently.
DEFAULT_UNIVERSE = "all"


def _glob(root: Path, table: str) -> str:
    return str(Path(root) / table / "**" / "*.parquet")


def _has_data(root: Path, table: str) -> bool:
    """Whether any partition exists for this table.

    DuckDB's read_parquet raises IOException when a glob matches nothing, so a
    store early in its life — or one truncated to a date before the first
    filing — would crash rather than report that it knows nothing yet. Knowing
    nothing is a legitimate answer to an honest question, and every caller
    already handles an empty frame.
    """
    return any(Path(root).joinpath(table).glob("**/*.parquet"))


def _empty(table: str) -> pd.DataFrame:
    """An empty frame carrying the identity columns, so callers can sort and
    group it without special-casing the "nothing known yet" branch."""
    return pd.DataFrame(columns=[*PARTITIONED[table], "knowledge_date"])


def _asof_sql(table: str, extra_where: str = "") -> str:
    """The as-of query. `knowledge_date` comes back from the directory name.

    hive_partitioning=1 is what makes the WHERE clause prune directories rather
    than filter rows: the later data is never read, so it cannot leak through a
    bug in some later filter.
    """
    keys = ", ".join(PARTITIONED[table])
    return f"""
        SELECT * FROM read_parquet($glob, hive_partitioning = 1)
        WHERE knowledge_date <= $kt {extra_where}
        QUALIFY row_number() OVER (
            PARTITION BY {keys} ORDER BY knowledge_date DESC
        ) = 1
    """


class PitStoreView:
    """A frozen view of everything knowable at one instant.

    Obtained only from `PitStore.as_of()`. No method here takes a knowledge-time
    argument, because the knowledge time is baked in — which is what stops a
    caller accidentally asking a view for something it should not know.
    """

    def __init__(
        self, root: Path, con: duckdb.DuckDBPyConnection, knowledge_time: date
    ) -> None:
        self._root = root
        self._con = con
        self._kt = knowledge_time

    @property
    def knowledge_time(self) -> date:
        return self._kt

    def _run(self, sql: str, **params: Any) -> pd.DataFrame:
        return self._con.execute(sql, {"kt": self._kt.isoformat(), **params}).df()

    def prices(self, assets: Sequence[AssetId], start: date, end: date) -> pd.DataFrame:
        """Daily bars as they were known at knowledge_time.

        Prices are deduplicated on (asset_id, event_date) exactly as
        fundamentals are. In the synthetic fixture a price is never revised, so
        this looks redundant — but the snapshotter's whole contract is that a
        correction arrives as a new capture with a later knowledge_date, and a
        vendor reissuing a bar is ordinary. Handling it only for fundamentals
        would leave prices as the one place a restatement silently wins.
        """
        if not _has_data(self._root, "prices"):
            return _empty("prices")
        sql = _asof_sql("prices", "AND event_date >= $start AND event_date <= $end")
        df = self._run(
            sql,
            glob=_glob(self._root, "prices"),
            start=start.isoformat(),
            end=end.isoformat(),
        )
        if len(assets):
            df = df[df["asset_id"].isin([str(a) for a in assets])]
        return df.sort_values(["event_date", "asset_id"]).reset_index(drop=True)

    def fundamentals(self, assets: Sequence[AssetId]) -> pd.DataFrame:
        """As REPORTED at knowledge_time, not as later restated.

        This is the method that kills naive stores, and the one the planted
        restatements in truth.json exist to test.
        """
        if not _has_data(self._root, "fundamentals"):
            return _empty("fundamentals")
        df = self._run(_asof_sql("fundamentals"), glob=_glob(self._root, "fundamentals"))
        if len(assets):
            df = df[df["asset_id"].isin([str(a) for a in assets])]
        return df.sort_values(["asset_id", "period_end"]).reset_index(drop=True)

    def universe(self, universe_id: str) -> list[AssetId]:
        """Names that were live at knowledge_time, INCLUDING ones since delisted.

        An unknown universe is a KeyError, never an empty list — per the Store
        Protocol. Downstream, an empty universe reads as "nothing to trade
        today" and the backtest records a flat day instead of failing, which is
        precisely the silent wrong answer this project keeps refusing.

        The liveness rule matches FixtureStore deliberately (a name is in if it
        traded within the last five observed sessions) so the two stores can be
        asserted equal.
        """
        if universe_id != DEFAULT_UNIVERSE:
            raise KeyError(
                f"the store has only the {DEFAULT_UNIVERSE!r} universe, "
                f"asked for {universe_id!r}"
            )
        if not _has_data(self._root, "prices"):
            return []
        df = self._run(
            """
            SELECT asset_id, max(event_date) AS last_seen
            FROM read_parquet($glob, hive_partitioning = 1)
            WHERE event_date <= $kt
            GROUP BY asset_id
            """,
            glob=_glob(self._root, "prices"),
        )
        if df.empty:
            return []
        sessions = self._run(
            """
            SELECT DISTINCT event_date FROM read_parquet($glob, hive_partitioning = 1)
            WHERE event_date <= $kt ORDER BY event_date DESC LIMIT 5
            """,
            glob=_glob(self._root, "prices"),
        )
        cutoff = sessions["event_date"].min()
        return [AssetId(a) for a in df.loc[df["last_seen"] >= cutoff, "asset_id"]]

    def resolve_ticker(self, ticker: str) -> AssetId | None:
        """Which asset did this ticker mean at knowledge_time? None if none did.

        NOTE the honest limitation: identifiers are versioned on EVENT time
        (valid_from/valid_to), not knowledge time, so this answers "who held
        this ticker on that date" and not "who did we BELIEVE held it". Those
        differ whenever a mapping is backfilled after the fact. Closing the gap
        is P1-16; until then this is the one method on this view whose answer is
        not strictly point-in-time, and it is said out loud rather than assumed.
        """
        if not _has_data(self._root, "identifiers"):
            return None
        df = self._con.execute(
            """
            SELECT asset_id FROM read_parquet($glob)
            WHERE id_type = 'ticker' AND id_value = $ticker
              AND valid_from <= $kt
              AND (valid_to IS NULL OR valid_to > $kt)
            """,
            {
                "glob": _glob(self._root, "identifiers"),
                "ticker": ticker,
                "kt": self._kt.isoformat(),
            },
        ).df()
        return AssetId(df.iloc[0]["asset_id"]) if len(df) else None


class PitStore:
    """The single chokepoint for historical data access. Satisfies `Store`.

    There is deliberately no method to ask for "the current value" of anything.
    The honest question is the only question this API allows — the design
    prevents the mistake rather than relying on you to remember not to make it.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        if not self._root.exists():
            raise FileNotFoundError(
                f"no store at {self._root}. Build one with "
                "asetpay_core.store.layout.build_from_fixture()"
            )
        self._con = duckdb.connect(":memory:")

    def as_of(self, knowledge_time: date) -> PitStoreView:
        """Everything genuinely knowable on this date, and nothing else."""
        return PitStoreView(self._root, self._con, knowledge_time)

    def close(self) -> None:
        self._con.close()
