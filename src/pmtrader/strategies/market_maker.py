"""Reward-aware Avellaneda–Stoikov market making (docs/STRATEGIES.md §5).

Disabled by default: it must be validated in paper mode (markouts!) before going live.
Selling YES without inventory is impossible on Polymarket, so the "ask" is expressed as a BUY of
NO at (1 − ask) unless we hold YES inventory to sell.
"""

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
from pmtrader.quant import rewards
from pmtrader.quant.avellaneda_stoikov import quote
from pmtrader.quant.fees import maker_rebate
from pmtrader.strategies.base import ScanContext, Strategy


class MarketMaker(Strategy):
    name = "market_maker"
    needs_judgment = True  # market *selection* benefits from judgment (news risk, toxicity)

    def _eligible(self, m: Market, ctx: ScanContext) -> bool:
        if not m.is_binary or not m.accepting_orders or m.rewards is None:
            return False
        ttc = m.seconds_to_close(ctx.now)
        if ttc is not None and ttc < self.param("min_hours_to_close", 24) * 3600:
            return False
        return not (m.game_start_time and m.game_start_time - ctx.now < self.param("pre_event_buffer_h", 2) * 3600)

    def detect(self, ctx: ScanContext) -> list[Opportunity]:
        out: list[Opportunity] = []
        for m in ctx.markets.values():
            if not self._eligible(m, ctx):
                continue
            book = ctx.book(m.yes.asset_id)
            if book is None or book.mid is None or book.spread is None:
                continue
            mid = book.mid
            if not (0.15 <= mid <= 0.85) or book.spread < 2 * m.tick_size:
                continue
            assert m.rewards is not None
            size = max(m.rewards.min_size, m.min_order_size)
            q = self._quote(m, mid, 0.0)
            ours = rewards.quote_q_min(q.bid, size, q.ask, size, mid, m.rewards.max_spread_cents, m.rewards.min_size)
            others = rewards.book_q_min(book.bids(), book.asks(), mid, m.rewards.max_spread_cents, m.rewards.min_size)
            pool = m.rewards.daily_rate_usd or self.param("assumed_daily_pool_usd", 20.0)
            exp_reward = rewards.expected_daily_reward(ours, others, pool)
            spread_capture = (q.ask - q.bid) / 2 if q.bid is not None and q.ask is not None else 0.0
            capital = size * mid + size * (1 - mid)
            edge = spread_capture + maker_rebate(1, mid, m.fees) + exp_reward / max(size, 1.0)
            if edge < self.cfg.min_edge:
                continue
            out.append(
                Opportunity(
                    strategy=self.name,
                    market_id=m.id,
                    question=m.question,
                    legs=((m.yes.asset_id, Side.BUY, q.bid or 0.0), (m.no.asset_id, Side.BUY, 1 - (q.ask or 1.0))),
                    fair=mid,
                    edge=edge,
                    size_hint=size,
                    capital_per_unit=capital / size,
                    days_to_close=ctx.days_to_close(m),
                    needs_judgment=True,
                    event_id=m.event_id,
                    category=m.category,
                    features={"exp_reward_day": round(exp_reward, 2), "spread": book.spread, "q_ours": round(ours, 1)},
                    note=f"MM: est. reward ${exp_reward:.2f}/day",
                )
            )
        return out

    def _quote(self, m: Market, mid: float, inventory: float):  # type: ignore[no-untyped-def]
        return quote(
            mid=mid,
            inventory=inventory,
            sigma_x=self.param("sigma_logit_per_sqrt_s", 0.002),
            gamma=self.param("gamma", 0.1),
            k=self.param("k", 150.0),
            tau_s=self.param("horizon_s", 3600.0),
            tick=m.tick_size,
            inventory_cap=self.param("inventory_cap_shares", 500.0),
        )

    def _inventory(self, m: Market, ctx: ScanContext) -> float:
        y = ctx.positions.get(m.yes.asset_id)
        n = ctx.positions.get(m.no.asset_id)
        return (y.qty if y else 0.0) - (n.qty if n else 0.0)

    def target_orders(self, m: Market, ctx: ScanContext) -> list[OrderIntent]:
        book = ctx.book(m.yes.asset_id)
        if book is None or book.mid is None or m.rewards is None:
            return []
        inv = self._inventory(m, ctx)
        q = self._quote(m, book.mid, inv)
        size = round_size(max(m.rewards.min_size, m.min_order_size))
        out: list[OrderIntent] = []
        if q.bid is not None:
            out.append(
                OrderIntent(
                    self.name,
                    m.id,
                    m.yes.asset_id,
                    Side.BUY,
                    q.bid,
                    size,
                    OrderType.GTC,
                    post_only=True,
                    reason="mm bid",
                )
            )
        if q.ask is not None:
            yes_pos = ctx.positions.get(m.yes.asset_id)
            if yes_pos and yes_pos.qty >= size:
                out.append(
                    OrderIntent(
                        self.name,
                        m.id,
                        m.yes.asset_id,
                        Side.SELL,
                        q.ask,
                        size,
                        OrderType.GTC,
                        post_only=True,
                        reduce_only=True,
                        reason="mm ask (sell inventory)",
                    )
                )
            else:
                px = round_price(1 - q.ask, m.tick_size, Side.BUY)
                out.append(
                    OrderIntent(
                        self.name,
                        m.id,
                        m.no.asset_id,
                        Side.BUY,
                        px,
                        size,
                        OrderType.GTC,
                        post_only=True,
                        reason="mm ask (buy NO)",
                    )
                )
        return out

    def plan_entry(self, opp: Opportunity, ctx: ScanContext, budget_usd: float) -> list[OrderIntent]:
        m = ctx.markets.get(opp.market_id)
        return self.target_orders(m, ctx) if m else []

    def manage(self, positions: Iterable[Position], ctx: ScanContext) -> list[OrderIntent]:
        """Requoting is driven by the StrategyEngine diffing target_orders() against open orders."""
        return []
