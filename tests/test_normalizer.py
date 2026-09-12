"""P2-09 — the normalizer, and the properties that are the reason it exists.

The done-when in the plan is "a z-score agent and a probability agent become
combinable". That is a claim about INVARIANCE, not about output values, so most
of these tests assert that two differently-scaled inputs produce identical
output rather than checking any particular number.

The one that matters most is the cross-sectional test. Standardizing across the
whole panel instead of within each day is a single-word bug that leaves every
individual number looking plausible, and it quietly puts market direction back
into scores this layer exists to strip it out of.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from asetpay_contracts import AssetId, Signal
from asetpay_core.evaluation import information_coefficient
from asetpay_core.signals import normalize, to_signal_score
from asetpay_core.synthetic import GeneratorConfig, generate


def _scores(data, col: str = "planted_signal_a"):
    return data["signals"][["as_of", "asset_id", col]].rename(columns={col: "score"})


def _one_day(values, day: str = "2026-01-05"):
    return pd.DataFrame(
        {
            "as_of": [day] * len(values),
            "asset_id": [f"asset-{i}" for i in range(len(values))],
            "score": values,
        }
    )


# --------------------------------------------------------------------------
# The headline property: only the ordering survives
# --------------------------------------------------------------------------


def test_normalization_is_invariant_to_monotone_rescaling() -> None:
    """exp(x), 2x + 7 and x must all normalize identically.

    This is the entire reason ranking comes first. It means an agent can be
    rescaled, reimplemented, or switched to a different internal unit without
    changing a single number the combiner sees — so P2-25's weights stay
    attached to the agent's ORDERING skill and not to its units.
    """
    data = generate(GeneratorConfig(n_assets=200, n_years=1))
    raw = _scores(data)

    affine = raw.copy()
    affine["score"] = 2.0 * affine["score"] + 7.0

    exponential = raw.copy()
    exponential["score"] = np.exp(exponential["score"])

    base = normalize(raw)["score"].to_numpy()
    np.testing.assert_allclose(normalize(affine)["score"].to_numpy(), base, atol=1e-12)
    np.testing.assert_allclose(normalize(exponential)["score"].to_numpy(), base, atol=1e-12)


def test_a_probability_agent_and_a_z_score_agent_become_combinable() -> None:
    """The plan's literal done-when.

    One agent emits a probability in (0, 1), another emits an unbounded z-score,
    both expressing the same underlying view. Before this layer their numbers
    could not be blended at all — the z-score agent's wider units would dominate
    any weighted average for no reason but its units.
    """
    data = generate(GeneratorConfig(n_assets=200, n_years=1))
    z_agent = _scores(data)

    probability_agent = z_agent.copy()
    probability_agent["score"] = 1.0 / (1.0 + np.exp(-probability_agent["score"]))
    assert probability_agent["score"].between(0.0, 1.0).all(), "not a probability"

    np.testing.assert_allclose(
        normalize(probability_agent)["score"].to_numpy(),
        normalize(z_agent)["score"].to_numpy(),
        atol=1e-12,
    )


# --------------------------------------------------------------------------
# It must not destroy the signal it is normalizing
# --------------------------------------------------------------------------


def test_normalizing_preserves_measured_rank_ic() -> None:
    """A layer that improved comparability by damaging predictive power would be
    worse than no layer at all.

    The difference is exactly zero, and that is the point: the transform IS the
    rank, monotonically restretched, so ranking its output returns the ranks it
    started from. Clipping does not disturb that here — at 400 names it catches
    one name in each tail and leaves both of them the most extreme.

    The tolerance is a guard, not slack. Past roughly 1100 names the ±3 bound
    starts catching the top TWO names in a single tail, which ties them for real
    and would shift rank IC very slightly.
    """
    data = generate(GeneratorConfig(ic_a=0.05, n_assets=400, n_years=2))
    raw = _scores(data)

    before = information_coefficient(raw, data["prices"], agent_id="raw")
    after = information_coefficient(normalize(raw), data["prices"], agent_id="normalized")

    assert before.mean_rank_ic > 0.02, "fixture produced no signal to preserve"
    assert after.mean_rank_ic == pytest.approx(before.mean_rank_ic, abs=1e-9)


# --------------------------------------------------------------------------
# Cross-sectional, never pooled
# --------------------------------------------------------------------------


def test_each_day_is_normalized_independently_of_every_other_day() -> None:
    """The bug this catches: standardizing over the whole panel.

    Give one day uniformly high raw scores and another uniformly low ones. Pooled
    standardization leaves the first day positive and the second negative, which
    is a market-direction bet wearing a cross-sectional disguise. Both days must
    come out centred on zero.
    """
    high = _one_day([100.0, 101.0, 102.0, 103.0], day="2026-01-05")
    low = _one_day([-100.0, -99.0, -98.0, -97.0], day="2026-01-06")

    out = normalize(pd.concat([high, low], ignore_index=True))

    for day in ("2026-01-05", "2026-01-06"):
        scores = out.loc[out["as_of"] == day, "score"]
        assert scores.mean() == pytest.approx(0.0, abs=1e-12), f"{day} is not centred"
        assert scores.min() < 0 < scores.max()


def test_the_output_is_approximately_standard_normal() -> None:
    """The "then standardize" half of the transform, checked per day.

    Mean is zero by construction — Phi^-1((r - 0.5)/n) is odd about r = (n+1)/2 —
    so it is asserted tightly. The standard deviation approaches 1 from below as
    n grows, which is why it gets a looser bound.
    """
    data = generate(GeneratorConfig(n_assets=400, n_years=1))
    out = normalize(_scores(data), clip=10.0)  # clip wide so it cannot bind

    per_day = out.groupby("as_of")["score"]
    assert per_day.mean().abs().max() < 1e-9
    assert abs(per_day.std().mean() - 1.0) < 0.05


# --------------------------------------------------------------------------
# The handoff to the Signal contract
# --------------------------------------------------------------------------


def test_to_signal_score_produces_a_score_the_contract_accepts() -> None:
    """P2-09 exists so that P2-10 can emit a Signal at all.

    Signal.__post_init__ rejects anything outside [-1, 1], so this constructs a
    real Signal from real normalized output. If the two ranges ever drift apart
    again, this fails here rather than inside the first agent someone writes.
    """
    data = generate(GeneratorConfig(n_assets=200, n_years=1))
    out = normalize(_scores(data), clip=3.0)

    assert out["score"].abs().max() <= 3.0
    scores = to_signal_score(out["score"], clip=3.0)
    assert scores.abs().max() <= 1.0

    row = out.iloc[0]
    emitted = Signal(
        as_of=date.fromisoformat(row["as_of"]),
        asset=AssetId(row["asset_id"]),
        agent_id="normalizer_smoke_v1",
        score=float(scores.iloc[0]),
        horizon_days=1,
    )
    assert -1.0 <= emitted.score <= 1.0


def test_an_unclipped_z_score_is_rejected_rather_than_silently_squashed() -> None:
    """The negative control for the test above.

    Skipping normalize() and handing raw z straight to to_signal_score must NOT
    quietly produce a valid-looking score. Signal refusing it is the desired
    behaviour: the caller has a real bug, and a silent clamp would hide it.
    """
    with pytest.raises(ValueError, match="normalized"):
        Signal(
            as_of=date(2026, 1, 5),
            asset=AssetId("asset-0"),
            agent_id="forgot_to_normalize_v1",
            score=float(to_signal_score(pd.Series([7.5]), clip=3.0).iloc[0]),
            horizon_days=1,
        )


# --------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------


def test_a_day_with_no_dispersion_normalizes_to_zero() -> None:
    """Every name scored the same, so the ranking reveals nothing.

    Zero is the honest answer and the safe one — it expresses no view. NaN would
    be wrong (the agent did produce output) and a crash would be worse: a single
    degenerate day should not take down a backtest.
    """
    out = normalize(_one_day([2.5, 2.5, 2.5, 2.5]))
    assert (out["score"] == 0.0).all()
    assert out["score"].notna().all()


def test_a_missing_score_stays_missing_and_does_not_shift_the_others() -> None:
    """An agent with partial coverage must not distort the names it did score.

    The three real names should normalize exactly as they would have if the
    fourth had never been in the frame.
    """
    with_gap = normalize(_one_day([1.0, 2.0, np.nan, 3.0]))
    without = normalize(_one_day([1.0, 2.0, 3.0]))

    assert np.isnan(with_gap["score"].iloc[2])
    np.testing.assert_allclose(
        with_gap["score"].dropna().to_numpy(), without["score"].to_numpy(), atol=1e-12
    )


def test_output_does_not_depend_on_input_row_order() -> None:
    """Catches rank(method="first"), which breaks ties by position.

    Row order is an accident of how a frame was built. If it changed the scores,
    two runs over the same data could disagree and features_hash would stop
    meaning anything.
    """
    data = generate(GeneratorConfig(n_assets=100, n_years=1))
    raw = _scores(data)
    shuffled = raw.sample(frac=1.0, random_state=0).reset_index(drop=True)

    merged = normalize(raw).merge(
        normalize(shuffled), on=["as_of", "asset_id"], suffixes=("_a", "_b")
    )
    assert len(merged) == len(raw)
    np.testing.assert_allclose(
        merged["score_a"].to_numpy(), merged["score_b"].to_numpy(), atol=1e-12
    )


def test_a_non_positive_clip_is_refused() -> None:
    """clip=-3 would invert the bounds and return silent garbage rather than
    failing, which is the one outcome worth a guard here."""
    with pytest.raises(ValueError, match="clip must be positive"):
        normalize(_one_day([1.0, 2.0, 3.0]), clip=0.0)
