"""Depth-aware structural arbitrage (docs/STRATEGIES.md §1–3).

A *basket* is a set of legs whose combined payoff is guaranteed ≥ ``payoff_per_set`` per set:
* binary complete set: [YES, NO] → 1
* neg-risk YES basket over N exhaustive outcomes → 1
* neg-risk NO basket → N − 1
* strike ladder pair YES(K₁) + NO(K₂), K₁ < K₂ → ≥ 1

We walk all legs' books simultaneously and keep buying sets while the *marginal* set cost
(prices + taker fees) stays below ``payoff_per_set − ε``. Because marginal prices only rise
as we walk, profit is concave in the number of sets and the greedy walk is optimal.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from pmtrader.core.types import FeeSchedule
from pmtrader.quant.fees import fee_per_share

Level = tuple[float, float]


@dataclass(frozen=True, slots=True)
class Leg:
    levels: Sequence[Level]  # asks ascending (buy) or bids descending (sell)
    fees: FeeSchedule
    asset_id: str = ""


@dataclass(frozen=True, slots=True)
class BasketResult:
    sets: float
    cost: float  # total USDC including fees
    payoff: float  # guaranteed payoff of the sets acquired
    limit_prices: tuple[float, ...]  # worst price per leg (use as limit price)
    top_set_cost: float  # cost of one set at top of book incl. fees (inf if a leg is empty)
    marginal_set_cost: float

    @property
    def profit(self) -> float:
        return self.payoff - self.cost

    @property
    def edge_per_set(self) -> float:
        return self.profit / self.sets if self.sets > 0 else 0.0

    @property
    def top_edge(self) -> float:
        """Edge per set at top of book (can be negative) — useful for scanning/display."""
        return -math.inf if math.isinf(self.top_set_cost) else 1.0 - self.top_set_cost


def _top_cost(legs: Sequence[Leg]) -> float:
    total = 0.0
    for leg in legs:
        if not leg.levels:
            return math.inf
        p = leg.levels[0][0]
        total += p + fee_per_share(p, leg.fees, taker=True)
    return total


def basket_buy(
    legs: Sequence[Leg],
    payoff_per_set: float = 1.0,
    epsilon: float = 0.0,
    max_sets: float = math.inf,
) -> BasketResult:
    """Greedy multi-leg depth walk. Returns the profitable number of sets (0 if none)."""
    n = len(legs)
    top = _top_cost(legs)
    if n == 0 or math.isinf(top):
        return BasketResult(0.0, 0.0, 0.0, tuple(0.0 for _ in legs), top, math.inf)
    # top_set_cost is expressed per unit payoff for display: normalise multi-payoff baskets
    top_norm = top - (payoff_per_set - 1.0)

    idx = [0] * n
    rem = [leg.levels[0][1] for leg in legs]
    limits = [0.0] * n
    sets = cost = 0.0
    marginal = math.inf
    while sets < max_sets - 1e-12:
        prices = [legs[i].levels[idx[i]][0] for i in range(n)]
        marginal = sum(p + fee_per_share(p, legs[i].fees, True) for i, p in enumerate(prices))
        if marginal >= payoff_per_set - epsilon:
            break
        chunk = min(min(rem), max_sets - sets)
        if chunk <= 1e-12:
            break
        sets += chunk
        cost += chunk * marginal
        for i in range(n):
            limits[i] = prices[i]
            rem[i] -= chunk
            if rem[i] <= 1e-12:
                idx[i] += 1
                if idx[i] >= len(legs[i].levels):
                    return BasketResult(sets, cost, sets * payoff_per_set, tuple(limits), top_norm, marginal)
                rem[i] = legs[i].levels[idx[i]][1]
    return BasketResult(sets, cost, sets * payoff_per_set, tuple(limits), top_norm, marginal)


@dataclass(frozen=True, slots=True)
class SellResult:
    sets: float
    proceeds: float  # USDC received net of fees
    mint_cost: float  # sets · payoff_per_set (cost to split/mint the sets)
    limit_prices: tuple[float, ...]
    top_set_proceeds: float

    @property
    def profit(self) -> float:
        return self.proceeds - self.mint_cost

    @property
    def top_edge(self) -> float:
        return self.top_set_proceeds - 1.0


def basket_sell(
    legs: Sequence[Leg],
    payoff_per_set: float = 1.0,
    epsilon: float = 0.0,
    max_sets: float = math.inf,
) -> SellResult:
    """Mirror image of :func:`basket_buy`: mint sets at ``payoff_per_set`` each and sell every leg
    into the bids while Σ(bid − fee) > payoff + ε. Needs on-chain split, so v0.1 reports only."""
    n = len(legs)
    if n == 0 or any(not leg.levels for leg in legs):
        return SellResult(0.0, 0.0, 0.0, tuple(0.0 for _ in legs), -math.inf)
    top = sum(leg.levels[0][0] - fee_per_share(leg.levels[0][0], leg.fees, True) for leg in legs)
    idx = [0] * n
    rem = [leg.levels[0][1] for leg in legs]
    limits = [0.0] * n
    sets = proceeds = 0.0
    while sets < max_sets - 1e-12:
        prices = [legs[i].levels[idx[i]][0] for i in range(n)]
        marginal = sum(p - fee_per_share(p, legs[i].fees, True) for i, p in enumerate(prices))
        if marginal <= payoff_per_set + epsilon:
            break
        chunk = min(min(rem), max_sets - sets)
        sets += chunk
        proceeds += chunk * marginal
        exhausted = False
        for i in range(n):
            limits[i] = prices[i]
            rem[i] -= chunk
            if rem[i] <= 1e-12:
                idx[i] += 1
                if idx[i] >= len(legs[i].levels):
                    exhausted = True
                else:
                    rem[i] = legs[i].levels[idx[i]][1]
        if exhausted:
            break
    return SellResult(sets, proceeds, sets * payoff_per_set, tuple(limits), top - (payoff_per_set - 1.0))
