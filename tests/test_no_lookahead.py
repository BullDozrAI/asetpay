"""P1-11 and P1-12 — the no-lookahead property, and proof it can fail.

THE PROPERTY
------------
For any feature f, any date d:

    f(store.as_of(d))  ==  f(a store containing only data from on or before d)

The second store is not filtered. It is built by SYMLINKING only the partitions
whose knowledge_date is at or before d, so the later data is physically absent
from the directory tree. Nothing in that query mentions knowledge_date at all.

That distinction is the entire value of this test. Every other test in the repo
checks the filter against itself: it asks the store for data and believes the
answer. This one asks two stores the same question, one of which *cannot* know
the future no matter what the SQL says, and demands the same answer. A wrong
WHERE clause, a wrong comparison operator, a timezone slip, a partition written
under the wrong date — all of them show up here and nowhere else.

Hypothesis generates the dates, because the bug is never on the date a human
would pick. The interesting ones are boundaries: the day a restatement was
filed, the day before, a date with no filings at all.

WHY THE FEATURES ARE PURE
-------------------------
A feature takes a StoreView and returns a value. It cannot consult the clock,
the filesystem, or a database — the AST check in scripts/check_feature_purity.py
enforces that for the real feature package (P1-18/P1-19). Purity is what makes
this property meaningful: if f could read `date.today()`, equality here would
prove nothing about lookahead.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from asetpay_core.store import PitStore, build_from_fixture
from asetpay_core.store.layout import PARTITIONED, UNPARTITIONED
from asetpay_core.store.pit import PitStoreView, _glob
from asetpay_core.synthetic import write

FIXTURE = Path("data/synthetic")

# The fixture spans 2019-01-01 to roughly 2023-12. Sampling inside it keeps
# every generated date meaningful; outside it every store is empty and the
# property holds trivially, which would make the test look healthier than it is.
FIRST, LAST = date(2019, 3, 1), date(2023, 6, 1)


# --------------------------------------------------------------- the features


def latest_eps_sum(view: PitStoreView) -> float:
    """Sum of the most recent eps figure per asset. Sensitive to restatements:
    if a revision leaks in early, this number changes."""
    df = view.fundamentals([])
    if df.empty:
        return 0.0
    latest = df.sort_values("period_end").groupby("asset_id").tail(1)
    return round(float(latest["value"].sum()), 9)


def known_fact_count(view: PitStoreView) -> int:
    """How many facts exist at all. Sensitive to a filter that is off by a day."""
    return len(view.fundamentals([]))


def universe_size(view: PitStoreView) -> int:
    """Sensitive to prices arriving early — a name that had not started trading
    yet would inflate this."""
    return len(view.universe("all"))


def mean_close_30d(view: PitStoreView) -> float:
    kt = view.knowledge_time
    df = view.prices([], kt - timedelta(days=30), kt)
    return 0.0 if df.empty else round(float(df["close"].mean()), 9)


FEATURES: dict[str, Callable[[PitStoreView], object]] = {
    "latest_eps_sum": latest_eps_sum,
    "known_fact_count": known_fact_count,
    "universe_size": universe_size,
    "mean_close_30d": mean_close_30d,
}


# ------------------------------------------------------------- truncation


def _truncate(full_root: Path, cut: date, dest: Path) -> Path:
    """A store containing ONLY partitions at or before `cut`.

    Real directories with symlinked files inside — the point is which
    partitions EXIST, and the later ones are simply not there. (Symlinking the
    directories themselves would be tidier but neither pathlib's `**` nor
    DuckDB's globbing reliably descends into a symlinked directory, so the
    truncated store would look empty and the test would pass for the wrong
    reason. That is the sort of false green this whole file exists to prevent.)
    """
    iso = cut.isoformat()
    for table in PARTITIONED:
        src = full_root / table
        (dest / table).mkdir(parents=True, exist_ok=True)
        if not src.exists():
            continue
        for part in src.iterdir():
            if not part.name.startswith("knowledge_date="):
                continue
            if part.name.split("=", 1)[1] > iso:
                continue
            out = dest / table / part.name
            out.mkdir(parents=True, exist_ok=True)
            for f in part.glob("*.parquet"):
                os.symlink(f, out / f.name)
    for table in UNPARTITIONED:
        src = full_root / table
        if not src.exists():
            continue
        (dest / table).mkdir(parents=True, exist_ok=True)
        for f in src.glob("*.parquet"):
            os.symlink(f, dest / table / f.name)
    return dest


@pytest.fixture(scope="module")
def full_root(tmp_path_factory):
    if not (FIXTURE / "truth.json").exists():
        write(FIXTURE)
    root = tmp_path_factory.mktemp("full")
    build_from_fixture(FIXTURE, root)
    return root


# ------------------------------------------------------------------- P1-11


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(d=st.dates(min_value=FIRST, max_value=LAST))
def test_no_feature_can_see_past_its_knowledge_date(full_root, tmp_path_factory, d):
    """P1-11. For any feature and any date, asking the full store as-of d must
    equal asking a store from which everything after d was physically removed.

    If these ever disagree, the as-of filter is reading something it should not
    — and the difference is the lookahead, measured rather than suspected.
    """
    dest = tmp_path_factory.mktemp("trunc")
    truncated = PitStore(_truncate(full_root, d, dest))
    full = PitStore(full_root)
    try:
        fv, tv = full.as_of(d), truncated.as_of(d)
        for name, f in FEATURES.items():
            assert f(fv) == f(tv), f"{name} differs at {d}: lookahead"
    finally:
        truncated.close()
        full.close()


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_the_boundary_days_around_a_restatement(full_root, tmp_path_factory, offset):
    """The dates a human would not think to pick, picked deliberately. A filter
    using `<` where it should use `<=` passes every other test in this file and
    fails exactly here."""
    import json

    truth = json.loads((FIXTURE / "truth.json").read_text())
    d = date.fromisoformat(truth["restatements"][0]["restated_known"]) + timedelta(days=offset)
    dest = tmp_path_factory.mktemp(f"b{offset + 1}")
    truncated = PitStore(_truncate(full_root, d, dest))
    full = PitStore(full_root)
    try:
        for name, f in FEATURES.items():
            assert f(full.as_of(d)) == f(truncated.as_of(d)), f"{name} at {d}"
    finally:
        truncated.close()
        full.close()


# ------------------------------------------------------------------- P1-12


class _LeakyView(PitStoreView):
    """The same store with the knowledge_date filter deleted — the naive
    implementation, written out so the property test has something to catch."""

    def fundamentals(self, assets):
        keys = ", ".join(PARTITIONED["fundamentals"])
        df = self._con.execute(
            f"""
            SELECT * FROM read_parquet($glob, hive_partitioning = 1)
            QUALIFY row_number() OVER (
                PARTITION BY {keys} ORDER BY knowledge_date DESC
            ) = 1
            """,
            {"glob": _glob(self._root, "fundamentals")},
        ).df()
        return df.sort_values(["asset_id", "period_end"]).reset_index(drop=True)


def test_deleting_the_knowledge_filter_makes_the_property_fail(full_root, tmp_path_factory):
    """P1-12 — the negative control, and the reason P1-11 is evidence.

    A test that has never failed may be incapable of failing. This removes the
    `WHERE knowledge_date <= $kt` clause and asserts the property test CATCHES
    it. If this ever passes, P1-11 has stopped discriminating and every
    guarantee resting on it is void.

    Note what the leak looks like: not a crash, not an error — a slightly
    different number. That is the whole danger. The restated figure is a better
    estimate of the truth, so a backtest reading it produces *better* results,
    and nobody investigates a pleasant surprise.
    """
    import json

    truth = json.loads((FIXTURE / "truth.json").read_text())
    d = date.fromisoformat(truth["restatements"][0]["restated_known"]) - timedelta(days=1)

    dest = tmp_path_factory.mktemp("leak")
    truncated = PitStore(_truncate(full_root, d, dest))
    full = PitStore(full_root)
    try:
        honest = full.as_of(d)
        leaky = _LeakyView(full._root, full._con, d)
        trunc = truncated.as_of(d)

        assert latest_eps_sum(honest) == latest_eps_sum(trunc), (
            "the honest store must pass the property before we test the leaky one"
        )
        assert latest_eps_sum(leaky) != latest_eps_sum(trunc), (
            "the leaky store passed the no-lookahead property — P1-11 is not "
            "capable of detecting lookahead, and proves nothing"
        )
    finally:
        truncated.close()
        full.close()


def test_the_leak_is_small_which_is_why_it_survives_review(full_root):
    """Quantifies the negative control. The point is not that the numbers differ
    but that they differ by little — a few percent on one metric, invisible in a
    tearsheet, and in the flattering direction."""
    import json

    truth = json.loads((FIXTURE / "truth.json").read_text())
    r = truth["restatements"][0]
    d = date.fromisoformat(r["restated_known"]) - timedelta(days=1)

    full = PitStore(full_root)
    try:
        honest = latest_eps_sum(full.as_of(d))
        leaky = latest_eps_sum(_LeakyView(full._root, full._con, d))
        assert honest != leaky
        if honest:
            assert abs(leaky - honest) / abs(honest) < 0.10, (
                "a leak this large would be noticed; the dangerous ones are small"
            )
    finally:
        full.close()
