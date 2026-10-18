# pmtrader — Polymarket scanner + autotrader

A single-process trading system for Polymarket. It has three parts:

* **Scanner**: streams live order books for the most active markets, runs mathematically grounded detectors
  (structural arbitrage, model-priced crypto markets, liquidity-reward market making, calibration-based longshot fading),
  and ranks opportunities by annualised net return after fees. Use it on its own as a trading information tool.
* **Autotrader**: turns approved opportunities into orders through a strict risk engine (daily market cap, loss and
  drawdown breakers, exposure limits, a persistent kill switch). It runs in paper mode against live books by default.
* **Interfaces**: a minimal real-time web UI (trade ticket, positions, kill switch), a Discord bot (notifications and phone
  control panel), and a token-frugal LLM gate (Claude, ChatGPT or local Qwen) that only decides *which markets to enter*.

```bash
uv sync && (cd web && npm install && npm run build)
uv run pmtrader scan          # terminal scanner, no trading
uv run pmtrader run           # paper trading + UI at http://127.0.0.1:8765
```

| Doc | What's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Design, patterns, technology choices, data flow |
| [docs/STRATEGIES.md](docs/STRATEGIES.md) | The math for every strategy, sizing, and fees |
| [docs/RISK.md](docs/RISK.md) | Risk limits, breakers, kill-switch semantics |
| [docs/LLM_GATE.md](docs/LLM_GATE.md) | How the LLM is used without burning tokens |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Install, operate, Discord/LLM setup, going-live checklist |
| [docs/POLYMARKET_API.md](docs/POLYMARKET_API.md) | Venue endpoints, protocol, quirks |
| [CLAUDE.md](CLAUDE.md) | Conventions for contributors (human or AI) |

> **Risk warning.** Trading prediction markets can lose money. This software is provided as is. Paper trade first,
> size small, and make sure you may legally use Polymarket where you live.
