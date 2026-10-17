"""Pure position & P&L accounting (average-cost method).

* BUY: qty += s; avg_cost = (qty·avg + s·p)/(qty + s); fee is expensed immediately.
* SELL: realized += s·(p − avg_cost); qty −= s. (Polymarket has no shorting: you buy the other outcome.)
* Settlement: shares pay ``payout`` (1, 0, or 0.5 on 50-50) → realized += qty·(payout − avg_cost).
* Cash moves with every fill/settlement; equity = cash + Σ qty·mark.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pmtrader.core.types import Fill, Position, Side, round_usd


@dataclass
class PnLBreakdown:
    realized: float = 0.0
    unrealized: float = 0.0
    fees: float = 0.0

    @property
    def total(self) -> float:
        return self.realized + self.unrealized


@dataclass
class Ledger:
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)  # by asset id
    realized_total: float = 0.0
    fees_total: float = 0.0
    realized_by_strategy: dict[str, float] = field(default_factory=dict)
    closed_positions: list[Position] = field(default_factory=list)

    def apply_fill(self, f: Fill, outcome: str = "", event_id: str = "") -> Position:
        pos = self.positions.get(f.asset_id)
        if pos is None:
            pos = Position(f.asset_id, f.market_id, outcome, event_id or f.market_id, f.strategy, opened_ts=f.ts)
            self.positions[f.asset_id] = pos
        pos.fees_paid += f.fee
        pos.realized_pnl -= f.fee
        self.fees_total += f.fee
        self._realize(f.strategy, -f.fee)
        if f.side is Side.BUY:
            new_qty = pos.qty + f.size
            pos.avg_cost = (pos.qty * pos.avg_cost + f.size * f.price) / new_qty if new_qty > 0 else 0.0
            pos.qty = new_qty
            self.cash -= f.size * f.price + f.fee
        else:
            size = min(f.size, pos.qty)
            pnl = size * (f.price - pos.avg_cost)
            pos.realized_pnl += pnl
            self._realize(pos.strategy, pnl)
            pos.qty -= size
            self.cash += f.size * f.price - f.fee
        pos.updated_ts = f.ts
        self.cash = round_usd(self.cash)
        if not pos.is_open:
            self._close(pos)
        return pos

    def settle(self, asset_id: str, payout: float, ts: float) -> float:
        """Resolve an outcome token; returns realized P&L from settlement."""
        pos = self.positions.get(asset_id)
        if pos is None or not pos.is_open:
            return 0.0
        pnl = pos.qty * (payout - pos.avg_cost)
        self.cash = round_usd(self.cash + pos.qty * payout)
        pos.realized_pnl += pnl
        self._realize(pos.strategy, pnl)
        pos.qty = 0.0
        pos.updated_ts = ts
        self._close(pos)
        return pnl

    def _realize(self, strategy: str, amount: float) -> None:
        self.realized_total += amount
        self.realized_by_strategy[strategy] = self.realized_by_strategy.get(strategy, 0.0) + amount

    def _close(self, pos: Position) -> None:
        self.positions.pop(pos.asset_id, None)
        self.closed_positions.append(pos)
        del self.closed_positions[:-500]

    # ------------------------------------------------------------- valuation
    def unrealized(self, marks: Mapping[str, float]) -> float:
        return sum(p.qty * (marks.get(a, p.avg_cost) - p.avg_cost) for a, p in self.positions.items())

    def market_value(self, marks: Mapping[str, float]) -> float:
        return sum(p.qty * marks.get(a, p.avg_cost) for a, p in self.positions.items())

    def equity(self, marks: Mapping[str, float]) -> float:
        return self.cash + self.market_value(marks)

    def gross_exposure(self) -> float:
        return sum(p.cost_basis for p in self.positions.values())

    def exposure_by_market(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self.positions.values():
            out[p.market_id] = out.get(p.market_id, 0.0) + p.cost_basis
        return out

    def exposure_by_event(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self.positions.values():
            out[p.event_id] = out.get(p.event_id, 0.0) + p.cost_basis
        return out
