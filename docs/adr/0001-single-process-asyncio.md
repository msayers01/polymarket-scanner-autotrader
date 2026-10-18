# ADR-0001: One asyncio process hosts engine, API, and Discord bot

**Status:** accepted (2026-10-17)

## Context
We need sub-second propagation from market data → engine → UI, plus a kill switch that works
from several surfaces (web, Discord, CLI). There is one operator and one account.

## Decision
Run everything in **one Python process on one asyncio event loop**: the trading engine,
FastAPI/uvicorn (REST + WebSocket) and the discord.py client. Components talk through an
in-process typed `EventBus`. Operator commands go through a single `ControlPlane`.

## Consequences
* No IPC, broker or serialization on the hot path. The UI sees events within milliseconds.
* Portfolio and order state have a single writer, so there are no locks.
* A CPU-heavy task can stall the loop. Rule: anything > ~5 ms of CPU (calibration fitting,
  backtests) runs in `asyncio.to_thread` or a separate CLI command.
* If we ever need multiple engines, the `EventBus` interface can be backed by Redis/NATS
  without changing producers or consumers.
