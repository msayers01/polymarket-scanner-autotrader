"""Kelly sizing for binary contracts with parameter-uncertainty shrinkage.

Buying a share at all-in cost ``c`` that pays 1 with probability ``q``:
    f* = (q − c) / (1 − c)
Uncertainty shrinkage (Baker & McHale 2013): multiply by e²/(e² + s²), e = q − c, s = stderr(q).
"""

from __future__ import annotations

import math


def kelly_fraction(q: float, cost: float) -> float:
    """Growth-optimal bankroll fraction for buying one binary share at ``cost``; 0 if no edge."""
    if not (0.0 < cost < 1.0) or q <= cost:
        return 0.0
    return min((q - cost) / (1.0 - cost), 1.0)


def shrinkage_factor(edge: float, stderr: float) -> float:
    if stderr <= 0:
        return 1.0
    e2 = edge * edge
    return e2 / (e2 + stderr * stderr)


def sized_fraction(q: float, cost: float, stderr: float = 0.0, lam: float = 0.25) -> float:
    """λ-fractional Kelly with uncertainty shrinkage."""
    f = kelly_fraction(q, cost)
    if f <= 0:
        return 0.0
    return lam * f * shrinkage_factor(q - cost, stderr)


def kelly_shares(
    bankroll: float,
    q: float,
    cost: float,
    stderr: float = 0.0,
    lam: float = 0.25,
    max_usd: float = math.inf,
) -> float:
    """Number of shares to buy (unrounded); caps at ``max_usd`` of spend."""
    if bankroll <= 0 or cost <= 0:
        return 0.0
    usd = min(bankroll * sized_fraction(q, cost, stderr, lam), max_usd)
    return max(usd / cost, 0.0)


def expected_log_growth(f: float, q: float, cost: float) -> float:
    """E[log wealth] after staking fraction ``f`` on a share costing ``cost`` (for tests/diagnostics)."""
    if f <= 0:
        return 0.0
    win = 1.0 + f * (1.0 - cost) / cost
    lose = 1.0 - f
    if lose <= 0:
        return -math.inf
    return q * math.log(win) + (1 - q) * math.log(lose)
