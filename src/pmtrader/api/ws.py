"""WebSocket hub: snapshot on connect, then coalesced delta frames (≤ max_fps) + immediate events.

Protocol (server → client):
  {"type": "snapshot", "seq": n, "data": {...full state...}}
  {"type": "delta", "seq": n, "data": {<changed sections>}, "events": [...]}
  {"type": "hb", "seq": n, "ts": t}
Client → server: {"type": "resync"}. Clients resync on a seq gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from pmtrader.core.events import (
    Alert,
    FeedHealth,
    FillEvent,
    GateDecision,
    LimitsChanged,
    OrderUpdated,
    PnLReport,
    PortfolioUpdated,
    RiskRejected,
    ScanCompleted,
    TradingStateChanged,
)

if TYPE_CHECKING:
    from pmtrader.app import Engine

log = structlog.get_logger(__name__)


def _default(o: Any) -> Any:
    if isinstance(o, Enum):
        return o.value
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    return str(o)


def dumps(obj: Any) -> str:
    """JSON for the browser: NaN/±inf become null (they are invalid JSON)."""
    return json.dumps(_sanitize(obj), default=_default, separators=(",", ":"))


def _sanitize(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if obj == obj and abs(obj) != float("inf") else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_sanitize(v) for v in obj]
    return obj


class WebSocketHub:
    def __init__(self, engine: Engine, max_fps: float = 10.0) -> None:
        self.engine = engine
        self.min_interval = 1.0 / max_fps
        self.clients: set[WebSocket] = set()
        self.seq = 0
        self._dirty: set[str] = set()
        self._events: list[dict[str, Any]] = []
        self.recent: deque[dict[str, Any]] = deque(maxlen=100)  # replayed to new clients
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ bus → dirty sections / events
    def _on_event(self, ev: Any) -> None:
        if isinstance(ev, PortfolioUpdated):
            self._dirty.add("portfolio")
            return
        if isinstance(ev, ScanCompleted):
            self._dirty.add("opportunities")
            return
        if isinstance(ev, FeedHealth):
            self._dirty.add("health")
            return
        if isinstance(ev, OrderUpdated):
            self._dirty.add("orders")
            o = ev.order
            if o.status.is_terminal or o.status.value == "OPEN":
                self._push(
                    {
                        "kind": "order",
                        "status": o.status.value,
                        "side": o.intent.side.value,
                        "price": o.intent.price,
                        "size": o.intent.size,
                        "filled": o.filled_size,
                        "strategy": o.intent.strategy,
                        "reason": o.reject_reason or o.intent.reason,
                    }
                )
            return
        if isinstance(ev, FillEvent):
            f = ev.fill
            m = self.engine.catalog.get(f.market_id)
            oc = m.outcome_for(f.asset_id) if m else None
            self._push(
                {
                    "kind": "fill",
                    "side": f.side.value,
                    "price": f.price,
                    "size": f.size,
                    "fee": f.fee,
                    "strategy": f.strategy,
                    "question": m.question if m else f.market_id,
                    "outcome": oc.name if oc else "",
                    "liquidity": f.liquidity.value,
                }
            )
            self._dirty.update({"orders", "portfolio"})
            return
        if isinstance(ev, TradingStateChanged | LimitsChanged):
            self._dirty.add("control")
            if isinstance(ev, TradingStateChanged):
                self._push({"kind": "state", "state": ev.state.value, "reason": ev.reason, "actor": ev.actor})
            return
        if isinstance(ev, Alert):
            self._push({"kind": "alert", "level": ev.level, "title": ev.title, "message": ev.message})
            return
        if isinstance(ev, RiskRejected):
            self._push({"kind": "reject", "code": ev.code, "message": ev.message, "strategy": ev.intent.strategy})
            return
        if isinstance(ev, GateDecision):
            self._dirty.add("opportunities")
            return
        if isinstance(ev, PnLReport):
            self._push({"kind": "report", "period": ev.period, "text": ev.text})

    def _push(self, e: dict[str, Any]) -> None:
        e["ts"] = time.time()
        self.recent.append(e)
        self._events.append(e)
        del self._events[:-200]
        self._wake.set()

    # ------------------------------------------------------------------ framing
    def _section(self, name: str) -> Any:
        e = self.engine
        getters: dict[str, Callable[[], Any]] = {
            "portfolio": e.portfolio.summary,
            "opportunities": e.opportunities_view,
            "orders": e.orders_view,
            "health": e.health,
            "control": e.control.snapshot,
        }
        return getters[name]()

    async def _broadcast(self, text: str) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=2.0)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def run(self) -> None:
        off = self.engine.bus.on(handler=self._on_event)
        last_send = 0.0
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.min_interval)
                self._wake.clear()
                if not self.clients:
                    self._dirty.clear()
                    self._events.clear()
                    continue
                now = time.monotonic()
                if self._events or (self._dirty and now - last_send >= self.min_interval):
                    data = {k: self._section(k) for k in sorted(self._dirty)}
                    self._dirty.clear()
                    events, self._events = self._events, []
                    self.seq += 1
                    await self._broadcast(dumps({"type": "delta", "seq": self.seq, "data": data, "events": events}))
                    last_send = now
                elif now - last_send >= 1.0:
                    self.seq += 1
                    await self._broadcast(dumps({"type": "hb", "seq": self.seq, "ts": time.time()}))
                    last_send = now
        finally:
            off()

    # ------------------------------------------------------------------ connections
    def _snapshot_frame(self) -> str:
        data = self.engine.snapshot()
        data["events"] = list(self.recent)
        return dumps({"type": "snapshot", "seq": self.seq, "data": data})

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text(self._snapshot_frame())
        self.clients.add(ws)
        try:
            while True:
                msg = await ws.receive_text()
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "resync":
                    await ws.send_text(self._snapshot_frame())
        except WebSocketDisconnect:
            pass
        finally:
            self.clients.discard(ws)
