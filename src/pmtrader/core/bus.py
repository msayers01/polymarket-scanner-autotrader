"""In-process typed pub/sub with per-subscriber back-pressure.

* ``publish`` never blocks or awaits: producers (WS feed) must not be slowed by consumers.
* Each subscription owns a bounded queue. When full, non-critical events are dropped
  (oldest first) and counted; critical events (orders, fills, state) evict a non-critical
  event or, if the queue holds only critical events, grow beyond the bound and log loudly.
* Synchronous handlers (``on``) run inline inside ``publish`` — keep them O(1).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

import structlog

from pmtrader.core.events import Event

log = structlog.get_logger(__name__)


class Subscription:
    def __init__(self, bus: EventBus, types: tuple[type[Event], ...], maxsize: int, name: str) -> None:
        self._bus = bus
        self.types = types
        self.maxsize = maxsize
        self.name = name
        self._q: deque[Event] = deque()
        self._waiter: asyncio.Future[None] | None = None
        self.dropped = 0
        self.closed = False

    def matches(self, ev: Event) -> bool:
        return not self.types or isinstance(ev, self.types)

    def _push(self, ev: Event) -> None:
        if len(self._q) >= self.maxsize:
            if not self._evict_one():
                if not ev.critical:
                    self.dropped += 1
                    return
                log.warning("bus.queue_overflow_critical", sub=self.name, size=len(self._q))
        self._q.append(ev)
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    def _evict_one(self) -> bool:
        for i, old in enumerate(self._q):
            if not old.critical:
                del self._q[i]
                self.dropped += 1
                return True
        return False

    async def get(self) -> Event:
        while not self._q:
            if self.closed:
                raise StopAsyncIteration
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None
        return self._q.popleft()

    def get_nowait(self) -> Event | None:
        return self._q.popleft() if self._q else None

    def drain(self) -> list[Event]:
        items = list(self._q)
        self._q.clear()
        return items

    def __len__(self) -> int:
        return len(self._q)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self

    async def __anext__(self) -> Event:
        return await self.get()

    def close(self) -> None:
        self.closed = True
        self._bus._subs.discard(self)
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self._handlers: list[tuple[tuple[type[Event], ...], Callable[[Any], None]]] = []
        self.published = 0

    def subscribe(self, *types: type[Event], maxsize: int = 10_000, name: str = "") -> Subscription:
        sub = Subscription(self, tuple(types), maxsize, name or ",".join(t.__name__ for t in types))
        self._subs.add(sub)
        return sub

    def on(self, *types: type[Event], handler: Callable[[Any], None]) -> Callable[[], None]:
        entry = (tuple(types), handler)
        self._handlers.append(entry)
        return lambda: self._handlers.remove(entry)

    def publish(self, ev: Event) -> None:
        self.published += 1
        for types, handler in self._handlers:
            if not types or isinstance(ev, types):
                try:
                    handler(ev)
                except Exception:  # a handler must never break the publisher
                    log.exception("bus.handler_error", event=type(ev).__name__)
        for sub in self._subs:
            if sub.matches(ev):
                sub._push(ev)

    def stats(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "subscriptions": {s.name: {"queued": len(s), "dropped": s.dropped} for s in self._subs},
        }
