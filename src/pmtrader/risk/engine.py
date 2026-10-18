"""Pre-trade risk checks and circuit breakers (docs/RISK.md).

The engine is synchronous and side-effect free except for its rate-limit bucket and counters,
so it can be called inline by the OrderManager on every order.

"Markets per day" counts distinct Polymarket *events* (one market page in the Polymarket UI),
so a 10-leg neg-risk basket is one market, not ten.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from pmtrader.config import RiskCeilings, RiskLimits
from pmtrader.core.clock import Clock
from pmtrader.core.orderbook import OrderBook
from pmtrader.core.types import Market, Mode, Order, OrderIntent, OrderType, Side, TradingState, is_on_tick
from pmtrader.portfolio.ledger import Ledger
from pmtrader.quant.fees import fee_per_share


@dataclass(frozen=True, slots=True)
class RiskDecision:
    ok: bool
    code: str = ""
    message: str = ""

    @staticmethod
    def accept() -> RiskDecision:
        return RiskDecision(True)

    @staticmethod
    def reject(code: str, message: str) -> RiskDecision:
        return RiskDecision(False, code, message)


@dataclass
class _Pending:
    """Exposure added by earlier legs of the same group (so a group is checked cumulatively)."""

    by_market: dict[str, float] = field(default_factory=dict)
    by_event: dict[str, float] = field(default_factory=dict)
    gross: float = 0.0
    cash: float = 0.0
    orders: int = 0
    new_events: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class Breach:
    code: str
    message: str
    severity: str  # pause | halt


class RiskEngine:
    def __init__(
        self,
        limits: RiskLimits,
        ceilings: RiskCeilings,
        clock: Clock,
        mode: Mode,
        ledger: Ledger,
        market: Callable[[str], Market | None],
        book: Callable[[str], OrderBook | None],
        open_orders: Callable[[], Iterable[Order]],
        trading_state: Callable[[], TradingState],
        strategy_allows_near_expiry: Callable[[str], bool] = lambda _s: False,
    ) -> None:
        self.limits = limits
        self.ceilings = ceilings
        self.clock = clock
        self.mode = mode
        self.ledger = ledger
        self._market = market
        self._book = book
        self._open_orders = open_orders
        self._state = trading_state
        self._near_expiry_ok = strategy_allows_near_expiry
        self.events_today: set[str] = set()
        self._order_times: deque[float] = deque()
        self.consecutive_rejects = 0
        self.reject_counts: dict[str, int] = {}

    # ------------------------------------------------------------------ limits management
    def update_limits(self, changes: dict[str, object]) -> dict[str, object]:
        """Validate against hard ceilings and apply. Returns the applied changes."""
        current = self.limits.model_dump()
        applied: dict[str, object] = {}
        for k, v in changes.items():
            if k not in current:
                raise ValueError(f"unknown limit: {k}")
            ceiling = getattr(self.ceilings, k, None)
            if ceiling is not None and isinstance(v, int | float) and v > ceiling:
                raise ValueError(f"{k}={v} exceeds hard ceiling {ceiling}")
            if isinstance(v, int | float) and v < 0:
                raise ValueError(f"{k} must be ≥ 0")
            current[k] = v
            applied[k] = v
        self.limits = RiskLimits(**current)
        return applied

    def reset_day(self, events_today: Iterable[str] = ()) -> None:
        self.events_today = set(events_today)

    # ------------------------------------------------------------------ checks
    def check_group(self, intents: list[OrderIntent]) -> RiskDecision:
        """All-or-nothing check for multi-leg orders (arb legs)."""
        pending = _Pending()
        for it in intents:
            d = self.check(it, pending)
            if not d.ok:
                return d
        return RiskDecision.accept()

    def check(self, it: OrderIntent, pending: _Pending | None = None) -> RiskDecision:
        d = self._check(it, pending or _Pending())
        if not d.ok:
            self.reject_counts[d.code] = self.reject_counts.get(d.code, 0) + 1
        return d

    def _check(self, it: OrderIntent, p: _Pending) -> RiskDecision:
        L = self.limits
        now = self.clock.now()
        state = self._state()
        reducing = it.reduce_only and it.side is Side.SELL

        # 1. state / mode / strategy
        if self.mode is Mode.SCANNER:
            return RiskDecision.reject("mode", "scanner mode: trading disabled")
        if state is TradingState.HALTED and not (reducing and it.source in ("flatten", "manual")):
            return RiskDecision.reject("halted", "trading is HALTED")
        if state is TradingState.PAUSED and not reducing:
            return RiskDecision.reject("paused", "trading is PAUSED (exits only)")
        if it.source == "strategy" and not reducing and it.strategy not in L.allowed_strategies:
            return RiskDecision.reject("strategy_not_allowed", f"{it.strategy} not in allowed_strategies")

        # 2. market validity
        m = self._market(it.market_id)
        if m is None:
            return RiskDecision.reject("unknown_market", it.market_id)
        if not m.accepting_orders or m.closed:
            return RiskDecision.reject("market_closed", "market not accepting orders")
        if m.outcome_for(it.asset_id) is None:
            return RiskDecision.reject("asset_mismatch", "asset does not belong to market")
        if not is_on_tick(it.price, m.tick_size) or not (0 < it.price < 1):
            return RiskDecision.reject("bad_price", f"price {it.price} not on tick {m.tick_size}")
        if it.size < m.min_order_size and not reducing:
            return RiskDecision.reject("min_size", f"size {it.size} < min {m.min_order_size}")
        q = m.question.lower()
        if not reducing and any(k.lower() in q for k in L.blocked_keywords):
            return RiskDecision.reject("blocked_keyword", "market matches blocked keyword")
        ttc = m.seconds_to_close(now)
        if (
            not reducing
            and ttc is not None
            and ttc < L.min_time_to_close_minutes * 60
            and not self._near_expiry_ok(it.strategy)
        ):
            return RiskDecision.reject("near_close", f"{ttc / 60:.1f} min to close")

        # 3. book freshness / price band
        book = self._book(it.asset_id)
        if book is None or book.age(now) > L.stale_book_seconds:
            return RiskDecision.reject("stale_book", "no fresh order book")
        mid = book.mid
        marketable = (it.side is Side.BUY and book.best_ask is not None and book.best_ask <= it.price + 1e-12) or (
            it.side is Side.SELL and book.best_bid is not None and book.best_bid >= it.price - 1e-12
        )
        is_taker = it.order_type in (OrderType.FAK, OrderType.FOK) or (marketable and not it.post_only)
        # fat-finger guard applies to aggressive orders only; passive quotes far from mid are harmless
        if is_taker and mid is not None and abs(it.price - mid) > L.max_price_deviation and it.source != "flatten":
            return RiskDecision.reject("price_band", f"|{it.price:.3f} − mid {mid:.3f}| > {L.max_price_deviation}")

        # 4. rate / open orders
        while self._order_times and now - self._order_times[0] > 60:
            self._order_times.popleft()
        if len(self._order_times) + p.orders >= L.max_orders_per_minute:
            return RiskDecision.reject("rate_limit", f"> {L.max_orders_per_minute} orders/min")
        open_orders = [o for o in self._open_orders() if o.is_open]
        if len(open_orders) + p.orders >= L.max_open_orders:
            return RiskDecision.reject("max_open_orders", f"≥ {L.max_open_orders} open orders")

        if reducing:
            pos = self.ledger.positions.get(it.asset_id)
            held = pos.qty if pos else 0.0
            pending_sells = sum(
                o.remaining for o in open_orders if o.intent.asset_id == it.asset_id and o.intent.side is Side.SELL
            )
            if it.size > held - pending_sells + 1e-9:
                return RiskDecision.reject(
                    "insufficient_position", f"sell {it.size} > available {held - pending_sells}"
                )
            self._accept_pending(p, it, m, 0.0)
            return RiskDecision.accept()
        if it.side is Side.SELL:
            return RiskDecision.reject("sell_requires_reduce_only", "sells must be reduce-only (no shorting)")

        # 5. per-order notional
        notional = it.notional
        if notional > L.max_order_usd + 1e-9:
            return RiskDecision.reject("max_order_usd", f"${notional:.2f} > ${L.max_order_usd}")

        # 6. exposures (positions + resting buys + earlier legs)
        pend_mkt: dict[str, float] = {}
        pend_evt: dict[str, float] = {}
        pend_gross = 0.0
        for o in open_orders:
            if o.intent.side is Side.BUY:
                n = o.remaining * o.intent.price
                pend_mkt[o.intent.market_id] = pend_mkt.get(o.intent.market_id, 0.0) + n
                om = self._market(o.intent.market_id)
                ev = om.event_id if om else o.intent.market_id
                pend_evt[ev] = pend_evt.get(ev, 0.0) + n
                pend_gross += n
        mkt_exp = self.ledger.exposure_by_market().get(m.id, 0.0) + pend_mkt.get(m.id, 0.0) + p.by_market.get(m.id, 0.0)
        if mkt_exp + notional > L.max_position_usd_per_market + 1e-9:
            return RiskDecision.reject("max_position_market", f"market exposure ${mkt_exp + notional:.2f}")
        ev_id = m.event_id
        ev_exp = self.ledger.exposure_by_event().get(ev_id, 0.0) + pend_evt.get(ev_id, 0.0) + p.by_event.get(ev_id, 0.0)
        if ev_exp + notional > L.max_exposure_per_event_usd + 1e-9:
            return RiskDecision.reject("max_exposure_event", f"event exposure ${ev_exp + notional:.2f}")
        gross = self.ledger.gross_exposure() + pend_gross + p.gross
        if gross + notional > L.max_gross_exposure_usd + 1e-9:
            return RiskDecision.reject("max_gross_exposure", f"gross exposure ${gross + notional:.2f}")

        # 7. daily new-market (event) cap
        holds_event = any(pos.event_id == ev_id for pos in self.ledger.positions.values()) or ev_id in pend_evt
        if not holds_event and ev_id not in self.events_today and ev_id not in p.new_events:
            if len(self.events_today) + len(p.new_events) >= L.max_new_markets_per_day:
                return RiskDecision.reject(
                    "daily_market_cap", f"max {L.max_new_markets_per_day} new markets/day reached"
                )
            p.new_events.add(ev_id)

        # 8. cash
        fee = it.size * fee_per_share(it.price, m.fees, taker=is_taker)
        if notional + fee > self.ledger.cash - pend_gross - p.cash + 1e-9:
            return RiskDecision.reject("insufficient_cash", f"need ${notional + fee:.2f}, cash ${self.ledger.cash:.2f}")

        self._accept_pending(p, it, m, notional + fee)
        return RiskDecision.accept()

    def _accept_pending(self, p: _Pending, it: OrderIntent, m: Market, cash: float) -> None:
        p.orders += 1
        if it.side is Side.BUY:
            p.by_market[m.id] = p.by_market.get(m.id, 0.0) + it.notional
            p.by_event[m.event_id] = p.by_event.get(m.event_id, 0.0) + it.notional
            p.gross += it.notional
            p.cash += cash

    # ------------------------------------------------------------------ bookkeeping hooks
    def on_submitted(self, it: OrderIntent, event_id: str) -> None:
        self._order_times.append(self.clock.now())
        if it.side is Side.BUY and not it.reduce_only:
            self.events_today.add(event_id)

    def on_exchange_result(self, accepted: bool) -> None:
        """Exchange-side rejects (not our own risk rejects) feed the reject-storm breaker."""
        self.consecutive_rejects = 0 if accepted else self.consecutive_rejects + 1

    # ------------------------------------------------------------------ circuit breakers
    def breakers(self, equity: float, day_start_equity: float | None, high_water: float) -> list[Breach]:
        out: list[Breach] = []
        L = self.limits
        if day_start_equity is not None:
            day_pnl = equity - day_start_equity
            if day_pnl <= -L.max_daily_loss_usd:
                out.append(Breach("daily_loss", f"day P&L ${day_pnl:.2f} ≤ −${L.max_daily_loss_usd}", "halt"))
        if high_water > 0:
            dd = (high_water - equity) / high_water * 100
            if dd >= L.max_drawdown_pct:
                out.append(Breach("drawdown", f"drawdown {dd:.1f}% ≥ {L.max_drawdown_pct}%", "halt"))
        if self.consecutive_rejects >= L.max_consecutive_rejects:
            out.append(Breach("reject_storm", f"{self.consecutive_rejects} consecutive rejects", "pause"))
        return out
