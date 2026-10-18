# ADR-0002: Raw HTTP/WS for market data; official SDK only for order writes

**Status:** accepted (2026-10-17)

## Context
Polymarket ships an official unified SDK (`polymarket-client`, 0.x, first released July 2026,
with frequent breaking minor releases). It also documents stable public REST/WS endpoints. Order
signing (EIP-712, CTF vs Protocol V2 routing, relayer/gasless) is subtle and easy to get wrong.

## Decision
* **Reads** (Gamma, CLOB book, market WS) use our own thin clients on `httpx`/`websockets`.
  We control parsing, reconnect, back-pressure and book maintenance, and we pin no SDK for reads.
* **Writes** (place/cancel orders, account trades, balances) go through the official SDK,
  isolated in `execution/polymarket_live.py` and imported lazily (optional extra `live`).

## Consequences
* SDK churn affects one file. Paper and scanner modes don't need the SDK at all.
* We must track WS protocol changes ourselves. `docs/POLYMARKET_API.md` is the checklist.
