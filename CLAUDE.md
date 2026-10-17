# CLAUDE.md — working on pmtrader

Polymarket scanner + autotrader. **The #1 goal is risk-adjusted profit**: correctness of pricing,
fees, sizing and risk controls matters more than features. Read `ARCHITECTURE.md` first. The math lives in
`docs/STRATEGIES.md`, risk in `docs/RISK.md`, the LLM gate in `docs/LLM_GATE.md`, and venue quirks in `docs/POLYMARKET_API.md`.

## Commands

```bash
uv sync                          # install (add --extra live for real trading)
uv run pytest -q                 # all tests (fast, no network)
uv run ruff check src tests && uv run ruff format src tests
uv run mypy                      # must stay clean
uv run pmtrader scan --seconds 60   # scanner only, live data, terminal table
uv run pmtrader run              # engine + web UI on http://127.0.0.1:8765 (paper by default)
uv run pmtrader pnl --period 7d  # P&L from the local DB
cd web && npm install && npm run check && npm run build   # UI (served by FastAPI from web/dist)
make check                       # lint + types + tests + UI check
```

## Layout (one line each)

- `src/pmtrader/quant/` — **pure math** (fees, Kelly, digital/barrier pricing, vol + seasonality, arbitrage walks, A-S quotes, rewards, calibration). No I/O. Every function is unit/property tested.
- `src/pmtrader/strategies/` — `detect()` / `plan_entry()` / `manage()` per strategy. `detect` must be pure given `ScanContext`. `engine.py` routes opportunities → LLM gate → OMS.
- `src/pmtrader/risk/engine.py` — the only gate to the exchange. `execution/oms.py` calls `risk.check()` for every order.
- `src/pmtrader/control/plane.py` — trading state machine (RUNNING/PAUSED/HALTED) and **all** operator commands (web, Discord, CLI), audited.
- `src/pmtrader/portfolio/` — `ledger.py` (pure, average cost) is rebuilt from the `fills` + `settlements` tables on boot. `service.py` is the single writer.
- `src/pmtrader/marketdata/` — Gamma catalog, sharded CLOB WS books, Binance/Deribit reference data, question parser.
- `src/pmtrader/api/` — FastAPI REST + WS hub (snapshot + coalesced deltas with `seq`). `discord_bot/` — slash commands + persistent button panel.
- `src/pmtrader/app.py` — composition root (the only place that wires concrete classes).
- `web/` — Svelte 5 (runes) + Vite + TS. State lives in `src/lib/store.svelte.ts`, REST in `src/lib/api.ts`.

## Rules (do not break)

1. **Never bypass `RiskEngine`.** New order paths go through `OrderManager.submit()`/`submit_group()`.
2. **Fees only via `quant/fees.py`.** Taker fee = `C·rate·(p(1−p))^exponent` from each market's `feeSchedule`.
3. **No I/O in `quant/` or in `Strategy.detect/manage`.** Strategies read the `ScanContext` only.
4. **Round at boundaries** (`round_price`, `round_size`, see ADR-0003). Every emitted order must be on tick.
5. **Kill switch semantics are sacred**: HALTED persists across restarts. Resuming needs confirm `RESUME`. State is persisted *before* side effects.
6. **The LLM chooses markets only.** It never sets size, price, strategy or exits. Structural arbitrage never calls the LLM.
7. **Secrets only via env/.env** (`PMT_SECRETS__*`, provider `api_key_env`). Never log or commit them.
8. Live mode requires `PMT_LIVE_CONFIRM=I_ACCEPT_RISK` and starts PAUSED. Don't weaken this.
9. Augmented neg-risk events and events whose open markets aren't provably exhaustive (`EventRef.basket_complete`) must never be basket-traded.
10. Use `apply_level` "new size" semantics for WS `price_change` (it is not a delta).

## Conventions

- Python 3.12+, asyncio, typed (`mypy` clean). Dataclasses (slots) inside the system, pydantic at the boundaries.
- Times are epoch seconds from `Clock` (use `ManualClock` in tests). Prices are probabilities (floats).
- New strategy: subclass `strategies.base.Strategy`, add it to `strategies/registry.py` and `config/default.yaml`, write the math in `quant/` with tests, and document it in `docs/STRATEGIES.md`. Ship it disabled (`enabled: false`) until it has been validated in paper trading.
- New operator command: add it to `ControlPlane` (audited), then expose it in `api/server.py` and `discord_bot/bot.py`.
- Tests: pure math → `tests/unit/quant`. Wiring → `tests/integration` using fixtures in `tests/conftest.py` (`world`, `core`).
- Log events with dotted names (`order.submitted`, `fill`, `risk.reject`, `control.*`, `pnl.*`). Those prefixes are routed to `logs/trades.jsonl`.

## Gotchas learned the hard way

- Crypto short-horizon pricing needs **seasonal volatility** (`quant/volatility.seasonal_profile`). BTC weekend variance is ~0.4× the weekly average and US-open hours are ~2.5×. A flat σ produced fake 10¢ "edges".
- Model fair values are **blended with the market mid** (`model_weight`, default 0.6). Never trade a raw model against an efficient market.
- Book freshness is **connection-level** (`OrderBook.link` → shard `last_rx`). Quiet books on a live socket are valid.
- Gamma `outcomes`, `outcomePrices` and `clobTokenIds` are JSON strings inside JSON. `rewardsMaxSpread` is in cents.
- `api.binance.com` is geo-blocked in some regions. Use `data-api.binance.vision` (configurable).
- YAML `off` parses as boolean false. Quote it (`gate_mode: "off"`).
- The paper fill model does not consume liquidity from the real book, so repeated FAKs can double-count depth. Treat paper P&L of large taker strategies as optimistic.

## Status / next steps

See ARCHITECTURE.md §15. The highest-value next steps: a replay backtester over `data/recordings`, markout tracking per market (MM), reconciliation against `list_open_orders` in live mode, and on-chain merge/redeem for completed sets.
