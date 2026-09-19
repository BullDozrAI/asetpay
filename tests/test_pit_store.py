"""P1-09 — does the store return what was knowable, and nothing else?

The synthetic fixture is the only dataset where the right answer is written
down. truth.json records, for each planted restatement, the original value, the
revised value, and the two dates they became knowable. So these are not
plausibility checks — every assertion has a number behind it.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from asetpay_contracts import AssetId
from asetpay_core.store import FixtureStore, PitStore, build_from_fixture
from asetpay_core.synthetic import write

FIXTURE = Path("data/synthetic")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    if not (FIXTURE / "truth.json").exists():
        write(FIXTURE)
    root = tmp_path_factory.mktemp("pit")
    counts = build_from_fixture(FIXTURE, root)
    assert counts["prices"] > 100, "expected one partition per knowledge date"
    store = PitStore(root)
    yield store, FixtureStore(FIXTURE), json.loads((FIXTURE / "truth.json").read_text())
    store.close()


def test_asof_returns_the_pre_restatement_figure(built):
    """P1-09's done-when: `as_of(d)` on a planted restatement returns the
    PRE-restatement figure for every d before the revision was filed.

    A store that overwrites returns the revised number for dates before the
    revision existed. Nothing crashes; the backtest just trades on figures
    nobody had, and looks better than it should.
    """
    store, _, truth = built
    checked = 0
    for r in truth["restatements"]:
        revised = date.fromisoformat(r["restated_known"])
        before = store.as_of(revised - timedelta(days=1)).fundamentals(
            [AssetId(r["asset_id"])]
        )
        after = store.as_of(revised).fundamentals([AssetId(r["asset_id"])])

        row_b = before[before["period_end"] == r["period_end"]]
        row_a = after[after["period_end"] == r["period_end"]]
        assert len(row_b) == 1, "a restated period must resolve to exactly one row"
        assert len(row_a) == 1

        assert row_b.iloc[0]["value"] == pytest.approx(r["original_value"]), (
            f"as_of before {revised} returned the RESTATED value — lookahead"
        )
        assert row_a.iloc[0]["value"] == pytest.approx(r["restated_value"])
        checked += 1
    assert checked >= 5, f"only {checked} restatements exercised"


def test_a_figure_is_invisible_before_it_was_filed(built):
    """The other direction: nothing at all before first_known. A store leaking
    the value early is as wrong as one leaking a revision."""
    store, _, truth = built
    r = truth["restatements"][0]
    view = store.as_of(date.fromisoformat(r["first_known"]) - timedelta(days=1))
    got = view.fundamentals([AssetId(r["asset_id"])])
    assert got[got["period_end"] == r["period_end"]].empty


def test_only_one_row_survives_per_fact(built):
    """What the QUALIFY clause buys. Filtering on knowledge_date alone returns
    BOTH versions of a restated figure."""
    store, _, _ = built
    df = store.as_of(date(2021, 6, 15)).fundamentals([])
    assert not df.duplicated(subset=["asset_id", "period_end", "metric"]).any()


@pytest.mark.parametrize(
    "d",
    [
        date(2019, 7, 1),
        date(2020, 5, 13),
        date(2021, 1, 4),
        date(2022, 8, 25),
        date(2023, 6, 15),
    ],
)
def test_pit_store_agrees_with_the_fixture_store(built, d):
    """P1-10 promises that when the real store lands, P2's code does not change.
    That promise is worth something only if the two answer identically.

    The dates are not arbitrary: 2020-05-13 is a planted restatement becoming
    known, 2022-08-25 a planted delisting.
    """
    pit, fix, _ = built
    key = ["asset_id", "period_end", "metric"]
    a = pit.as_of(d).fundamentals([]).sort_values(key).reset_index(drop=True)
    b = fix.as_of(d).fundamentals([]).sort_values(key).reset_index(drop=True)
    pd.testing.assert_frame_equal(
        a[[*key, "value"]], b[[*key, "value"]], check_dtype=False, atol=1e-9
    )


def test_prices_agree_with_the_fixture_store(built):
    pit, fix, _ = built
    d, start = date(2021, 3, 1), date(2021, 1, 1)
    key = ["event_date", "asset_id"]
    a = pit.as_of(d).prices([], start, d).sort_values(key).reset_index(drop=True)
    b = fix.as_of(d).prices([], start, d).sort_values(key).reset_index(drop=True)
    pd.testing.assert_frame_equal(
        a[[*key, "close"]], b[[*key, "close"]], check_dtype=False, atol=1e-9
    )


def test_universe_agrees_with_the_fixture_store(built):
    pit, fix, _ = built
    d = date(2021, 6, 15)
    assert sorted(pit.as_of(d).universe("all")) == sorted(fix.as_of(d).universe("all"))


def test_an_unknown_universe_raises_rather_than_returning_empty(built):
    """Per the Store Protocol. An empty list reads downstream as "nothing to
    trade today" and the backtest records a flat day instead of failing."""
    store, _, _ = built
    with pytest.raises(KeyError, match="sp500"):
        store.as_of(date(2021, 6, 15)).universe("sp500")


def test_knowledge_only_accumulates(built):
    """Later views know at least as much. If knowing more ever removed a fact,
    partition pruning would be dropping data it should have read."""
    store, _, _ = built
    early = store.as_of(date(2020, 1, 2)).fundamentals([])
    late = store.as_of(date(2022, 1, 3)).fundamentals([])
    assert set(zip(early["asset_id"], early["period_end"], strict=True)) <= set(
        zip(late["asset_id"], late["period_end"], strict=True)
    )


def test_the_view_carries_its_knowledge_time(built):
    store, _, _ = built
    assert store.as_of(date(2021, 6, 15)).knowledge_time == date(2021, 6, 15)


def test_a_missing_store_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError, match="no store at"):
        PitStore(tmp_path / "nope")


def test_a_store_that_knows_nothing_yet_says_so_rather_than_crashing(tmp_path):
    """Found by the P1-11 property test, which truncated the store to a date
    before the first filing.

    DuckDB's read_parquet raises when a glob matches nothing, so `as_of()` on
    any date before the history begins used to crash. Knowing nothing is a
    legitimate answer to an honest question — and it is the answer for every
    date in the first weeks of a real store's life.
    """
    for table in ("prices", "fundamentals", "identifiers"):
        (tmp_path / table).mkdir(parents=True)
    store = PitStore(tmp_path)
    try:
        view = store.as_of(date(2019, 1, 1))
        assert view.fundamentals([]).empty
        assert view.prices([], date(2018, 1, 1), date(2019, 1, 1)).empty
        assert view.universe("all") == []
        assert view.resolve_ticker("AAAA") is None
    finally:
        store.close()


def test_building_a_store_from_nothing_refuses(tmp_path):
    """`make store` on a machine without the fixture printed `{}` and exited 0.
    An empty store answers every as-of query with "nothing was knowable", which
    is indistinguishable from a correct answer about an early date."""
    from asetpay_core.store import build_from_fixture as build

    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit, match="make fixture"):
        build(tmp_path / "empty", tmp_path / "out")
