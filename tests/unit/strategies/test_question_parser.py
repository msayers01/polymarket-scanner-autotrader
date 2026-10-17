import pytest

from pmtrader.marketdata.question_parser import parse_crypto_question as p


@pytest.mark.parametrize(
    ("q", "kind", "asset", "strike", "hi", "window"),
    [
        ("Will the price of Bitcoin be above $74,000 on October 18?", "above", "bitcoin", 74000, None, None),
        ("Will the price of Ethereum be less than $2,100 on October 18?", "below", "ethereum", 2100, None, None),
        ("Will the price of Bitcoin be greater than $92,000 on October 18?", "above", "bitcoin", 92000, None, None),
        (
            "Will the price of Bitcoin be between $74,000 and $76,000 on October 18?",
            "between",
            "bitcoin",
            74000,
            76000,
            None,
        ),
        ("Will Bitcoin reach $150,000 in October?", "touch_up", "bitcoin", 150000, None, None),
        ("Will Ethereum reach $3,200 October 12-18?", "touch_up", "ethereum", 3200, None, None),
        ("Will Bitcoin dip to $60,000 by December 31, 2026?", "touch_down", "bitcoin", 60000, None, None),
        ("Will XRP dip to $1.00 by December 31, 2026?", "touch_down", "xrp", 1.0, None, None),
        ("Bitcoin Up or Down - October 17, 6:15PM-6:30PM ET", "updown", "bitcoin", None, None, 900),
        ("Bitcoin Up or Down - October 17, 4:00PM-8:00PM ET", "updown", "bitcoin", None, None, 4 * 3600),
        ("Bitcoin Up or Down - October 17, 6PM ET", "updown", "bitcoin", None, None, 3600),
        ("Ethereum Up or Down on October 18?", "updown", "ethereum", None, None, 86400),
        ("Will Trump win the 2028 election?", None, None, None, None, None),
        ("Predict.fun FDV above $50M one day after launch?", None, None, None, None, None),
    ],
)
def test_templates(q, kind, asset, strike, hi, window):
    c = p(q)
    if kind is None:
        assert c is None
        return
    assert c is not None
    assert (c.kind, c.asset, c.strike, c.strike_hi, c.window_seconds) == (kind, asset, strike, hi, window)
