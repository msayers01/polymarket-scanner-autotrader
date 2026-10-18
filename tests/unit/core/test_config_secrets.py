from __future__ import annotations

import pytest

from pmtrader.config import Secrets


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_secrets_are_unset(blank: str) -> None:
    s = Secrets(web_token=blank, discord_token=blank, live_confirm=blank, polymarket_wallet=blank)
    assert s.web_token is None
    assert s.discord_token is None
    assert s.live_confirm is None
    assert s.polymarket_wallet is None


def test_real_secrets_are_kept() -> None:
    s = Secrets(web_token="s3cret", live_confirm="I_ACCEPT_RISK")
    assert s.web_token is not None and s.web_token.get_secret_value() == "s3cret"
    assert s.live_confirm == "I_ACCEPT_RISK"
