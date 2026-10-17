"""ControlPlane: trading state machine + every operator command (web, Discord, CLI share this path).

Each command is audited (actor, params, result) and state changes are persisted *before* side
effects so a crash mid-kill still leaves the system HALTED (docs/RISK.md → Kill switch).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import structlog

from pmtrader.config import Settings
from pmtrader.core.bus import EventBus
from pmtrader.core.clock import Clock
from pmtrader.core.events import Alert, LimitsChanged, TradingStateChanged
from pmtrader.core.orderbook import OrderBook
from pmtrader.core.types import (
    Market,
    Mode,
    OrderIntent,
    OrderType,
    Side,
    TradingState,
    round_price,
    round_size,
)
from pmtrader.execution.oms import OrderManager
from pmtrader.persistence.repository import Repository
from pmtrader.portfolio.ledger import Ledger
from pmtrader.risk.engine import Breach, RiskEngine

log = structlog.get_logger(__name__)

RESUME_CONFIRM = "RESUME"
_ALLOWED: dict[TradingState, set[TradingState]] = {
    TradingState.RUNNING: {TradingState.PAUSED, TradingState.HALTED},
    TradingState.PAUSED: {TradingState.RUNNING, TradingState.HALTED},
    TradingState.HALTED: {TradingState.PAUSED, TradingState.RUNNING},
}


class CommandError(Exception):
    pass


class ControlPlane:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        repo: Repository,
        settings: Settings,
        oms: OrderManager,
        risk: RiskEngine,
        ledger: Ledger,
        market: Callable[[str], Market | None],
        book: Callable[[str], OrderBook | None],
    ) -> None:
        self.bus, self.clock, self.repo, self.settings = bus, clock, repo, settings
        self.oms, self.risk, self.ledger = oms, risk, ledger
        self._market, self._book = market, book
        self.state = TradingState.PAUSED
        self.state_reason = "starting"
        self.state_since = clock.now()
        self.strategy_auto: dict[str, bool] = {n: c.auto_trade for n, c in settings.strategies.items()}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ boot
    async def load(self) -> None:
        saved = await self.repo.kv_get("trading_state")
        overrides = await self.repo.kv_get("risk_overrides", {}) or {}
        if overrides:
            try:
                self.risk.update_limits(overrides)
            except ValueError as exc:
                log.warning("control.bad_persisted_overrides", error=str(exc))
        auto = await self.repo.kv_get("strategy_auto", {}) or {}
        self.strategy_auto.update({k: bool(v) for k, v in auto.items() if k in self.strategy_auto})
        if saved and saved.get("state") == TradingState.HALTED.value:
            self.state, self.state_reason = TradingState.HALTED, saved.get("reason", "halted before restart")
        elif self.settings.mode is Mode.LIVE:
            self.state, self.state_reason = TradingState.PAUSED, "live mode starts paused — resume to trade"
        elif saved and saved.get("state") == TradingState.PAUSED.value:
            self.state, self.state_reason = TradingState.PAUSED, saved.get("reason", "paused before restart")
        else:
            self.state, self.state_reason = TradingState.RUNNING, "startup"
        self.state_since = self.clock.now()
        log.info("control.state_loaded", state=self.state.value, reason=self.state_reason)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.state_reason,
            "since": self.state_since,
            "mode": self.settings.mode.value,
            "limits": self.risk.limits.model_dump(),
            "strategy_auto": dict(self.strategy_auto),
        }

    # ------------------------------------------------------------------ state machine
    async def _transition(self, new: TradingState, actor: str, reason: str) -> None:
        old = self.state
        if new is old:
            return
        if new not in _ALLOWED[old]:
            raise CommandError(f"cannot go {old.value} → {new.value}")
        await self.repo.kv_set(
            "trading_state", {"state": new.value, "reason": reason, "actor": actor, "ts": self.clock.now()}
        )
        self.state, self.state_reason, self.state_since = new, reason, self.clock.now()
        log.warning("control.trading_state", state=new.value, previous=old.value, actor=actor, reason=reason)
        self.bus.publish(TradingStateChanged(new, old, reason, actor, self.state_since))

    async def _audit(self, actor: str, action: str, params: dict[str, Any], result: str) -> None:
        log.info("control.command", actor=actor, action=action, params=params, result=result)
        try:
            await self.repo.audit(self.clock.now(), actor, action, params, result)
        except Exception:
            log.exception("control.audit_failed")

    # ------------------------------------------------------------------ commands
    async def pause(self, actor: str, reason: str = "manual pause") -> str:
        async with self._lock:
            await self._transition(TradingState.PAUSED, actor, reason)
            # entry orders are cancelled; reduce-only exits stay working
            n = await self.oms.cancel_where(lambda o: not o.intent.reduce_only)
            msg = f"PAUSED ({n} entry orders cancelled)"
            await self._audit(actor, "pause", {"reason": reason}, msg)
            return msg

    async def resume(self, actor: str, confirm: str | None = None) -> str:
        async with self._lock:
            if self.state is TradingState.HALTED and (confirm or "").strip().upper() != RESUME_CONFIRM:
                raise CommandError(f"system is HALTED — resume requires confirm='{RESUME_CONFIRM}'")
            await self._transition(TradingState.RUNNING, actor, "manual resume")
            self.risk.consecutive_rejects = 0
            await self._audit(actor, "resume", {}, "RUNNING")
            return "RUNNING"

    async def kill(self, actor: str, reason: str = "kill switch", flatten: bool = False) -> str:
        async with self._lock:
            if self.state is not TradingState.HALTED:
                await self._transition(TradingState.HALTED, actor, reason)
            try:
                n = await self.oms.cancel_all()
            except Exception as exc:
                self.bus.publish(Alert("critical", "KILL: cancel_all FAILED", repr(exc)))
                n = -1
            msg = f"HALTED; cancelled {n} orders"
        if flatten:
            msg += "; " + await self.flatten(actor)
        self.bus.publish(Alert("critical", "Kill switch activated", f"{reason} (by {actor})", {"result": msg}))
        await self._audit(actor, "kill", {"reason": reason, "flatten": flatten}, msg)
        return msg

    async def flatten(self, actor: str) -> str:
        """Reduce-only marketable sells for every open position, bounded by flatten_max_slippage."""
        slip = self.risk.limits.flatten_max_slippage
        sent, skipped = 0, []
        for pos in list(self.ledger.positions.values()):
            m = self._market(pos.market_id)
            book = self._book(pos.asset_id)
            if m is None or book is None or book.best_bid is None:
                skipped.append(pos.outcome or pos.asset_id[:8])
                continue
            px = round_price(max(book.best_bid - slip, m.tick_size), m.tick_size, Side.SELL)
            intent = OrderIntent(
                "flatten",
                m.id,
                pos.asset_id,
                Side.SELL,
                px,
                round_size(pos.qty),
                OrderType.FAK,
                reduce_only=True,
                reason="flatten",
                source="flatten",
            )
            order, d = await self.oms.submit(intent)
            sent += int(order is not None)
            if order is None:
                skipped.append(f"{pos.outcome}: {d.code}")
        msg = f"flatten: {sent} orders sent" + (f", skipped {skipped}" if skipped else "")
        await self._audit(actor, "flatten", {}, msg)
        return msg

    async def set_limits(self, actor: str, changes: dict[str, Any]) -> dict[str, Any]:
        try:
            applied = self.risk.update_limits(changes)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        overrides = dict(await self.repo.kv_get("risk_overrides", {}) or {})
        overrides.update(applied)
        await self.repo.kv_set("risk_overrides", overrides)
        self.bus.publish(LimitsChanged(applied, actor))
        await self._audit(actor, "set_limits", applied, "ok")
        return applied

    async def set_strategy_auto(self, actor: str, name: str, enabled: bool) -> None:
        if name not in self.strategy_auto:
            raise CommandError(f"unknown strategy {name}")
        self.strategy_auto[name] = enabled
        await self.repo.kv_set("strategy_auto", self.strategy_auto)
        await self._audit(actor, "strategy_auto", {"name": name, "enabled": enabled}, "ok")

    async def place_manual(self, actor: str, intent: OrderIntent) -> dict[str, Any]:
        intent.source = "manual"
        intent.strategy = intent.strategy or "manual"
        order, d = await self.oms.submit(intent)
        res = {
            "ok": d.ok,
            "code": d.code,
            "message": d.message,
            "order_id": order.id if order else None,
            "status": order.status.value if order else None,
            "filled": order.filled_size if order else 0.0,
        }
        await self._audit(
            actor,
            "manual_order",
            {"asset": intent.asset_id, "side": intent.side.value, "price": intent.price, "size": intent.size},
            str(res),
        )
        return res

    async def cancel_order(self, actor: str, order_id: str) -> bool:
        ok = await self.oms.cancel(order_id)
        await self._audit(actor, "cancel_order", {"order_id": order_id}, str(ok))
        return ok

    async def cancel_all(self, actor: str) -> int:
        n = await self.oms.cancel_all()
        await self._audit(actor, "cancel_all", {}, str(n))
        return n

    # ------------------------------------------------------------------ automatic responses
    def on_breach(self, b: Breach) -> None:
        self.bus.publish(Alert("critical" if b.severity == "halt" else "warning", f"Risk breach: {b.code}", b.message))
        if b.severity == "halt":
            asyncio.get_running_loop().create_task(self.kill("risk-engine", b.message))
        elif self.state is TradingState.RUNNING:
            asyncio.get_running_loop().create_task(self.pause("risk-engine", b.message))

    def on_supervisor_escalation(self, task: str, exc: BaseException) -> None:
        self.bus.publish(Alert("critical", f"Task {task} keeps crashing", repr(exc)))
        if self.state is TradingState.RUNNING:
            asyncio.get_running_loop().create_task(self.pause("supervisor", f"{task} unstable: {exc!r}"))
