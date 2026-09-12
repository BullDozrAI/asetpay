# Risk limits

*Set 2026-09-09, while calm and before any money is at risk. That timing is the
entire point: a limit chosen while looking at a loss is not a limit.*

Both signatures to invoke. **Neither signature to override.**

---

## The numbers

| Trigger | Threshold | Action |
|---|---|---|
| **Daily circuit breaker** | −2% of trading capital in one day | Halt for the day. Investigate before resuming. |
| **Total drawdown kill** | −10% from peak | Go flat. Halt. Post-mortem. Both signatures to restart. |
| **Backtest divergence** | Live IR below half the backtested IR, 3 consecutive months | The model of the market is wrong, not mistuned. Return to Phase 2. |
| **Agent decay** | Marginal contribution negative for 2 consecutive quarters | Remove the agent. Do not tune it. |
| **No surviving edge** | No agent survives costs out-of-sample | Ship allocation-only. Do not add layers hoping to find edge. |

### Why −10%

A strategy targeting ~8–10% annual volatility produces 5–8% drawdowns as
ordinary noise. A tighter limit would fire on nothing and get widened, which
teaches everyone that limits get widened. Ten percent is far enough out to carry
information and close enough in that 90% of the capital survives the lesson.

### The limit that matters more than any of these

**Allocate only capital you could lose entirely.** Every threshold above is
calibrated as tuition at that size. At any larger size they stop being a
learning mechanism and become a disaster with a number attached.

Phase 4 starts at the smallest size where costs are still representative.

---

## Restart conditions

A halt with no defined path back becomes either permanent or arbitrary, and
arbitrary is how pre-commitment dies. So:

- **After a daily breaker** — resume next session once the cause is identified
  and written down. An unexplained breaker is not a resumable one.
- **After a drawdown kill** — flat until a post-mortem names the cause, the fix
  ships, and both people sign. Restart at or below the previous allocation,
  never above it.
- **After a divergence halt** — no restart from Phase 4. Re-enter at Phase 2 and
  re-earn Gate 2a.

---

## Decisions of record — 2026-09-09

1. **Broker: Alpaca, all phases.** Supersedes the moomoo MY / IBKR line in
   `where-we-are.md`. P1-01 is not the critical-path long-lead item it was
   described as. *Open:* confirm the account can actually be **funded** from
   Malaysia and Sri Lanka — opening and funding are different questions, and SL
   outward-investment controls are the specific risk. A "no" reshapes Phase 4,
   so it is worth learning now rather than at month 9.

2. **Environment isolation: `models` and `backtest` stay out of the workspace.**
   A uv workspace resolves every member against one lockfile, so torch and
   nautilus would have to co-resolve — the exact failure J-01 exists to prevent.
   They become standalone projects with their own locks, depending on
   `asetpay-contracts` by path. Guarded now by
   `tests/test_ci_gates.py::test_the_heavy_dependencies_never_enter_the_shared_lock`,
   which fails at the moment of introduction rather than at the moment of
   collision.

3. **`Signal.horizon_days` is TRADING days**, counted as rows of price data per
   asset — which is what `harness.forward_returns` already did while the
   contract left the unit undefined. Documented, not changed. **Flagged for J-03
   sign-off:** this edits `contracts`, and that file's own rule requires both
   people in the room.

4. **"Zero losses" is retired as an objective.** It is unreachable in any
   strategy with positive expected excess return, and strategies that appear to
   achieve it are selling tail risk — smooth until they are catastrophic. An
   unusually smooth equity curve is treated here as a bug report, not a
   milestone. The numbers above replace it.

---

## Decisions of record — 2026-09-12 (J-03)

5. **Long-only.** The system does not short. Consequences: `CostModel` carries
   no borrow-cost term, and gross exposure equals net exposure in P2-31's risk
   overlay. *Why:* it sits consistently with the mandate's zero-leverage rule,
   and recommend-only launch means a human places these trades in their own
   account — asking a retail user to short is a materially bigger ask than
   asking them to buy. **Cost accepted:** roughly half a ranking signal's usable
   information lives in identifying the losers, and long-only forgoes it.
   Adding shorts later is a Protocol change, which is the right amount of
   friction for a decision this size.

6. **`meta` and `confidence` become structurally unreadable at P2-25.** The
   combiner takes a plain `(as_of, asset_id, agent_id, score)` frame projected
   from Signals — not `Signal` objects. *Why:* today "nothing may read meta" is
   a promise kept by memory. If the combiner never receives the field, `meta`
   is not unread but absent, and `confidence` cannot be leaned on because it
   never crosses the boundary. One decision, two rules enforced by physics
   rather than discipline. **How to apply:** when building P2-25, define the
   projection once at the boundary; do not pass `list[Signal]` inward.

7. **`features_hash` becomes mandatory when the first agent reads a
   `FeatureSet`** (P1-18/P1-20), not before. *Why:* momentum against
   `FixtureStore` computes from prices, so there are no feature bytes to
   fingerprint — requiring a hash now would mean requiring a lie. **How to
   apply:** it lands *together* with the `OMP_NUM_THREADS=1` and
   `PYTHONHASHSEED=0` pins. Without those, identical inputs hash differently on
   different machines, which buildflow warns "looks like a data bug and takes a
   week to find" — so requiring the hash without the pins would manufacture
   exactly that bug.
