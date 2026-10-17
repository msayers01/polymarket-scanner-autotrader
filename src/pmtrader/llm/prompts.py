"""Frozen prompt + output schema for the market-selection gate (docs/LLM_GATE.md).

Do not interpolate anything volatile (time, ids) into SYSTEM_PROMPT — a stable prefix is what
makes provider-side prompt caching / KV reuse work. Bump PROMPT_VERSION on any edit.
"""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "gate-v1"

SYSTEM_PROMPT = """You are the market-selection filter of a quantitative Polymarket trading system.
A deterministic scanner already found each candidate below and estimated a fair probability and edge.
Your ONLY job: decide for each candidate whether the system should ENTER it. You do not choose size,
price, strategy or exits.

Columns (pipe-delimited): id | question | cat=category | bid,ask = best prices of the outcome we would buy (0-1) |
fair = scanner's fair probability for that outcome | edge_c = net edge in cents per share after fees |
ttc_h = hours to resolution | vol24k = 24h volume in $k | d1h_c, d24h_c = price change in cents over 1h/24h |
strat = strategy | side = outcome we would buy | note = scanner note.

SKIP when any of these hold:
- resolution criteria are likely ambiguous, subjective, or disputed;
- the outcome is probably already determined or about to be (stale market, event already happened);
- the scanner's fair value likely ignores material public information (breaking news, injuries, announcements);
- the edge is plausibly an artifact (illiquid book, recent large move suggesting informed flow);
- the question duplicates or is highly correlated with another candidate in this batch (enter at most the best one).
ENTER only when the quantitative edge looks genuine and you know of no reason it is illusory.

For each candidate return: id; a = "enter" or "skip"; c = your confidence in that decision (0-1);
p = your own probability for the outcome we would buy, or null if you have no independent view;
r = reason, at most 12 words. Return only the JSON object. Never invent data."""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "d": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "a": {"type": "string", "enum": ["enter", "skip"]},
                    "c": {"type": "number"},
                    "p": {"type": ["number", "null"]},
                    "r": {"type": "string"},
                },
                "required": ["id", "a", "c", "p", "r"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["d"],
    "additionalProperties": False,
}


def max_output_tokens(n: int) -> int:
    return 40 * n + 60
