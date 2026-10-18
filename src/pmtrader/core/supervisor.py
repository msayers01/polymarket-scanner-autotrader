"""Supervised long-running tasks with restart + escalation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

TaskFactory = Callable[[], Awaitable[None]]


@dataclass
class _Spec:
    name: str
    factory: TaskFactory
    critical: bool
    failures: list[float] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


class Supervisor:
    """Runs coroutines, restarting them with exponential backoff when they crash.

    If a *critical* task fails ``max_failures`` times within ``window_s``, ``on_escalate`` is
    called (the engine wires this to a HALT) and the task keeps retrying at max backoff.
    """

    def __init__(
        self,
        on_escalate: Callable[[str, BaseException], None] | None = None,
        max_failures: int = 5,
        window_s: float = 300.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._specs: dict[str, _Spec] = {}
        self._on_escalate = on_escalate
        self._max_failures = max_failures
        self._window_s = window_s
        self._max_backoff_s = max_backoff_s
        self._stopping = False

    def start(self, name: str, factory: TaskFactory, critical: bool = False) -> None:
        spec = _Spec(name, factory, critical)
        self._specs[name] = spec
        spec.task = asyncio.create_task(self._run(spec), name=name)

    async def _run(self, spec: _Spec) -> None:
        backoff = 0.5
        while not self._stopping:
            try:
                await spec.factory()
                if self._stopping:
                    return
                log.warning("supervisor.task_exited", task=spec.name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                now = time.monotonic()
                spec.failures = [t for t in spec.failures if now - t < self._window_s] + [now]
                log.exception("supervisor.task_crashed", task=spec.name, failures=len(spec.failures))
                if spec.critical and len(spec.failures) >= self._max_failures and self._on_escalate:
                    self._on_escalate(spec.name, exc)
                    spec.failures.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff_s)

    async def stop(self) -> None:
        self._stopping = True
        tasks = [s.task for s in self._specs.values() if s.task]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def status(self) -> dict[str, dict[str, object]]:
        return {
            n: {"running": bool(s.task and not s.task.done()), "recent_failures": len(s.failures)}
            for n, s in self._specs.items()
        }
