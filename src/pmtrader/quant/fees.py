"""Polymarket fee model: fee = C · rate · (p(1-p))^exponent, charged to takers.

This module is the single source of truth for fees (docs/STRATEGIES.md §0.1).
"""

from __future__ import annotations

from pmtrader.core.types import FeeSchedule


def fee_per_share(price: float, fees: FeeSchedule, taker: bool = True) -> float:
    """Fee per share in USDC (unrounded) for a fill at ``price``."""
    if not fees.enabled or (fees.taker_only and not taker):
        return 0.0
    p = min(max(price, 0.0), 1.0)
    return fees.rate * (p * (1.0 - p)) ** fees.exponent


def fee(size: float, price: float, fees: FeeSchedule, taker: bool = True) -> float:
    """Venue fee for a fill, rounded to 5 dp; amounts below the 0.00001 minimum round to zero."""
    raw = size * fee_per_share(price, fees, taker)
    return round(raw, 5)


def maker_rebate(size: float, price: float, fees: FeeSchedule) -> float:
    """Expected rebate paid to the resting maker (a fraction of the taker's fee)."""
    return size * fee_per_share(price, fees, taker=True) * fees.rebate_rate


def buy_cost_per_share(price: float, fees: FeeSchedule, taker: bool = True) -> float:
    """All-in USDC cost to acquire one share."""
    return price + fee_per_share(price, fees, taker)


def sell_proceeds_per_share(price: float, fees: FeeSchedule, taker: bool = True) -> float:
    """Net USDC received for selling one share."""
    return price - fee_per_share(price, fees, taker)


def breakeven_edge(price: float, fees: FeeSchedule, round_trip: bool = False) -> float:
    """Probability points needed just to cover taker fees (entry, or entry + exit)."""
    one = fee_per_share(price, fees, taker=True)
    return 2 * one if round_trip else one
