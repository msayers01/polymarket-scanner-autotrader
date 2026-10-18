# ADR-0003: Floats internally, explicit rounding at boundaries

**Status:** accepted (2026-10-17)

## Context
Prices are probabilities on a 0.01 or 0.001 tick. Sizes are shares (2 dp), and cash is USDC (6 dp).
Quant code (Φ, logit, Kelly) is naturally floating point. `Decimal` everywhere would add friction
and slow the hot path, while naked floats risk tick-misaligned orders and P&L drift.

## Decision
* Use `float` everywhere internally.
* Round **at boundaries only**, with helpers in `core/types.py`:
  `round_price(p, tick, side)` (bids round down, asks round up, for conservative maker prices),
  `round_size(c)` (floor to 0.01 shares) and `round_usd(x)` (6 dp).
* The ledger stores money rounded to 6 dp in SQLite. The SDK boundary converts to `str` with exact formatting.

## Consequences
* Double precision (~15 significant digits) is ample for account sizes < $10⁸.
* Tests assert tick alignment of every order emitted by strategies.
