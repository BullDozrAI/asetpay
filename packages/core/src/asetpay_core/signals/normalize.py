"""P2-09 — the layer that makes two different agents comparable at all.

Agents compute on whatever scale suits them: a z-score, a probability in [0, 1],
a log-odds, a raw dollar spread. Combining those directly is meaningless — the
agent with the widest internal units would dominate the blend for no reason
other than its units. This module maps all of them onto one common scale, which
is what P2-25's combiner needs before it can weight anything.

WHY RANK FIRST
--------------
Ranking discards everything except the ORDER of the scores. That buys three
properties, and they are the whole reason this layer exists:

  invariant to monotone rescaling   exp(x), 2x + 7 and x all normalize to the
                                    same thing, so an agent can be reimplemented
                                    or rescaled without disturbing the combiner
  robust to outliers                one absurd score moves one rank, not the
                                    entire distribution's mean and variance
  stable as distributions drift     an agent whose raw spread widens over time
                                    does not silently gain weight

TWO FUNCTIONS, BECAUSE THERE ARE TWO SCALES
-------------------------------------------
The plan specifies clipping at ±3 sigma. `Signal.score` is validated to
[-1, 1]. Those ranges are incompatible, so the work splits:

  normalize()        -> z in [-clip, clip].  The combiner works here, because
                        z-space is where the covariance maths is meaningful.
  to_signal_score()  -> [-1, 1].             What an agent emits.

The second is a linear rescale of the first, so nothing is lost in either
direction: multiply by `clip` to get back.

The rank -> normal step is the van der Waerden transform, Phi^-1((r - 0.5) / n),
rather than rank -> uniform -> standardize. Two reasons. A uniform's extreme is
sqrt(3) ~ 1.73, so a ±3 sigma clip could never bind and would be dead code. And
the fixture's planted IC is defined against NORMAL signals, so normal scores
keep a measured Pearson IC comparable to what truth.json says was planted.
"""

from __future__ import annotations

import pandas as pd
from scipy.special import ndtri


def normalize(
    scores: pd.DataFrame,
    *,
    score_col: str = "score",
    date_col: str = "as_of",
    clip: float = 3.0,
) -> pd.DataFrame:
    """Cross-sectional rank -> normal scores -> clip, one day at a time.

    Returns a copy with `score_col` replaced. Every other column is preserved,
    and the long-frame convention matches evaluation.harness, so the output
    feeds straight back into information_coefficient with no reshaping.

    CROSS-SECTIONAL means within each `date_col` group and never across the
    panel. Standardizing over the whole history would let a day on which every
    name scored high stay high after normalization, which reintroduces exactly
    the market-direction exposure this layer exists to strip out.

    NaN scores stay NaN and take no part in that day's ranks. A day on which
    every score is identical yields all zeros — the honest answer, since ranking
    identical values reveals no cross-sectional information.
    """
    if clip <= 0:
        raise ValueError(f"clip must be positive, got {clip!r}")

    out = scores.copy()
    grouped = out.groupby(date_col, observed=True)[score_col]

    # method="average" and not "first": "first" breaks ties by row order, which
    # would make the output depend on how the caller happened to sort the frame.
    rank = grouped.rank(method="average")
    n = grouped.transform("count")

    uniform = (rank - 0.5) / n
    out[score_col] = pd.Series(ndtri(uniform.to_numpy()), index=out.index).clip(-clip, clip)
    return out


def to_signal_score(z: pd.Series, *, clip: float = 3.0) -> pd.Series:
    """Map clipped z-scores onto the [-1, 1] domain Signal.score validates.

    Pass the same `clip` used for normalize(), or ±1 stops meaning "the most
    extreme name we were willing to represent" and Signal will reject the row.
    That rejection is deliberate and worth keeping loud: a score outside [-1, 1]
    means the caller skipped normalize(), which is a real bug rather than a
    rounding inconvenience.
    """
    return z / clip
