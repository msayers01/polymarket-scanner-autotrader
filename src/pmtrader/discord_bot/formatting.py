"""Embeds/text for Discord (phone-friendly: short lines, key numbers first)."""

from __future__ import annotations

from typing import Any

import discord

GREEN, RED, AMBER, GREY, BLUE = 0x2ECC71, 0xE74C3C, 0xF39C12, 0x95A5A6, 0x3498DB
STATE_COLOR = {"RUNNING": GREEN, "PAUSED": AMBER, "HALTED": RED}


def money(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def status_embed(snap: dict[str, Any]) -> discord.Embed:
    c, p, h = snap["control"], snap["portfolio"], snap["health"]
    e = discord.Embed(
        title=f"{c['state']} · {c['mode'].upper()}",
        color=STATE_COLOR.get(c["state"], GREY),
        description=c.get("reason") or "",
    )
    e.add_field(name="Equity", value=f"${p['equity']:,.2f}")
    e.add_field(name="Today", value=money(p.get("day_pnl")))
    e.add_field(name="Exposure", value=f"${p['gross_exposure']:,.0f} / ${c['limits']['max_gross_exposure_usd']:,.0f}")
    e.add_field(name="Markets today", value=f"{p.get('markets_today', 0)} / {c['limits']['max_new_markets_per_day']}")
    e.add_field(name="Positions", value=str(len(p["positions"])))
    e.add_field(name="Open orders", value=str(len(snap["orders"])))
    feed = "🟢" if h.get("feed_connected") else "🔴"
    e.set_footer(
        text=f"feed {feed} {h.get('subscribed', 0)} books · scan {h.get('scan_ms', 0)}ms · "
        f"LLM {h['llm']['calls']} calls/${h['llm']['usd']}"
    )
    return e


def positions_embed(portfolio: dict[str, Any], limit: int = 10) -> discord.Embed:
    e = discord.Embed(title="Positions", color=BLUE)
    if not portfolio["positions"]:
        e.description = "No open positions."
    for pos in portfolio["positions"][:limit]:
        e.add_field(
            name=f"{pos['question'][:80]}",
            value=f"{pos['outcome']} · {pos['qty']:.0f} @ {pos['avg_cost']:.3f} → {pos['mark']:.3f} "
            f"({money(pos['unrealized'])}) · {pos['strategy']}",
            inline=False,
        )
    return e


def opportunities_embed(opps: list[dict[str, Any]], limit: int = 8) -> discord.Embed:
    e = discord.Embed(title="Top opportunities", color=BLUE)
    if not opps:
        e.description = "Nothing above threshold right now."
    for o in opps[:limit]:
        legs = " + ".join(f"{lg['side']} {lg['outcome']}@{lg['price']:.3f}" for lg in o["legs"])
        llm = ""
        if o.get("llm"):
            llm = f" · LLM {'✅' if o['llm']['enter'] else '⛔'} {o['llm']['c']:.2f}"
        e.add_field(
            name=o["question"][:90],
            value=f"{o['strategy']} · edge {o['edge'] * 100:.1f}¢ · EV ${o['ev_usd']:.2f} · "
            f"APR {o['score'] * 100:.0f}%{llm}\n{legs}",
            inline=False,
        )
    return e


def fill_embed(ev: dict[str, Any]) -> discord.Embed:
    color = GREEN if ev["side"] == "BUY" else AMBER
    e = discord.Embed(
        title=f"Fill · {ev['side']} {ev['size']:.1f} {ev['outcome']} @ {ev['price']:.3f}",
        color=color,
        description=ev["question"][:200],
    )
    e.set_footer(text=f"{ev['strategy']} · {ev['liquidity']} · fee ${ev['fee']:.4f}")
    return e
