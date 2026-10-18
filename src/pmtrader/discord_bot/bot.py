"""Discord bot: notifications + phone-friendly control panel (docs: ARCHITECTURE.md §11).

Only ``discord.owner_user_ids`` may use commands/buttons. Dangerous actions (kill, flatten, resume
from HALTED) require a second confirmation click.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import discord
import structlog
from discord import app_commands

from pmtrader.control.plane import RESUME_CONFIRM, CommandError
from pmtrader.core.events import Alert, FillEvent, PnLReport, TradingStateChanged
from pmtrader.core.types import TradingState
from pmtrader.discord_bot import formatting as fmt

if TYPE_CHECKING:
    from pmtrader.app import Engine

log = structlog.get_logger(__name__)


class ConfirmView(discord.ui.View):
    def __init__(self, bot: TraderBot, action: str) -> None:
        super().__init__(timeout=30)
        self.bot, self.action = bot, action

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await self.bot.authorized(interaction):
            return
        actor = f"discord:{interaction.user}"
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            c = self.bot.engine.control
            if self.action == "kill":
                msg = await c.kill(actor, "kill switch (discord)")
            elif self.action == "kill_flatten":
                msg = await c.kill(actor, "kill + flatten (discord)", flatten=True)
            elif self.action == "flatten":
                msg = await c.flatten(actor)
            elif self.action == "resume":
                msg = await c.resume(actor, RESUME_CONFIRM)
            else:
                msg = "unknown action"
        except CommandError as exc:
            msg = f"⚠️ {exc}"
        await interaction.followup.send(msg, ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


class ControlPanel(discord.ui.View):
    """Persistent view (custom_ids survive restarts)."""

    def __init__(self, bot: TraderBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary, custom_id="pmt:status", row=0)
    async def status(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await self.bot.authorized(interaction):
            await interaction.response.send_message(embed=fmt.status_embed(self.bot.engine.snapshot()), ephemeral=True)

    @discord.ui.button(label="P&L", style=discord.ButtonStyle.secondary, custom_id="pmt:pnl", row=0)
    async def pnl(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await self.bot.authorized(interaction):
            await interaction.response.send_message(await self.bot.pnl_text("today"), ephemeral=True)

    @discord.ui.button(label="Positions", style=discord.ButtonStyle.secondary, custom_id="pmt:positions", row=0)
    async def positions(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await self.bot.authorized(interaction):
            await interaction.response.send_message(
                embed=fmt.positions_embed(self.bot.engine.portfolio.summary()), ephemeral=True
            )

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, custom_id="pmt:pause", row=1)
    async def pause(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await self.bot.authorized(interaction):
            try:
                msg = await self.bot.engine.control.pause(f"discord:{interaction.user}", "paused from Discord")
            except CommandError as exc:
                msg = f"⚠️ {exc}"
            await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.success, custom_id="pmt:resume", row=1)
    async def resume(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await self.bot.authorized(interaction):
            return
        c = self.bot.engine.control
        if c.state is TradingState.HALTED:
            await interaction.response.send_message(
                "System is **HALTED**. Confirm resume?", view=ConfirmView(self.bot, "resume"), ephemeral=True
            )
            return
        try:
            msg = await c.resume(f"discord:{interaction.user}")
        except CommandError as exc:
            msg = f"⚠️ {exc}"
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="KILL", style=discord.ButtonStyle.danger, custom_id="pmt:kill", row=2)
    async def kill(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await self.bot.authorized(interaction):
            await interaction.response.send_message(
                "**Kill switch**: halt + cancel all orders?", view=ConfirmView(self.bot, "kill"), ephemeral=True
            )

    @discord.ui.button(label="Kill + Flatten", style=discord.ButtonStyle.danger, custom_id="pmt:killflat", row=2)
    async def kill_flatten(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await self.bot.authorized(interaction):
            await interaction.response.send_message(
                "**Kill + flatten**: halt, cancel all, and sell every position?",
                view=ConfirmView(self.bot, "kill_flatten"),
                ephemeral=True,
            )


class TraderBot(discord.Client):
    def __init__(self, engine: Engine) -> None:
        super().__init__(intents=discord.Intents.default())
        self.engine = engine
        self.cfg = engine.settings.discord
        self.tree = app_commands.CommandTree(self)
        self._channel: Any = None
        self._register_commands()

    # ------------------------------------------------------------------ auth
    async def authorized(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.cfg.owner_user_ids:
            return True
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        log.warning("discord.unauthorized", user=str(interaction.user), uid=interaction.user.id)
        return False

    async def pnl_text(self, period: str) -> str:
        from pmtrader.portfolio.reports import render_text

        return render_text(await self.engine.portfolio.build_report(period))

    # ------------------------------------------------------------------ commands
    def _register_commands(self) -> None:
        tree, bot = self.tree, self

        @tree.command(name="status", description="Trading state, equity, today's P&L")
        async def status(interaction: discord.Interaction) -> None:
            if await bot.authorized(interaction):
                await interaction.response.send_message(embed=fmt.status_embed(bot.engine.snapshot()), ephemeral=True)

        @tree.command(name="pnl", description="P&L summary")
        @app_commands.choices(period=[app_commands.Choice(name=p, value=p) for p in ("today", "7d", "all")])
        async def pnl(interaction: discord.Interaction, period: str = "today") -> None:
            if await bot.authorized(interaction):
                await interaction.response.send_message(await bot.pnl_text(period), ephemeral=True)

        @tree.command(name="top", description="Top scanner opportunities")
        async def top(interaction: discord.Interaction, n: int = 8) -> None:
            if await bot.authorized(interaction):
                await interaction.response.send_message(
                    embed=fmt.opportunities_embed(bot.engine.opportunities_view(n), n), ephemeral=True
                )

        @tree.command(name="positions", description="Open positions")
        async def positions(interaction: discord.Interaction) -> None:
            if await bot.authorized(interaction):
                await interaction.response.send_message(
                    embed=fmt.positions_embed(bot.engine.portfolio.summary()), ephemeral=True
                )

        @tree.command(name="pause", description="Stop new entries (exits keep working)")
        async def pause(interaction: discord.Interaction) -> None:
            if await bot.authorized(interaction):
                msg = await bot.engine.control.pause(f"discord:{interaction.user}", "paused from Discord")
                await interaction.response.send_message(msg, ephemeral=True)

        @tree.command(name="resume", description="Resume trading")
        async def resume(interaction: discord.Interaction) -> None:
            if not await bot.authorized(interaction):
                return
            if bot.engine.control.state is TradingState.HALTED:
                await interaction.response.send_message(
                    "System is HALTED. Confirm resume?", view=ConfirmView(bot, "resume"), ephemeral=True
                )
                return
            await interaction.response.send_message(
                await bot.engine.control.resume(f"discord:{interaction.user}"), ephemeral=True
            )

        @tree.command(name="kill", description="KILL SWITCH: halt and cancel all orders")
        async def kill(interaction: discord.Interaction, flatten: bool = False) -> None:
            if await bot.authorized(interaction):
                await interaction.response.send_message(
                    "Confirm kill" + (" + flatten" if flatten else "") + "?",
                    view=ConfirmView(bot, "kill_flatten" if flatten else "kill"),
                    ephemeral=True,
                )

        @tree.command(name="flatten", description="Sell all positions (reduce-only, slippage-bounded)")
        async def flatten(interaction: discord.Interaction) -> None:
            if await bot.authorized(interaction):
                await interaction.response.send_message(
                    "Confirm flatten?", view=ConfirmView(bot, "flatten"), ephemeral=True
                )

        @tree.command(name="limits", description="Show risk limits")
        async def limits(interaction: discord.Interaction) -> None:
            if await bot.authorized(interaction):
                lim = bot.engine.risk.limits.model_dump()
                lines = [f"`{k}` = {v}" for k, v in lim.items() if not isinstance(v, list)]
                await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @tree.command(name="setlimit", description="Change a risk limit (validated against hard ceilings)")
        async def setlimit(interaction: discord.Interaction, name: str, value: float) -> None:
            if not await bot.authorized(interaction):
                return
            current = bot.engine.risk.limits.model_dump().get(name)
            v: float | int = int(value) if isinstance(current, int) and not isinstance(current, bool) else value
            try:
                applied = await bot.engine.control.set_limits(f"discord:{interaction.user}", {name: v})
                msg = f"✅ {applied}"
            except CommandError as exc:
                msg = f"⚠️ {exc}"
            await interaction.response.send_message(msg, ephemeral=True)

        @tree.command(name="strategy", description="Enable/disable auto-trading for a strategy")
        async def strategy(interaction: discord.Interaction, name: str, auto: bool) -> None:
            if not await bot.authorized(interaction):
                return
            try:
                await bot.engine.control.set_strategy_auto(f"discord:{interaction.user}", name, auto)
                msg = f"✅ {name} auto={auto}"
            except CommandError as exc:
                msg = f"⚠️ {exc}"
            await interaction.response.send_message(msg, ephemeral=True)

        @tree.command(name="panel", description="Post the control panel in this channel")
        async def panel(interaction: discord.Interaction) -> None:
            if await bot.authorized(interaction):
                await interaction.response.send_message(
                    embed=fmt.status_embed(bot.engine.snapshot()), view=ControlPanel(bot)
                )

    # ------------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        self.add_view(ControlPanel(self))
        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.loop.create_task(self._notifier())

    async def on_ready(self) -> None:
        log.info("discord.ready", user=str(self.user))
        if self.cfg.channel_id:
            self._channel = self.get_channel(self.cfg.channel_id) or await self.fetch_channel(self.cfg.channel_id)

    async def run_bot(self) -> None:
        token = self.engine.settings.secrets.discord_token
        assert token is not None
        await self.start(token.get_secret_value())

    async def _send(self, **kwargs: Any) -> None:
        if self._channel is None:
            return
        try:
            await self._channel.send(**kwargs)
        except discord.HTTPException as exc:
            log.warning("discord.send_failed", error=repr(exc))

    async def _notifier(self) -> None:
        await self.wait_until_ready()
        sub = self.engine.bus.subscribe(FillEvent, Alert, TradingStateChanged, PnLReport, maxsize=1000, name="discord")
        try:
            async for ev in sub:
                if isinstance(ev, FillEvent) and self.cfg.notify_fills:
                    f = ev.fill
                    m = self.engine.catalog.get(f.market_id)
                    oc = m.outcome_for(f.asset_id) if m else None
                    await self._send(
                        embed=fmt.fill_embed(
                            {
                                "side": f.side.value,
                                "size": f.size,
                                "price": f.price,
                                "fee": f.fee,
                                "outcome": oc.name if oc else "",
                                "question": m.question if m else f.market_id,
                                "strategy": f.strategy,
                                "liquidity": f.liquidity.value,
                            }
                        )
                    )
                elif isinstance(ev, TradingStateChanged):
                    color = fmt.STATE_COLOR.get(ev.state.value, fmt.GREY)
                    await self._send(
                        embed=discord.Embed(
                            title=f"{ev.previous.value} → {ev.state.value}",
                            description=f"{ev.reason} (by {ev.actor})",
                            color=color,
                        )
                    )
                elif isinstance(ev, Alert) and ev.level in ("warning", "critical"):
                    color = fmt.RED if ev.level == "critical" else fmt.AMBER
                    await self._send(embed=discord.Embed(title=ev.title, description=ev.message[:1500], color=color))
                elif isinstance(ev, PnLReport):
                    await self._send(content=ev.text[:1900])
                await asyncio.sleep(0.25)  # stay well under Discord rate limits
        finally:
            sub.close()
