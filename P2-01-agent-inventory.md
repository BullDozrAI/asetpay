# P2-01 — Agent inventory

*2026-09-29 · P2 · input to P2-16 (the correlation matrix)*

## 1. Existing agents: none

The build flow's P2-01 assumed some "fragmented agents you already have". There
are none. Checked: `packages/` in this repo (only the normalizer under
`signals/`), the `earlydraft` repo (data collectors and a SQLite store; both
`strategies/` folders empty), the BullDozrAI GitHub org (`asetpay`,
`earlydraft` only, no forks), and GimsaraK's personal GitHub account. Nothing to
inventory, so this page is an assessment of the planned and candidate agents
instead.

## 2. The three tests every agent must pass

1. **Free at 500 names.** The constraint that killed FinBERT (P2-15). Anything
   metered per name, or needing news at breadth, fails.
2. **Emits a score, not a trade.** The Signal contract: a number in [-1, 1],
   cross-sectional, and nothing downstream may size or order from it directly.
3. **Measurable by the harness.** Scorable over the synthetic fixture and the
   point-in-time store across the full history, with no input the store cannot
   partition by knowledge date. Otherwise its IC cannot be trusted and it never
   reaches the correlation matrix.

## 3. The inventory

| Agent | Output | Frequency | Coverage (free) | Test 1 | Test 2 | Test 3 | Status |
|---|---|---|---|---|---|---|---|
| Momentum (P2-10) | Rank score from trailing return | Daily | 500, from snapshots | ✓ | ✓ | ✓ | **Build first.** The benchmark everything else must beat |
| Reversal (P2-11) | Rank score from short-term return | Daily | 500 | ✓ | ✓ | ✓ | After momentum. Expected negatively correlated with it |
| Volatility (P2-13) | Rank score from realized vol | Daily | 500 | ✓ | ✓ | ✓ | After momentum |
| Value (P2-12) | Rank score from PIT fundamentals | Quarterly, carried daily (≈4 obs/yr, see P2-07) | 500 US filers, once P1-14 lands | ✓ (after P1-14) | ✓ | ✓, must pass P1-11 | Blocked on P1-14 (deferred) |
| [Kronos](https://github.com/shiyu-coder/Kronos) (P2-14) | Price forecast, ranked cross-sectionally | Daily, batched (`predict_batch`) | 500, OHLCV only, CPU, ~$0.50/mo | ✓ | ✓ (rank, never act) | ✓ | After momentum, own env (J-01b). Survives only if ρ < 0.7 with momentum |
| FinBERT (P2-15) | News sentiment score | Daily | Not at 500 names | ✗ | ✓ | ✗ | **Deferred**, decision already recorded |
| TradingAgents fork | One trade decision per ticker | Daily, sequential per ticker | Not at 500 names; dozens of LLM calls each | ✗ | ✗ | ✗ | **Out.** See §4 |
| Vibe-Trading | Strategy code, backtests, reports | n/a | n/a | n/a | n/a | n/a | **Not an agent.** See §5 |

## 4. Decision: TradingAgents (TauricResearch)

<https://github.com/TauricResearch/TradingAgents> · README read 2026-09-29

**Out, as an agent.** Recorded before any code was written, as the build flow asks.

- Its free-data inputs (technical, fundamentals) are the same bars and filings
  momentum and value read: a duplicate by construction, and the ρ > 0.7 rule
  would remove it.
- Its novel inputs (news, social sentiment) are unavailable at 500 names, the
  same constraint as FinBERT.
- It emits a trade with timing and size, not a score. Fails the Signal contract.
- An LLM's training data is lookahead that no point-in-time store can filter:
  a model trained through 2025 cannot analyse June 2020 without knowing what
  followed. Its backtest IC is contaminated in the flattering direction by an
  unknown amount, which is the one failure this architecture exists to prevent.
  Outputs are also non-deterministic, so `features_hash` and the experiment
  log's `code_sha` cannot pin a result.
- Cost: assuming ~20 LLM calls per ticker-day (the README gives no count, but
  four analysts, a researcher debate, a trader and a risk stage each make
  several), 20 × 500 names × 1,250 days ≈ 12.5M calls per five-year backtest,
  against a $0/month budget. Kronos costs ~$0.50/month.

**The one surviving role:** commentary after the ranker, on a handful of top
names, for the dashboard. Read-only, downstream of every decision, never fed
back into a weight (P1-38's rule). That is a P1 dashboard question for weeks
9–15, not a P2 agent.

**Revisit only if all three change:** free news at breadth, budget, and a model
with a verifiable training cutoff before the backtest window.

## 5. Decision: Vibe-Trading (HKUDS)

<https://github.com/HKUDS/Vibe-Trading> · README read 2026-09-29

**Not an agent.** It is a research assistant that turns prompts into strategy
code, backtests and reports, with its own engines and factor library. It
overlaps with what this project deliberately builds itself (harness, purged
walk-forward, cost model) rather than adding a signal. Possible use: a source
of factor *ideas*, each of which would still be written as an in-house agent
and made to beat momentum. Not mentioned anywhere in the plan.

## 6. What this feeds

- **P2-16/18:** the correlation matrix has five candidate rows (momentum,
  reversal, volatility, value, Kronos). The Kronos-versus-momentum decision is
  the phase's real output.
- **P2-10 next.** Nothing here changes the spine order.
