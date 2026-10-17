"""Polymarket liquidity-reward scoring (docs/POLYMARKET_API.md → Liquidity rewards).

S(v, s) = ((v − s)/v)² · b for an order at spread s (cents) from the adjusted mid, within max spread v.
Q_min = max(min(Q_one, Q_two), max(Q_one, Q_two)/c) if mid ∈ [0.10, 0.90], else min(Q_one, Q_two).
"""

from __future__ import annotations

from collections.abc import Sequence

Level = tuple[float, float]


def order_score(max_spread_c: float, spread_c: float, multiplier: float = 1.0) -> float:
    if max_spread_c <= 0 or spread_c >= max_spread_c or spread_c < 0:
        return 0.0
    return ((max_spread_c - spread_c) / max_spread_c) ** 2 * multiplier


def q_min(q_one: float, q_two: float, mid: float, c: float = 3.0) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q_one, q_two), max(q_one, q_two) / c)
    return min(q_one, q_two)


def side_score(levels: Sequence[Level], mid: float, max_spread_c: float, min_size: float) -> float:
    """Σ S·size for levels (bids or asks of one token) that qualify by spread and size."""
    total = 0.0
    for price, size in levels:
        if size < min_size:
            continue
        s_c = abs(price - mid) * 100
        total += order_score(max_spread_c, s_c) * size
    return total


def quote_q_min(
    bid: float | None, bid_size: float, ask: float | None, ask_size: float, mid: float, max_spread_c: float,
    min_size: float,
) -> float:
    """Q_min of our own two-sided quote on a single token."""
    q_one = side_score([(bid, bid_size)], mid, max_spread_c, min_size) if bid is not None else 0.0
    q_two = side_score([(ask, ask_size)], mid, max_spread_c, min_size) if ask is not None else 0.0
    return q_min(q_one, q_two, mid)


def book_q_min(bids: Sequence[Level], asks: Sequence[Level], mid: float, max_spread_c: float, min_size: float) -> float:
    """Approximate competitors' aggregate Q_min from the visible book (treats the book as one maker —
    an upper bound on competition, so reward estimates are conservative)."""
    return q_min(side_score(bids, mid, max_spread_c, min_size), side_score(asks, mid, max_spread_c, min_size), mid)


def expected_daily_reward(our_q: float, others_q: float, daily_pool_usd: float) -> float:
    """Expected reward if our share of Q_min held for the whole epoch."""
    tot = our_q + others_q
    return 0.0 if tot <= 0 else daily_pool_usd * our_q / tot
