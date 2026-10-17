"""Fade overpriced longshots using a fitted calibration curve (docs/STRATEGIES.md §6).

Disabled until ``data/calibration.json`` exists (``pmtrader calibrate``).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import structlog

from pmtrader.core.types import Opportunity, OrderIntent, OrderType, Position, Side, round_size
from pmtrader.quant.calibration import CalibrationCurve
from pmtrader.quant.fees import fee_per_share
from pmtrader.quant.kelly import kelly_shares
from pmtrader.strategies.base import ScanContext, Strategy, exit_intent

log = structlog.get_logger(__name__)


class LongshotFade(Strategy):
    name = "longshot_fade"
    needs_judgment = True

    def __init__(self, cfg, calibration_path: Path | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(cfg)
        self.curve: CalibrationCurve | None = None
        if calibration_path and calibration_path.exists():
            self.curve = CalibrationCurve.load(calibration_path)
            log.info("longshot.calibration_loaded", path=str(calibration_path))

    def detect(self, ctx: ScanContext) -> list[Opportunity]:
        if self.curve is None:
            return []
        lo, hi = self.param("min_yes_price", 0.02), self.param("max_yes_price", 0.15)
        max_days = self.param("max_days_to_close", 30)
        out: list[Opportunity] = []
        for m in ctx.markets.values():
            if not m.is_binary or not m.accepting_orders:
                continue
            days = ctx.days_to_close(m)
            if days is None or days > max_days:
                continue
            by, bn = ctx.book(m.yes.asset_id), ctx.book(m.no.asset_id)
            if by is None or bn is None or by.mid is None or not bn.asks():
                continue
            p = by.mid
            if not (lo <= p <= hi):
                continue
            q, se = self.curve.predict(p)
            ask_no = bn.asks()[0][0]
            cost = ask_no + fee_per_share(ask_no, m.fees)
            edge = (1 - q) - cost
            if edge < self.cfg.min_edge:
                continue
            shares = kelly_shares(ctx.bankroll, 1 - q, cost, se, self.cfg.kelly_fraction, self.cfg.max_usd_per_trade)
            size = min(shares, bn.asks()[0][1])
            if size < m.min_order_size:
                continue
            out.append(
                Opportunity(
                    strategy=self.name,
                    market_id=m.id,
                    question=m.question,
                    legs=((m.no.asset_id, Side.BUY, ask_no),),
                    fair=1 - q,
                    edge=edge,
                    size_hint=size,
                    capital_per_unit=cost,
                    days_to_close=days,
                    needs_judgment=True,
                    event_id=m.event_id,
                    category=m.category,
                    confidence=max(0.0, 1 - se * 5),
                    features={"yes_mid": p, "calibrated_yes": round(q, 4), "se": round(se, 4)},
                    note=f"YES {p:.3f} vs calibrated {q:.3f}",
                )
            )
        return out

    def plan_entry(self, opp: Opportunity, ctx: ScanContext, budget_usd: float) -> list[OrderIntent]:
        asset, side, price = opp.legs[0]
        m = ctx.market_for_asset(asset)
        if m is None:
            return []
        size = round_size(min(opp.size_hint, budget_usd / opp.capital_per_unit))
        if size < m.min_order_size:
            return []
        return [OrderIntent(self.name, m.id, asset, side, price, size, OrderType.FAK, reason=opp.note)]

    def manage(self, positions: Iterable[Position], ctx: ScanContext) -> list[OrderIntent]:
        """Stop out if the longshot reprices to > stop_multiple × its level at entry."""
        out: list[OrderIntent] = []
        stop_yes = self.param("stop_yes_price", 0.35)
        for pos in positions:
            book = ctx.book(pos.asset_id)
            if book is None or book.best_bid is None:
                continue
            if 1 - book.best_bid >= stop_yes:
                out.append(exit_intent(pos, book.best_bid, f"stop: implied YES ≥ {stop_yes:.2f}", self.name))
        return out
