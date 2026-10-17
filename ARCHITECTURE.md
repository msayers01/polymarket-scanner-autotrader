# Architecture — Polymarket Scanner + Autotrader (`pmtrader`)

> Status: v0.1 foundation. This is the source of truth for how the system is put
> together. Update it in the same PR as any change that moves a boundary.

## 1. Goals and non-goals

**Primary goal: risk-adjusted profit.** Every design choice is judged by whether
it makes positive-expectancy trading more likely and catastrophic loss less likely.

| # | Requirement | Where it lives |
|---|---|---|
| R1 | Scanner usable on its own as an information tool for manual trading | `scanner/`, `pmtrader scan`, Web UI "Opportunities" |
| R2 | Scanner drives an autotrader that enters/exits by strategy | `scanner/` → `strategies/` → `risk/` → `execution/` |
| R3 | Strategies that fit the market and are mathematically sound | `quant/` (pure math) + `strategies/`, documented in `docs/STRATEGIES.md` |
| R4 | P&L summaries, reported and logged | `portfolio/`, `reports`, DB snapshots, JSONL logs, Discord |
| R5 | Discord bot: notifications + kill/pause/resume/flatten buttons | `discord_bot/` |
| R6 | LLM pre-processing for *market selection only*, token-frugal, cloud or local (Qwen) | `llm/`, `scanner/digest.py`, `docs/LLM_GATE.md` |
| R7 | Minimal, real-time browser UI with trade ticket and kill switch | `api/` (FastAPI + WebSocket) + `web/` (Svelte) |
| R8 | Risk controls incl. a daily limit on new markets | `risk/`, runtime-editable via UI/Discord |

**Non-goals (for now):** multi-user / multi-account, HFT-grade latency
(sub-millisecond), running in the cloud as a SaaS, and portfolio margining across venues.

## 2. Technology choices

| Concern | Choice | Why |
|---|---|---|
| Language | **Python 3.12+** | The official Polymarket SDK (`polymarket-client`) is Python, the quant ecosystem is Python, and it is easy for an LLM or a human to extend. The latency budget is in milliseconds to seconds, which Python handles. |
| Concurrency | **asyncio**, single process | Every component is I/O bound. One event loop means no locks on portfolio state, and the UI and bot can see engine state in-process with no IPC. |
| Packaging | **uv** + `pyproject.toml` (hatchling) | Fast and reproducible (`uv.lock`). |
| Config | **pydantic-settings** + YAML (`config/*.yaml`) + `.env` for secrets | Typed, validated config. Secrets stay out of YAML and git. |
| Domain models | **pydantic v2** at boundaries; `dataclass(slots=True)` in hot paths | Validation where data enters the system, speed inside it. |
| HTTP | **httpx** (async, HTTP/2) | Gamma, CLOB REST, Data API, and crypto price feeds. |
| WebSocket client | **websockets** | The CLOB market channel. Using the raw protocol keeps book handling under our control. |
| Exchange writes | **`polymarket-client` (official unified SDK)**, behind an adapter | Order signing (EIP-712, Protocol V2 / CTF routing) is subtle. The SDK is young (0.x), so it is isolated in `execution/polymarket_live.py` and imported lazily. |
| Persistence | **SQLite (WAL) via SQLAlchemy 2.0 async + aiosqlite** | Zero-ops, a single file, durable, and enough for one trader. Can move to Postgres by changing the URL. |
| Logging | **structlog** → JSON lines (`logs/*.jsonl`) + pretty console | Machine-parseable, so P&L and trades can be audited and grepped. |
| Web backend | **FastAPI + uvicorn** | Async-native, WebSocket support, typed. |
| Web frontend | **Svelte 5 + Vite + TypeScript** | Small bundle, fine-grained reactivity (good for streaming ticks), little boilerplate. Built to `web/dist` and served by FastAPI. |
| Discord | **discord.py 2.x** | Slash commands, persistent button views, embeds. |
| LLM (cloud) | **`anthropic` SDK** (Claude) + an **OpenAI-compatible** HTTP adapter (ChatGPT) | Structured JSON output and prompt caching. |
| LLM (local) | Same OpenAI-compatible adapter → **Ollama / LM Studio / llama.cpp / vLLM** (Qwen etc.) | One adapter covers every local server. |
| Numerics | **numpy** + stdlib `math` (no scipy) | `Φ` comes from `math.erf`, and isotonic regression is our own PAV, which keeps dependencies light. |
| Tests | **pytest, pytest-asyncio, hypothesis** | Property tests for the pricing/arb math, which is where bugs cost money. |
| Lint/types | **ruff** (lint + format), **mypy** | CI gates. |

## 3. Architectural style

**Hexagonal (ports and adapters) around an event-driven core.**

```
             ┌──────────────────────── adapters (I/O) ─────────────────────────┐
  Gamma REST ─┤ marketdata.gamma     execution.polymarket_live ├─ CLOB (orders)
  CLOB WS ────┤ marketdata.clob_ws   execution.paper           │
  Binance/Deribit ─ marketdata.external                         │
  Claude/OpenAI/Ollama ─ llm.providers                          │
  SQLite ─────┤ persistence                                      │
  Browser ────┤ api (FastAPI/WS)    discord_bot ├─ Discord       │
             └──────────────────────────┬───────────────────────────────────────┘
                                         │ ports (Protocols)
             ┌──────────────────────────┴───────────── core ────────────────────┐
             │ quant/ (pure math)  strategies/  risk/  portfolio/ledger  control │
             │                 EventBus (typed, in-process pub/sub)            │
             └──────────────────────────────────────────────────────────────────┘
```

Rules (CI enforces some; reviewers enforce the rest):

1. **`quant/` is pure.** It does no I/O, no asyncio and no global state, and is deterministic. That makes it testable and safe to reuse in backtests.
2. **Only `execution/` talks to the exchange's write path**, and only through `ExecutionGateway`.
   **Every order goes through `RiskEngine.check()`** first, including manual orders from the UI.
3. **Components communicate via the `EventBus`**, not by calling each other's internals.
   Commands (pause, kill, place order) go through the `ControlPlane`, so Web, Discord and CLI share one code path and one audit log.
4. **The composition root** (`app.py`) is the only place that builds concrete adapters and wires them together. That gives dependency injection without a framework.
5. **One writer per piece of state.** The portfolio is mutated only by `PortfolioService` (from fills), orders only by the `OrderManager`, and trading state only by the `ControlPlane`.

### Patterns used

| Pattern | Where | Purpose |
|---|---|---|
| Ports & adapters | `ExecutionGateway`, `LLMProvider`, `Clock`, repositories | Swap paper/live, cloud/local, and real/simulated time. |
| Observer / pub-sub | `core/bus.py` | Decouples producers (feeds) from consumers (scanner, UI, bot). |
| Strategy + registry | `strategies/registry.py` | Plug-in strategies selected by config. |
| State machine | `control/plane.py` (RUNNING/PAUSED/HALTED), `execution/oms.py` (order lifecycle) | Makes illegal transitions impossible, e.g. trading while HALTED. |
| Command + audit log | `ControlPlane` | Every operator action is replayable and attributable (web/discord/cli). |
| Chain of responsibility | `RiskEngine` checks | Each check is small, ordered and individually testable. |
| Specification / composite | `strategies/exits.py` exit rules | Composable exit policies per strategy. |
| Supervisor | `core/supervisor.py` | Restarts crashed tasks with backoff, and escalates to HALT if a critical task keeps dying. |
| Repository | `persistence/repositories.py` | Keeps SQL out of the domain. |
| Coalescing/back-pressure | Bus subscriber queues, WS hub frames | Market data never blocks order/fill handling. |

## 4. Runtime topology

One OS process (`pmtrader run`) hosts everything:

```
uvicorn (FastAPI app)
 └─ lifespan → Engine.start()
     ├─ MarketCatalog        (Gamma poll, 60s)            → MarketsUpdated
     ├─ MarketDataService    (CLOB WS, book store)        → BookUpdated / TradePrint / FeedHealth
     ├─ ExternalPriceFeed    (Binance/Deribit spot & vol) → ReferencePrice
     ├─ Scanner              (every N ms on dirty books)  → ScanCompleted(opportunities)
     ├─ LLMGate              (optional, budgeted)         → GateDecision
     ├─ StrategyEngine       (entries/exits per strategy) → OrderIntent
     ├─ RiskEngine           (sync check inside OMS)      → RiskBreach
     ├─ OrderManager + ExecutionGateway (paper|live)      → OrderUpdated / Fill
     ├─ PortfolioService     (ledger, marks, snapshots)   → PortfolioUpdated / PnLSnapshot
     ├─ ReportScheduler      (daily/periodic P&L)         → PnLReport
     ├─ ControlPlane         (state machine + commands)   → TradingStateChanged
     ├─ WebSocketHub         (UI fan-out)
     └─ DiscordBot           (optional)
```

Modes (`mode:` in config):

* `scanner` — data + scanner + UI + Discord only. The execution gateway is `NullGateway`, so no orders are possible. This satisfies R1.
* `paper` — everything, with `PaperGateway`, which fills against the live order book. **This is the default.**
* `live` — `PolymarketLiveGateway`. It requires secrets **and** `PMT_LIVE_CONFIRM=I_ACCEPT_RISK` in the environment.

## 5. Data flow (happy path for an autotrade)

```
Gamma ──► MarketCatalog ──► universe filter ──► MarketDataService.subscribe(tokens)
CLOB WS ─► OrderBook store ──► BookUpdated ──► Scanner
Scanner: features (pure) → each Strategy.detect(snapshot) → Opportunity(edge, EV, size_hint)
         → rank by EV/$ after fees → ScanCompleted (UI, Discord /top, LLM digest)
Opportunity needs judgment?  ── yes ─► LLMGate (batched digest, cached, budgeted) ─► approve/skip
                             └─ no (structural arb) ───────────────────────────────► approve
StrategyEngine.on_approved → Strategy.plan_entry() → OrderIntent(s)
OrderManager.submit(intent) → RiskEngine.check(intent, portfolio, limits) → Gateway.place()
Gateway fills → Fill → PortfolioService.apply(fill) → PortfolioUpdated → UI/Discord
StrategyEngine.on_tick → Strategy.manage(position, snapshot) → ExitPolicy → OrderIntent(reduce)
```

## 6. Domain model (key types in `core/types.py`)

* `Market`: condition_id, question, slug, event (id, title, neg_risk), `outcomes: [Outcome(name, token_id)]`, tick_size, min_order_size, end_time, fee schedule (`rate`, `exponent`, `taker_only`, `rebate_rate`), rewards (min_size, max_spread, daily_rate), category/tags, accepting_orders.
* `OrderBook`: token_id, bids/asks as sorted price levels, timestamp, hash. Includes helpers for best bid/ask, mid, microprice, depth and VWAP walk.
* `Opportunity`: market + token(s), strategy name, side, fair value, edge (in probability points), expected value $/share net of fees, suggested size, confidence, `needs_judgment` flag, and a features dict.
* `OrderIntent` → `Order` (OMS-owned, state machine) → `Fill`.
* `Position`: token_id, qty (shares), avg_cost, realized P&L, fees, strategy, opened_at.
* `TradingState`: `RUNNING | PAUSED | HALTED` (+ reason, actor, since).

Prices are probabilities in `[0, 1]`, carried as `float` and rounded to the market tick at the order boundary. Sizes are shares (2dp), and money is USDC (6dp). See ADR-0003 for the reasoning.

## 7. Strategies (summary — math in `docs/STRATEGIES.md`)

| Strategy | Edge source | Judgment needed? | Exit |
|---|---|---|---|
| `complete_set_arb` | YES_ask + NO_ask + fees < 1 | No (structural) | Hold to resolution / merge |
| `negrisk_basket_arb` | Σ YES_ask over a mutually exclusive, exhaustive event < 1 (or Σ NO_ask < N−1) | No | Hold to resolution |
| `ladder_arb` | Monotonicity violation across strikes (P(>K₂) > P(>K₁) for K₂ > K₁) | No | Hold to resolution |
| `crypto_fair_value` | Model price (lognormal digital / barrier-touch via reflection principle) vs market | Light (model risk) | Edge decay, edge reversal, time stop |
| `market_maker` | Spread + maker rebates + liquidity rewards, with Avellaneda–Stoikov inventory control in logit space | Yes (market selection) | Continuous; inventory-driven unwinds |
| `longshot_fade` | Favourite–longshot bias from a fitted calibration curve (isotonic + Beta shrinkage) | Yes | Hold / edge decay / stop |

Sizing is **fractional Kelly with uncertainty shrinkage**, capped by risk limits and by visible depth. Fees use the venue formula `fee = C · rate · (p·(1−p))^exponent` (taker only, where configured). Maker execution is preferred whenever the edge does not require crossing the spread.

## 8. Risk management (summary — details in `docs/RISK.md`)

* **Pre-trade checks (every order):** trading state, mode, per-order notional, per-market position, per-event exposure, gross exposure, open-order count, order rate, price band vs mid, tick/min-size validity, stale book, time-to-close, **daily new-market cap**, and a cash check.
* **Circuit breakers (continuous):** daily loss limit, drawdown from equity high-water mark, feed staleness, repeated rejects, and supervisor escalations. A breach → `PAUSED` (soft) or `HALTED` (hard), depending on severity.
* **Kill switch:** `HALTED` → cancel all open orders → optionally flatten → persist the state so a restart stays halted. Only an explicit `resume` with a confirm phrase clears it.
* All limits are editable at runtime (UI/Discord), validated, persisted and audited.

## 9. LLM gate (summary — details in `docs/LLM_GATE.md`)

The LLM **only decides which markets to enter**. Strategy choice, sizing, execution and exits are deterministic.
Token frugality works in layers:

1. Deterministic pre-filters and ranking (zero tokens).
2. Structural arbitrage bypasses the LLM entirely.
3. Compact, pipe-delimited digest rows (~40–60 tokens per market), batched N at a time.
4. A stable system prompt (cache-friendly), strict JSON output, and a small `max_tokens`.
5. A decision cache keyed by (market, price bucket, rule version) with a TTL. The LLM is re-asked only on material change.
6. Hard daily budgets (calls, tokens, $). When a budget is exhausted the gate falls back to the configured policy (`skip` or `deterministic`).
7. A provider cascade: local Qwen first (free), escalating to the cloud model only for borderline or high-notional candidates (optional).

## 10. Real-time UI

* `GET /api/snapshot` returns the full state. `WS /ws` sends a `snapshot` and then `delta` frames, each with a monotonically increasing `seq`.
* The server coalesces high-frequency updates (books, scanner) into ≤10 frames/s per client. Fills, orders, state changes and alerts go out immediately.
* The client detects a `seq` gap and asks for a resync. It reconnects with backoff, and shows a "stale" banner if no heartbeat arrives within 3 s.
* Auth: binds to `127.0.0.1` by default. Optional bearer token (`PMT_WEB_TOKEN`), passed as a query param for the WS.
* Layout (only the essentials): a status bar (mode, state, equity, today P&L, exposure, markets today N/max, feed health, PAUSE/RESUME, KILL); Opportunities; Positions; Open Orders; Activity; a collapsible Risk Limits panel; and a Trade Ticket modal.

## 11. Discord bot

* Slash commands: `/status`, `/pnl [today|7d|all]`, `/top [n]`, `/positions`, `/pause`, `/resume`, `/kill`, `/flatten`, `/limits`, `/setlimit <name> <value>`, `/panel`.
* A persistent **control panel** message with buttons: Pause, Resume, Kill (2-step confirm), Flatten (2-step confirm), P&L, Status. It survives restarts (persistent `View` with custom_ids).
* Notifications: fills, risk breaches/state changes, daily P&L summary (scheduled), and startup/shutdown.
* Authorization: commands and buttons only work for `discord.owner_user_ids`. Everyone else gets an ephemeral refusal.

## 12. Persistence and observability

SQLite tables: `orders`, `fills`, `positions`, `pnl_snapshots`, `daily_stats`, `opportunities` (sampled), `llm_decisions` (with token usage and cost), `audit_log`, `risk_events`, `settings_overrides`, `kv` (trading state, high-water mark).
Logs: `logs/pmtrader.jsonl` (all events) and `logs/trades.jsonl` (orders/fills only). Daily P&L reports go to `data/reports/pnl-YYYY-MM-DD.json` and `.md`.
Market data recording (`pmtrader record`) writes compressed JSONL book/trade streams to `data/recordings/`, which feed backtests and calibration.

## 13. Failure modes and responses

| Failure | Detection | Response |
|---|---|---|
| WS disconnect / silent stall | heartbeat + per-book staleness | Reconnect with backoff and re-snapshot. Stale books block new orders; quotes are pulled after `stale_data_seconds`. |
| Exchange reject storm | consecutive reject counter | Pause the strategy, then halt if it is systemic. |
| Order state drift | periodic reconcile with `list_open_orders` / trades | The exchange is the source of truth. Orphans are cancelled and drift is alerted. |
| Crash/restart | persisted state + reconcile on boot | Comes back in the persisted `TradingState`. Never auto-resumes from HALTED. |
| LLM down / over budget | provider errors, budget counters | Fallback policy; trading continues for structural strategies. |
| Bad model params / parse error | strategy guards (confidence, sanity bands) | Skip the market. Never trade on a parse we are unsure of. |

## 14. Directory map

```
src/pmtrader/
  app.py              composition root (Engine)
  cli.py              `pmtrader` CLI (run | scan | record | calibrate | pnl)
  config.py           typed settings (YAML + env)
  logging_setup.py
  core/               bus, events, types, orderbook, clock, supervisor
  quant/              pure math: fees, kelly, digital, barrier, avellaneda_stoikov,
                      arbitrage, calibration, volatility, rewards, microstructure
  marketdata/         gamma, clob_rest, clob_ws, catalog, feed, external/, question_parser
  scanner/            features, filters, scanner, digest
  strategies/         base, registry, exits, engine, <one file per strategy>
  llm/                gate, budget, cache, prompts, providers/
  risk/               limits, engine
  execution/          gateway, paper, polymarket_live, oms
  portfolio/          ledger (pure), service, reports
  control/            plane (state machine + commands + audit)
  persistence/        db, models, repositories
  api/                server, routes, ws hub, auth
  discord_bot/        bot, views, formatting
  backtest/           recorder, replay (skeleton)
web/                  Svelte 5 + Vite + TS single-page UI
config/               default.yaml (+ local.yaml overrides, git-ignored)
docs/                 STRATEGIES, RISK, LLM_GATE, RUNBOOK, POLYMARKET_API, adr/
tests/                unit/ (pure), integration/ (wired with fakes)
```

## 15. Roadmap

1. **v0.1 (this commit):** foundation; paper trading end to end; scanner; structural arb + crypto fair value; risk; UI; Discord; LLM gate.
2. **v0.2:** recorder + replay backtester with a queue-position fill model, calibration fitting from resolved markets (`pmtrader calibrate`), and market maker hardening (toxicity filter, reward score tracking).
3. **v0.3:** live trading soak in small size, reconciliation hardening, merge/redeem automation, and per-strategy capital allocation (risk parity on realized Sharpe).
4. **v0.4:** news/event ingestion for the LLM gate (headlines only, cheap), and cross-venue reference prices.
