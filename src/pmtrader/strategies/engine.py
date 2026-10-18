"""StrategyEngine: turns scanner opportunities into orders and manages exits/quotes.

Flow: ScanCompleted → (LLM gate for needs_judgment) → Strategy.plan_entry → OrderManager (risk) →
gateway. A separate loop calls Strategy.manage for exits and re-quotes market-making orders.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import structlog

from pmtrader.core.bus import EventBus
from pmtrader.core.clock import Clock
from pmtrader.core.events import ScanCompleted
from pmtrader.core.types import Opportunity, OrderIntent, Side, TradingState
from pmtrader.execution.oms import OrderManager
from pmtrader.llm.gate import LLMGate
from pmtrader.portfolio.ledger import Ledger
from pmtrader.risk.engine import RiskEngine
from pmtrader.scanner.scanner import Scanner
from pmtrader.strategies.base import ScanContext, Strategy
from pmtrader.strategies.market_maker import MarketMaker

log = structlog.get_logger(__name__)


class StrategyEngine:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        strategies: Mapping[str, Strategy],
        scanner: Scanner,
        oms: OrderManager,
        risk: RiskEngine,
        ledger: Ledger,
        gate: LLMGate,
        trading_state: object,  # ControlPlane (duck-typed: .state, .strategy_auto)
        llm_fallback: str = "deterministic",
    ) -> None:
        self.bus, self.clock, self.strategies, self.scanner = bus, clock, strategies, scanner
        self.oms, self.risk, self.ledger, self.gate = oms, risk, ledger, gate
        self.control = trading_state
        self.llm_fallback = llm_fallback
        self._cooldown: dict[str, float] = {}
        self._gate_queue: dict[str, Opportunity] = {}
        self.mm_markets: set[str] = set()
        self.entries_attempted = 0

    # ------------------------------------------------------------------ helpers
    def _running(self) -> bool:
        return self.control.state is TradingState.RUNNING  # type: ignore[attr-defined]

    def _auto(self, strategy: str) -> bool:
        return bool(self.control.strategy_auto.get(strategy, False))  # type: ignore[attr-defined]

    def _cool(self, opp: Opportunity) -> bool:
        return self._cooldown.get(f"{opp.strategy}|{opp.market_id}", 0.0) > self.clock.now()

    def _set_cooldown(self, opp: Opportunity, strat: Strategy) -> None:
        self._cooldown[f"{opp.strategy}|{opp.market_id}"] = self.clock.now() + strat.param("cooldown_s", 30.0)

    def _budget(self, strat: Strategy, opp: Opportunity) -> float:
        per_leg_cap = self.risk.limits.max_order_usd
        legs = max(len(opp.legs), 1)
        return min(strat.cfg.max_usd_per_trade, per_leg_cap * legs)

    # ------------------------------------------------------------------ entries
    async def on_scan(self, ev: ScanCompleted) -> None:
        if not self._running():
            return
        for opp in ev.opportunities:
            strat = self.strategies.get(opp.strategy)
            if strat is None or not self._auto(opp.strategy) or self._cool(opp):
                continue
            if opp.needs_judgment:
                mode = self.gate.mode
                if mode == "off":
                    if self.llm_fallback != "deterministic":
                        continue
                else:
                    verdict = self.gate.cached(opp)
                    if verdict is None:
                        self._gate_queue[opp.key] = opp
                        if mode == "required":
                            continue
                    elif mode == "required" and not verdict.enter:
                        continue
            await self.enter(opp, strat)

    async def enter(self, opp: Opportunity, strat: Strategy) -> None:
        ctx = self.scanner.context()
        intents = strat.plan_entry(opp, ctx, self._budget(strat, opp))
        self._set_cooldown(opp, strat)
        if not intents:
            return
        self.entries_attempted += 1
        if isinstance(strat, MarketMaker):
            self.mm_markets.add(opp.market_id)
        if len(intents) > 1 and all(i.group_id for i in intents) and intents[0].group_id == intents[-1].group_id:
            await self.oms.submit_group(intents)
        else:
            for it in intents:
                await self.oms.submit(it)

    # ------------------------------------------------------------------ exits / quoting
    async def manage_once(self) -> None:
        ctx = self.scanner.context()
        open_sells = {o.intent.asset_id for o in self.oms.open_orders() if o.intent.side is Side.SELL}
        by_strategy: dict[str, list] = {}
        for p in self.ledger.positions.values():
            by_strategy.setdefault(p.strategy, []).append(p)
        state = self.control.state  # type: ignore[attr-defined]
        for name, strat in self.strategies.items():
            positions = by_strategy.get(name, [])
            if positions and state is not TradingState.HALTED:
                try:
                    exits = strat.manage(positions, ctx)
                except Exception:
                    log.exception("engine.manage_failed", strategy=name)
                    exits = []
                for it in exits:
                    if it.asset_id in open_sells:
                        continue
                    open_sells.add(it.asset_id)
                    await self.oms.submit(it)
            if isinstance(strat, MarketMaker):
                await self._requote(strat, ctx)

    async def _requote(self, mm: MarketMaker, ctx: ScanContext) -> None:
        mine = [o for o in self.oms.open_orders() if o.intent.strategy == mm.name]
        if not self._running() or not self._auto(mm.name):
            if mine:
                await self.oms.cancel_where(lambda o: o.intent.strategy == mm.name and not o.intent.reduce_only)
            return
        still_ok = {o.market_id for o in self.scanner.latest_by_key.values() if o.strategy == mm.name}
        for mid in list(self.mm_markets):
            m = ctx.markets.get(mid)
            orders = [o for o in mine if o.intent.market_id == mid]
            if m is None or mid not in still_ok:
                for o in orders:
                    await self.oms.cancel(o.id)
                self.mm_markets.discard(mid)
                continue
            targets = mm.target_orders(m, ctx)
            tick = m.tick_size

            def same(o_int: OrderIntent, t: OrderIntent, tick: float = tick) -> bool:
                return o_int.asset_id == t.asset_id and o_int.side is t.side and abs(o_int.price - t.price) < tick / 2

            for o in orders:
                if not any(same(o.intent, t) for t in targets):
                    await self.oms.cancel(o.id)
            for t in targets:
                if not any(same(o.intent, t) and o.is_open for o in orders):
                    await self.oms.submit(t)

    # ------------------------------------------------------------------ loops
    async def _gate_worker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if not self._gate_queue or self.gate.mode == "off":
                continue
            batch = list(self._gate_queue.values())
            self._gate_queue.clear()
            try:
                await self.gate.evaluate(batch, notional=lambda o: o.size_hint * o.capital_per_unit)
            except Exception:
                log.exception("engine.gate_failed")

    async def _manage_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.manage_once()
            except Exception:
                log.exception("engine.manage_loop_failed")

    async def run(self) -> None:
        sub = self.bus.subscribe(ScanCompleted, maxsize=2, name="strategy-engine")
        tasks = [asyncio.create_task(self._gate_worker()), asyncio.create_task(self._manage_loop())]
        try:
            async for ev in sub:
                try:
                    await self.on_scan(ev)  # type: ignore[arg-type]
                except Exception:
                    log.exception("engine.on_scan_failed")
        finally:
            for t in tasks:
                t.cancel()
            sub.close()
