# Runbook

## Install

```bash
# Python 3.12+ and uv (https://docs.astral.sh/uv/)
uv sync                      # paper/scanner
uv sync --extra live         # + official Polymarket SDK for live trading
cd web && npm install && npm run build && cd ..
cp .env.example .env         # fill in only what you use
```

Windows: run the same commands in PowerShell. `uv run pmtrader ...` works the same.

## Everyday use

| Goal | Command |
|---|---|
| Just watch opportunities (no trading) | `uv run pmtrader scan` or `uv run pmtrader run --mode scanner` |
| Paper trade with the web UI | `uv run pmtrader run` → open http://127.0.0.1:8765 |
| P&L summary | `uv run pmtrader pnl --period today\|7d\|all`, the UI, or `/pnl` in Discord |
| Record order-book data for research | `uv run pmtrader record --minutes 240` |
| Fit the longshot calibration curve | `uv run pmtrader calibrate` (writes `data/calibration.json`) |
| Inspect the effective config | `uv run pmtrader check-config` |

Daily P&L reports are written to `data/reports/` and posted to Discord at `reports.daily_time` (in `reports.timezone`).
Every trade and command is logged to `logs/trades.jsonl` (all events go to `logs/pmtrader.jsonl`).

## Local LLMs (Qwen etc.)

1. Run Ollama (`ollama serve`, `ollama pull qwen3:14b`) or LM Studio's server.
2. In `config/local.yaml`:
   ```yaml
   llm:
     gate_mode: advisory          # start with advisory; switch to "required" once it proves useful
     providers:
       - {name: qwen-local, kind: openai_compat, base_url: "http://localhost:11434/v1", model: "qwen3:14b"}
   ```
3. Optionally add a Claude provider as the second tier (`kind: anthropic`, `api_key_env: ANTHROPIC_API_KEY`).
   It is only consulted for borderline or high-notional candidates.

## Discord

1. Create an application and bot at https://discord.com/developers. Invite it with the `applications.commands` and `bot` scopes (Send Messages, Embed Links).
2. `.env`: `PMT_SECRETS__DISCORD_TOKEN=...`
3. `config/local.yaml`:
   ```yaml
   discord: {enabled: true, guild_id: <server id>, channel_id: <channel id>, owner_user_ids: [<your user id>]}
   ```
4. In the channel run `/panel` once to post the button panel (Status · P&L · Positions · Pause · Resume · KILL · Kill+Flatten).

## Web UI on another device

The UI binds to `127.0.0.1` by default. To reach it from another device on your LAN, set `api.host: 0.0.0.0` **and** `PMT_SECRETS__WEB_TOKEN`, then open `http://<pc-ip>:8765/?token=<token>`. Never expose it to the internet without a VPN or tunnel with auth.

## Going-live checklist

1. ☐ At least 2 weeks of paper trading with positive P&L net of fees for each strategy you enable. Review `logs/trades.jsonl`.
2. ☐ You are permitted to use Polymarket from your jurisdiction.
3. ☐ A dedicated wallet funded with only what you can lose. Set `PMT_SECRETS__POLYMARKET_PRIVATE_KEY` / `_WALLET` (+ relayer key).
4. ☐ Conservative limits in `config/local.yaml`: `max_daily_loss_usd`, `max_gross_exposure_usd`, `max_order_usd`, `max_new_markets_per_day`.
5. ☐ Kill switch tested from both the web UI and Discord (paper mode).
6. ☐ `PMT_LIVE_CONFIRM=I_ACCEPT_RISK` and `uv run pmtrader run --mode live`. The engine starts **PAUSED**. Watch the first fills manually, then press Resume.
7. ☐ Start at ≤10% of target size for a week.

## Incidents

| Symptom | Action |
|---|---|
| Something looks wrong | **KILL** first (UI, Discord, or `curl -X POST localhost:8765/api/control/kill -H 'Content-Type: application/json' -d '{}'`), then investigate. |
| `HALTED` after a restart | Expected: halts persist. Read the reason in the UI banner or `audit_log`, then resume with the `RESUME` confirmation. |
| "Stream stale" banner | WS feed down. Orders are blocked automatically (stale-book check). Check network and `logs/pmtrader.jsonl` for `feed.disconnected`. |
| Daily loss breaker fired | The system HALTS and cancels orders. Positions stay open; decide whether to flatten. |
| Many `risk.reject` | `/api/health` → `risk_rejects` shows the codes. Usually limits or stale books, not bugs. |
