# Risk management

The `RiskEngine` (`src/pmtrader/risk/engine.py`) is the **only** gate between an `OrderIntent` and the
exchange. It runs synchronously inside `OrderManager.submit()`. If it is not running, nothing trades.

## Trading states

```
            pause                 kill / hard breach
 RUNNING ───────────► PAUSED ───────────────────────► HALTED
    ▲   ◄───────────    │  ▲                            │
    │      resume       │  └────── resume (confirm) ────┘
    └───────────────────┘
```

| State | New entries | Exits / reduce-only | Quotes (MM) | Open orders |
|---|---|---|---|---|
| RUNNING | yes | yes | yes | kept |
| PAUSED | **no** | yes | pulled | entry orders cancelled |
| HALTED | **no** | only via manual *flatten* | pulled | **all cancelled** |

* The state is persisted (`kv.trading_state`). **A process restart never leaves HALTED automatically.**
* Leaving HALTED requires `resume` with the confirm phrase (`RESUME`) from an authorised actor.
* Every transition writes an `audit_log` row and sends a Discord alert.

## Limits (runtime-editable)

Defaults are in `config/default.yaml` under `risk:`. Overrides set from the UI or Discord are validated against hard ceilings (`risk_ceilings:`) and stored in `settings_overrides`.

| Limit | Default | Check |
|---|---|---|
| `max_new_markets_per_day` | 10 | Number of distinct markets in which a *new* position was opened today (UTC day, or `report.timezone`). |
| `max_daily_loss_usd` | 50 | realized + unrealized P&L since the day start ≤ −limit → **HALT** + cancel all. |
| `max_drawdown_pct` | 15 | Equity vs high-water mark → **HALT**. |
| `max_order_usd` | 50 | Notional of a single order. |
| `max_position_usd_per_market` | 100 | Cost basis + pending buys per market. |
| `max_exposure_per_event_usd` | 150 | Correlated outcomes (same Gamma event). |
| `max_gross_exposure_usd` | 500 | Sum of position costs + open buy notional. |
| `max_open_orders` | 40 | |
| `max_orders_per_minute` | 30 | Token bucket. Also protects against API rate limits. |
| `max_price_deviation` | 0.05 | \|limit − mid\| for taker orders (fat-finger guard). |
| `min_time_to_close_minutes` | 10 | No new entries this close to the end, unless the strategy declares `allows_near_expiry`. |
| `stale_book_seconds` | 5 | The book must be fresh to trade. |
| `min_market_liquidity_usd` | 1000 | From Gamma `liquidityNum`. |
| `kelly_fraction` | 0.25 | Global λ multiplier (strategies may use less). |
| `max_consecutive_rejects` | 5 | → PAUSE. |
| `allowed_strategies` | list | Strategies allowed to open positions. |
| `blocked_keywords` | list | Markets whose question matches are never traded. |

## Order of checks (chain of responsibility)

The order is cheapest first, and any failure short-circuits:

1. trading state / mode / strategy enabled / reduce-only exemption
2. market validity: accepting orders, tick size, min size, not blocked, time to close
3. book freshness and price band
4. rate limit and open-order count
5. per-order notional
6. per-market → per-event → gross exposure (pending orders included)
7. daily new-market cap (only for orders that would open a new market)
8. cash / balance (live: from the exchange; paper: simulated cash)

Every rejection is logged with a reason code (`RiskReject.code`) and counted. The UI shows the last N rejects.

## Kill switch semantics

`kill(reason, actor, flatten=False)`:
1. The state becomes HALTED (persisted first, so a crash mid-kill still leaves it halted).
2. All open orders are cancelled (`gateway.cancel_all()`), retried until the exchange confirms or 3 attempts pass. A failure raises a critical alert.
3. If `flatten`: reduce-only marketable orders close every position within `flatten_max_slippage`. Positions that cannot be closed are reported.
4. Alerts go to Discord and the UI.

## Operational guidance

* Start in `paper` mode. Go `live` only after reading `docs/RUNBOOK.md` → "Going live checklist".
* Keep `max_daily_loss_usd` at or below what you are genuinely comfortable losing *in a day without being asked*.
* Losses beyond the limit are possible through gaps (resolution events, illiquid exits), so size so that a gap is survivable.
