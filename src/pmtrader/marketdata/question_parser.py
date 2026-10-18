"""Parse crypto price-market questions into structured contracts.

Only exact template matches are returned (confidence 1.0). Anything else → ``None`` and the
strategy skips the market. Never trade a parse we are unsure of (docs/STRATEGIES.md §4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["above", "below", "between", "touch_up", "touch_down", "updown"]

ASSETS = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "solana": "solana",
    "sol": "solana",
    "xrp": "xrp",
    "dogecoin": "dogecoin",
    "doge": "dogecoin",
    "bnb": "bnb",
}

_NUM = r"\$([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM])?"
_ASSET = r"(bitcoin|btc|ethereum|eth|solana|sol|xrp|dogecoin|doge|bnb)"


@dataclass(frozen=True, slots=True)
class CryptoContract:
    asset: str
    kind: Kind
    strike: float | None = None
    strike_hi: float | None = None  # for "between"
    window_seconds: float | None = None  # for updown: candle length (start = end − window)


def _num(s: str, suffix: str | None) -> float:
    v = float(s.replace(",", ""))
    if suffix:
        v *= 1_000 if suffix.lower() == "k" else 1_000_000
    return v


_RE_ABOVE = re.compile(rf"^will the price of {_ASSET} be (above|greater than|higher than) {_NUM} on .+\?$", re.I)
_RE_BELOW = re.compile(rf"^will the price of {_ASSET} be (below|less than|lower than) {_NUM} on .+\?$", re.I)
_RE_BETWEEN = re.compile(rf"^will the price of {_ASSET} be between {_NUM} and {_NUM} on .+\?$", re.I)
_RE_REACH = re.compile(rf"^will {_ASSET} (reach|hit) {_NUM} .+\?$", re.I)
_RE_DIP = re.compile(rf"^will {_ASSET} (dip to|fall to|drop to) {_NUM} .+\?$", re.I)
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)"
_RE_UPDOWN_RANGE = re.compile(rf"^{_ASSET} up or down\s*-\s*.+?,\s*{_TIME}\s*-\s*{_TIME}\s*et\??$", re.I)
_RE_UPDOWN_HOUR = re.compile(rf"^{_ASSET} up or down\s*-\s*.+?,\s*{_TIME}\s*et\??$", re.I)
_RE_UPDOWN_DAY = re.compile(rf"^{_ASSET} up or down on .+\?$", re.I)


def _minutes(h: str, m: str | None, ap: str) -> int:
    hh = int(h) % 12 + (12 if ap.lower() == "pm" else 0)
    return hh * 60 + int(m or 0)


def parse_crypto_question(question: str) -> CryptoContract | None:
    q = " ".join(question.strip().split())
    if m := _RE_BETWEEN.match(q):
        return CryptoContract(ASSETS[m[1].lower()], "between", _num(m[2], m[3]), _num(m[4], m[5]))
    if m := _RE_ABOVE.match(q):
        return CryptoContract(ASSETS[m[1].lower()], "above", _num(m[3], m[4]))
    if m := _RE_BELOW.match(q):
        return CryptoContract(ASSETS[m[1].lower()], "below", _num(m[3], m[4]))
    if m := _RE_REACH.match(q):
        return CryptoContract(ASSETS[m[1].lower()], "touch_up", _num(m[3], m[4]))
    if m := _RE_DIP.match(q):
        return CryptoContract(ASSETS[m[1].lower()], "touch_down", _num(m[3], m[4]))
    if m := _RE_UPDOWN_RANGE.match(q):
        start = _minutes(m[2], m[3], m[4])
        end = _minutes(m[5], m[6], m[7])
        dur = (end - start) % (24 * 60)
        if dur <= 0:
            return None
        return CryptoContract(ASSETS[m[1].lower()], "updown", window_seconds=dur * 60.0)
    if m := _RE_UPDOWN_HOUR.match(q):
        return CryptoContract(ASSETS[m[1].lower()], "updown", window_seconds=3600.0)
    if m := _RE_UPDOWN_DAY.match(q):
        return CryptoContract(ASSETS[m[1].lower()], "updown", window_seconds=86400.0)
    return None
