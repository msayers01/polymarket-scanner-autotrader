# Polymarket API notes (verified 2026-10-17)

The venue changes often. **Verify against https://docs.polymarket.com (index: `/llms.txt`) before relying on anything here.**
Each doc page is also available as markdown by appending `.md` to its URL.

## Endpoints we use

| Purpose | Endpoint | Auth | Module |
|---|---|---|---|
| Market/event catalog | `GET https://gamma-api.polymarket.com/markets`, `/events` (`active`, `closed`, `limit`, `offset`, `order`, `ascending`, `tag_slug`) | none | `marketdata/gamma.py` |
| Order book snapshot | `GET https://clob.polymarket.com/book?token_id=…` | none | `marketdata/clob_rest.py` |
| Price history | `GET https://clob.polymarket.com/prices-history?market=<token_id>&interval=…&fidelity=…` | none | `marketdata/clob_rest.py` |
| Market stream | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | none | `marketdata/clob_ws.py` |
| Orders / account | official SDK `polymarket-client` (`AsyncSecureClient`) | private key (+ relayer key for gasless) | `execution/polymarket_live.py` |
| Crypto reference | `https://data-api.binance.vision/api/v3/...` (klines, ticker). `api.binance.com` is geo-blocked from some regions. | none | `marketdata/external/crypto.py` |
| Implied vol | `https://www.deribit.com/api/v2/public/get_volatility_index_data` (DVOL) | none | `marketdata/external/crypto.py` |

## Gamma market fields that matter

`id`, `conditionId`, `question`, `slug`, `endDate`, `outcomes` (JSON string), `outcomePrices` (JSON string),
`clobTokenIds` (JSON string, same order as `outcomes`), `orderPriceMinTickSize`, `orderMinSize`, `negRisk`,
`acceptingOrders`, `active`, `closed`, `liquidityNum`, `volume24hr`, `bestBid`, `bestAsk`, `spread`, `lastTradePrice`,
`oneHourPriceChange`, `oneDayPriceChange`, `feesEnabled`, `feeSchedule {rate, exponent, takerOnly, rebateRate}`,
`rewardsMinSize`, `rewardsMaxSpread` (cents), `gameStartTime`, `events[0] {id, title, slug, negRisk}`,
`umaResolutionStatus`, `version` ("v1" = CTF token ids; "v2" = Protocol V2 position ids).

Quirks:
* `outcomes`, `outcomePrices` and `clobTokenIds` are **JSON-encoded strings** inside JSON.
* `feeSchedule` may be missing. Treat a missing schedule plus `feesEnabled != true` as zero fees.
* `rewardsMaxSpread` is in **cents**.

## Market WebSocket protocol

* Subscribe: `{"assets_ids": [...], "type": "market", "custom_feature_enabled": true}`
* Add/remove dynamically: `{"assets_ids": [...], "operation": "subscribe" | "unsubscribe"}`
* Heartbeat: send the text `PING` every 10 s; the server answers `PONG`.
* Events (`event_type`): `book` (full snapshot), `price_change` (level updates in `price_changes[]`, each with `asset_id`, `price`, `size`, `side`, `best_bid`, `best_ask`), `last_trade_price`, `tick_size_change`, plus `best_bid_ask`, `new_market` and `market_resolved` with custom features on.
* A `price_change` `size` is the **new total size at that level** (0 = remove the level), not a delta.
* Messages may arrive as a JSON **array** of events.

## Fees

`fee = C × rate × (p(1−p))^exponent`, charged to takers at match time and rounded to 5 dp. Makers pay none and receive `rebateRate` of the taker fee.

## Trading through the SDK (`polymarket-client` ≥ 0.13)

```python
from polymarket import AsyncSecureClient
client = await AsyncSecureClient.create(private_key=..., wallet=..., api_key=RelayerApiKey(...))
await client.place_limit_order(asset_id=..., side="BUY", price="0.52", size="10", post_only=True, expiration=None)
await client.place_market_order(asset_id=..., side="BUY", amount="10", max_price=..., order_type="FAK")
await client.cancel_order(order_id=...); await client.cancel_all()
client.list_open_orders(...); client.list_account_trades(...)      # paginators
```
`asset_id` is `token_id` for v1 (CTF) markets and `position_id` for v2 markets. Our `Outcome.asset_id` already resolves this.

## Liquidity rewards

Per-order score `S(v,s) = ((v − s)/v)² · b` within max spread `v` (cents) of the size-cutoff-adjusted midpoint, for size ≥ `rewardsMinSize`.
Two-sided scoring uses `Q_min = max(min(Q_one, Q_two), max(Q_one, Q_two)/c)` with c = 3 when mid ∈ [0.10, 0.90]; otherwise only two-sided liquidity scores (`min`).
The book is sampled every minute and rewards are paid daily (minimum $1).

## Jurisdiction

Polymarket's international venue restricts some jurisdictions (including the US); Polymarket US is a separate, regulated venue.
**You are responsible for using the venue lawfully.** This software adds no geo-circumvention, and none should ever be added.
