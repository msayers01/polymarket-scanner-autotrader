"""Model-based pricing of Binance-resolved crypto markets (docs/STRATEGIES.md §4)."""

from __future__ import annotations

from collections.abc import Iterable

from pmtrader.core.types import (
    Market,
    Opportunity,
    OrderIntent,
    OrderType,
    Position,
    Side,
    round_price,
    round_size,
)
from pmtrader.marketdata.question_parser import CryptoContract, parse_crypto_question
from pmtrader.quant import digital
from pmtrader.quant.fees import fee_per_share
from pmtrader.quant.kelly import kelly_shares
from pmtrader.quant.microstructure import walk_book
from pmtrader.strategies.base import ScanContext, Strategy, exit_intent


class CryptoFairValue(Strategy):
    name = "crypto_fair_value"
    needs_judgment = False  # model-driven; set params.require_llm=1 to route through the gate
    allows_near_expiry = True

    def __init__(self, cfg) -> None:  # type: ignore[no-untyped-def]
        super().__init__(cfg)
        self._parsed: dict[str, CryptoContract | None] = {}
        if cfg.params.get("require_llm"):
            self.needs_judgment = True  # type: ignore[misc]

    def contract(self, m: Market) -> CryptoContract | None:
        if m.id not in self._parsed:
            c = parse_crypto_question(m.question)
            src = m.resolution_source.lower()
            # only price markets whose resolution source we actually model
            if c is not None and src and "binance" not in src:
                c = None
            self._parsed[m.id] = c
        return self._parsed[m.id]

    def fair_band(self, m: Market, ctx: ScanContext) -> digital.FairBand | None:
        """Model band shrunk toward the market mid: q = w·q_model + (1−w)·p_market.

        The market is an informative prior (it sees news, flows and vol regimes the model does not);
        blending means we only trade where the model disagrees strongly *and* robustly.
        """
        raw = self.model_band(m, ctx)
        if raw is None:
            return None
        w = self.param("model_weight", 0.6)
        book = ctx.book(m.yes.asset_id)
        p = book.mid if book is not None else None
        if p is None or w >= 1.0:
            return raw if p is not None or w >= 1.0 else None
        return digital.FairBand(w * raw.lo + (1 - w) * p, w * raw.mid + (1 - w) * p, w * raw.hi + (1 - w) * p)

    def model_band(self, m: Market, ctx: ScanContext) -> digital.FairBand | None:
        c = self.contract(m)
        if c is None or ctx.crypto is None or m.end_time is None:
            return None
        tau_s = m.end_time - ctx.now
        if tau_s < self.param("min_seconds_to_expiry", 90):
            return None
        max_age = 5.0 if tau_s < 3600 else 15.0
        spot = ctx.crypto.spot(c.asset, max_age=max_age)
        # seasonal σ for exactly the remaining window (quiet weekend evenings ≠ US-open hours)
        sigma = ctx.crypto.vol_for_window(c.asset, ctx.now, m.end_time)
        if spot is None or not sigma:
            return None
        tau = tau_s / digital.SECONDS_PER_YEAR
        m_lo, m_hi = self.param("vol_mult_lo", 0.8), self.param("vol_mult_hi", 1.4)

        if c.kind == "above":
            k = c.strike or 0.0
            return digital.band(lambda s: digital.prob_above(spot, k, s, tau), sigma, m_lo, m_hi)
        if c.kind == "below":
            k = c.strike or 0.0
            return digital.band(lambda s: 1.0 - digital.prob_above(spot, k, s, tau), sigma, m_lo, m_hi)
        if c.kind == "between":
            lo, hi = c.strike, c.strike_hi
            return digital.band(lambda s: digital.prob_between(spot, lo, hi, s, tau), sigma, m_lo, m_hi)
        if c.kind == "touch_up":
            h = c.strike or 0.0
            return digital.band(lambda s: digital.prob_touch_up(spot, h, s, tau), sigma, m_lo, m_hi)
        if c.kind == "touch_down":
            low = c.strike or 0.0
            return digital.band(lambda s: digital.prob_touch_down(spot, low, s, tau), sigma, m_lo, m_hi)
        if c.kind == "updown" and c.window_seconds:
            start = m.end_time - c.window_seconds
            if ctx.now < start:
                return None
            open_px = ctx.crypto.open_at(c.asset, start)
            if open_px is None:
                return None
            # "Up" wins on close ≥ open; P(S_T = open) is ~0 so ≥ vs > is immaterial
            return digital.band(lambda s: digital.prob_above(spot, open_px, s, tau), sigma, m_lo, m_hi)
        return None

    # ----------------------------------------------------------------- detection
    def detect(self, ctx: ScanContext) -> list[Opportunity]:
        out: list[Opportunity] = []
        margin = self.cfg.min_edge
        for m in ctx.markets.values():
            if not m.is_binary or not m.accepting_orders:
                continue
            fb = self.fair_band(m, ctx)
            if fb is None:
                continue
            for outcome, q_lo, q_mid, q_hi in (
                (m.yes, fb.lo, fb.mid, fb.hi),
                (m.no, 1 - fb.hi, 1 - fb.mid, 1 - fb.lo),
            ):
                book = ctx.book(outcome.asset_id)
                if book is None or not book.asks():
                    continue
                ask = book.asks()[0][0]
                if ask > 0.97 or ask < 0.02:
                    continue  # extreme prices: resolution/settlement risk dominates the model
                cost = ask + fee_per_share(ask, m.fees)
                edge = q_lo - cost  # conservative: edge vs the low end of the fair band
                if edge < margin:
                    continue
                shares = kelly_shares(
                    ctx.bankroll,
                    q_mid,
                    cost,
                    stderr=fb.stderr,
                    lam=self.cfg.kelly_fraction,
                    max_usd=self.cfg.max_usd_per_trade,
                )
                # never walk beyond the price where the edge disappears
                limit = round_price(q_lo - margin - fee_per_share(ask, m.fees), m.tick_size, Side.BUY)
                depth = walk_book(book.asks(), shares, limit=limit).filled
                size = min(shares, depth)
                if size < m.min_order_size:
                    continue
                out.append(
                    Opportunity(
                        strategy=self.name,
                        market_id=m.id,
                        question=m.question,
                        legs=((outcome.asset_id, Side.BUY, limit),),
                        fair=q_mid,
                        edge=edge,
                        size_hint=size,
                        capital_per_unit=cost,
                        days_to_close=ctx.days_to_close(m),
                        needs_judgment=self.needs_judgment,
                        confidence=max(0.0, min(1.0, 1 - fb.stderr * 4)),
                        event_id=m.event_id,
                        category=m.category,
                        features={"q_lo": round(q_lo, 4), "q_hi": round(q_hi, 4), "ask": ask, "outcome": outcome.name},
                        note=f"model {q_lo:.3f}–{q_hi:.3f} vs ask {ask:.3f}",
                    )
                )
        return out

    def plan_entry(self, opp: Opportunity, ctx: ScanContext, budget_usd: float) -> list[OrderIntent]:
        asset, side, limit = opp.legs[0]
        m = ctx.market_for_asset(asset)
        if m is None:
            return []
        size = round_size(min(opp.size_hint, budget_usd / opp.capital_per_unit))
        if size < m.min_order_size:
            return []
        return [
            OrderIntent(
                strategy=self.name,
                market_id=m.id,
                asset_id=asset,
                side=side,
                price=limit,
                size=size,
                order_type=OrderType.FAK,
                reason=opp.note,
            )
        ]

    # ----------------------------------------------------------------- exits
    def manage(self, positions: Iterable[Position], ctx: ScanContext) -> list[OrderIntent]:
        """Sell when the bid (net of fee) reaches the model's fair value plus ``exit_margin``.

        Holding a fairly priced digital has zero EV while exiting costs the taker fee, so we only
        sell when proceeds ≥ fair + margin (edge fully harvested). Otherwise hold to resolution.
        """
        out: list[OrderIntent] = []
        exit_margin = self.param("exit_margin", 0.0)
        for pos in positions:
            m = ctx.markets.get(pos.market_id)
            book = ctx.book(pos.asset_id)
            if m is None or book is None or book.best_bid is None:
                continue
            fb = self.fair_band(m, ctx)
            if fb is None:
                continue
            q_mid = fb.mid if pos.asset_id == m.yes.asset_id else 1 - fb.mid
            bid = book.best_bid
            proceeds = bid - fee_per_share(bid, m.fees)
            if proceeds >= q_mid + exit_margin:
                out.append(exit_intent(pos, bid, f"edge harvested: bid {bid:.3f} ≥ fair {q_mid:.3f}", self.name))
        return out
