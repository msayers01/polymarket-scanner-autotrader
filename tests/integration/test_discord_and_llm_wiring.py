"""Construction-level checks for the optional integrations (no network, no tokens)."""

from pmtrader.app import Engine
from pmtrader.config import LLMConfig, LLMProviderConfig, Settings
from pmtrader.core.types import Mode
from pmtrader.llm.gate import build_providers


async def test_discord_bot_registers_commands(tmp_path):
    from pmtrader.discord_bot.bot import ControlPanel, TraderBot

    engine = Engine(Settings(mode=Mode.PAPER, data_dir=tmp_path, log_dir=tmp_path))
    bot = TraderBot(engine)
    names = {c.name for c in bot.tree.get_commands()}
    assert {
        "status",
        "pnl",
        "top",
        "positions",
        "pause",
        "resume",
        "kill",
        "flatten",
        "limits",
        "setlimit",
        "strategy",
        "panel",
    } <= names
    panel = ControlPanel(bot)
    assert {c.custom_id for c in panel.children} >= {"pmt:pause", "pmt:resume", "pmt:kill", "pmt:status"}
    assert panel.is_persistent()
    await bot.close()
    await engine.http.aclose()


async def test_llm_providers_build():
    cfg = LLMConfig(
        providers=[
            LLMProviderConfig(
                name="qwen", kind="openai_compat", model="qwen3:14b", base_url="http://localhost:11434/v1"
            ),
            LLMProviderConfig(name="claude", kind="anthropic", model="claude-opus-5-5", api_key_env="NOPE_NOT_SET"),
        ]
    )
    providers = build_providers(cfg)
    assert [p.name for p in providers] == ["qwen", "claude"]
    for p in providers:
        await p.close()
