"""Order-book microstructure helpers: depth walks, microprice, imbalance."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

Level = tuple[float, float]


@dataclass(frozen=True, slots=True)
class Walk:
    filled: float  # shares obtainable
    cost: float  # Σ price·size (excl. fees)
    vwap: float  # cost / filled (0 if nothing)
    marginal_price: float  # price of the last level touched
    levels_used: int


def walk_book(levels: Sequence[Level], qty: float, limit: float | None = None, is_buy: bool = True) -> Walk:
    """Take up to ``qty`` shares from ``levels`` (asks ascending for buys, bids descending for sells).

    ``limit`` stops the walk at a worst acceptable price.
    """
    remaining, cost, filled, marginal, used = qty, 0.0, 0.0, 0.0, 0
    for price, size in levels:
        if remaining <= 1e-12:
            break
        if limit is not None and ((is_buy and price > limit + 1e-12) or (not is_buy and price < limit - 1e-12)):
            break
        take = min(size, remaining)
        cost += take * price
        filled += take
        remaining -= take
        marginal = price
        used += 1
    return Walk(filled, cost, cost / filled if filled > 0 else 0.0, marginal, used)


def microprice(bid: float, bid_size: float, ask: float, ask_size: float) -> float:
    """Size-weighted mid (Stoikov's microprice, first-order): leans toward the thinner side's price."""
    tot = bid_size + ask_size
    if tot <= 0:
        return (bid + ask) / 2
    return (bid * ask_size + ask * bid_size) / tot


def imbalance(bid_size: float, ask_size: float) -> float:
    """Top-of-book imbalance in [-1, 1]; positive = more bid pressure."""
    tot = bid_size + ask_size
    return 0.0 if tot <= 0 else (bid_size - ask_size) / tot


def depth_within(levels: Sequence[Level], touch: float, within: float, is_bid: bool) -> float:
    """Shares resting within ``within`` probability points of ``touch``."""
    if is_bid:
        return sum(s for p, s in levels if touch - p <= within + 1e-12)
    return sum(s for p, s in levels if p - touch <= within + 1e-12)
