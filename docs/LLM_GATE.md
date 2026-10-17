# LLM gate — token-frugal market selection

**Scope:** The LLM answers one question: *"Of these candidate markets, which should we enter?"*
It never chooses the strategy, the size, the price or the exits. Those are deterministic (`strategies/`, `risk/`).

## Modes (`llm.gate_mode`)

| mode | behaviour |
|---|---|
| `off` | No LLM. Opportunities with `needs_judgment=True` follow `llm.fallback` (`skip` or `deterministic`). |
| `advisory` | The LLM verdict is shown in the UI/Discord, but trading follows the deterministic rules. Use this to evaluate the LLM's value for free in paper mode. |
| `required` | `needs_judgment` opportunities are traded only if the LLM says `enter` with `conf ≥ llm.min_confidence`. |

Structural arbitrage (`needs_judgment=False`) **never** calls the LLM.

## Why it is cheap

1. **Funnel first.** The scanner filters thousands of markets to the top-K candidates per cycle with zero tokens. Only `needs_judgment` opportunities that already pass risk pre-checks get as far as the gate.
2. **Compact digest.** One pipe-delimited row per market (~40–60 tokens), and a header defines the columns once:
   ```
   id|question|cat|bid|ask|fair|edge_c|ttc_h|vol24k|liq_k|d1h_c|d24h_c|strat|side
   m7|Will the price of Bitcoin be above $74,000 on October 18?|crypto|.61|.62|.70|7.4|17.5|820|95|+2|-6|crypto_fair_value|YES
   ```
   Numbers are rounded and units fixed by the header. There are no descriptions, URLs or prose. Long questions are truncated at 120 chars.
3. **Batching.** Up to `llm.batch_size` (default 12) markets per call, with one shared system prompt.
4. **Stable, cache-friendly prefix.** The system prompt is a frozen constant (no timestamps, no IDs), so provider prompt caching applies when the prefix is long enough (Anthropic needs ≥ the model's minimum cacheable length; shorter prompts simply are not cached, which is fine since they are small). Local servers (Ollama/llama.cpp) reuse the KV cache for an identical prefix.
5. **Tiny, strict output.** JSON schema `{"d":[{"id":"m7","a":"enter|skip","c":0.0-1.0,"p":0.0-1.0|null,"r":"<=12 words"}]}` with `max_tokens ≈ 40·N + 50`. Claude enforces it via `output_config.format`, and OpenAI-compatible servers via `response_format`. A malformed reply is treated as a `skip` for every market in the batch.
6. **Decision cache.** Keyed on `(market_id, side, price_bucket(2¢), strategy, prompt_version)` with a TTL (default 6 h). A market is re-asked only if the key changes, i.e. the price moved materially or the TTL expired.
7. **Budgets.** `llm.daily_max_calls`, `llm.daily_max_tokens` and `llm.daily_max_usd` are hard caps, persisted in `daily_stats`. When one is exhausted the gate switches to `llm.fallback` and alerts once.
8. **Cascade (optional).** `llm.providers` is an ordered list, e.g. `[qwen-local, claude]`. The first provider answers every batch. The next one is consulted only for decisions with `0.45 ≤ conf ≤ 0.65` (borderline), or when the intended notional ≥ `llm.escalate_notional_usd`.

Expected cost with the defaults (cloud only, ~20 calls/day × ~1.2k tokens) is well under 50k tokens/day. With a local Qwen first tier, cloud usage is typically near zero.

## Providers

| name | adapter | example config |
|---|---|---|
| Claude | `llm/providers/anthropic_provider.py` (official `anthropic` SDK) | `model: claude-opus-5-5`, `effort: low` |
| ChatGPT / OpenAI | `llm/providers/openai_compat.py` | `base_url: https://api.openai.com/v1`, `model: <your choice>` |
| Local Qwen (Ollama) | `openai_compat.py` | `base_url: http://localhost:11434/v1`, `model: qwen3:14b` |
| LM Studio / llama.cpp / vLLM | `openai_compat.py` | `base_url: http://localhost:1234/v1` |

The model choice is yours. Cheaper Claude tiers (e.g. `claude-haiku-5-5`) can be set per provider in `config/local.yaml`.

## Prompt contract (`llm/prompts.py`)

The system prompt tells the model to:
* act as a prediction-market analyst choosing *whether to enter*, given a quantitative pre-screen;
* skip when resolution criteria are ambiguous, the event is likely already decided (stale market), the market is a duplicate or correlated with an existing position, or the information edge is unclear;
* return `p` (its own probability) only if confident, otherwise `null`;
* never invent data and never output anything but the JSON.

`prompt_version` is bumped whenever the prompt changes, which invalidates the cache.

## Measuring whether the LLM adds value

Every decision is stored in `llm_decisions` with the opportunity's fair value and the market's later
resolution. `pmtrader pnl --llm-report` (v0.2) compares the hit rate and Brier score of `enter` vs `skip`
decisions, and the P&L of gated vs ungated paper trades. Keep `required` mode only if it improves P&L net of token cost.
