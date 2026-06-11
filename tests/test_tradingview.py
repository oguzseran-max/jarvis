"""Unit tests for tradingview_access — offline only, no network calls."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tradingview_access import (
    _spoken_change,
    _spoken_price,
    format_quote_for_voice,
    parse_quote_payload,
    resolve_alias,
    resolve_chart_url,
    search_symbol,
)


def test_alias_resolves_indices_and_crypto():
    assert resolve_alias("bitcoin") == "BITSTAMP:BTCUSD"
    assert resolve_alias("S&P 500") == "SP:SPX"
    assert resolve_alias("the cac 40".replace("the ", "")) == "EURONEXT:PX1"
    assert resolve_alias("Dow Jones") == "TVC:DJI"


def test_alias_strips_trailing_punctuation():
    assert resolve_alias("bitcoin?") == "BITSTAMP:BTCUSD"


def test_alias_unknown_returns_none():
    assert resolve_alias("some obscure penny stock") is None


def test_search_symbol_alias_path_is_offline():
    # Aliases must resolve without any network call
    match = asyncio.run(search_symbol("bitcoin"))
    assert match["symbol"] == "BITSTAMP:BTCUSD"
    assert match["type"] == "alias"


def test_parse_quote_payload_normalizes_fields():
    quote = parse_quote_payload("NASDAQ:AAPL", {
        "close": 212.34, "change": 1.23, "change_abs": 2.58,
        "high": 214.0, "low": 209.9, "volume": 51000000,
        "currency_code": "USD",
    })
    assert quote["price"] == 212.34
    assert quote["change_pct"] == 1.23
    assert quote["currency"] == "USD"


def test_parse_quote_payload_rejects_empty():
    assert parse_quote_payload("NASDAQ:AAPL", {}) is None
    assert parse_quote_payload("NASDAQ:AAPL", {"close": None}) is None


def test_spoken_price_formats():
    assert _spoken_price(212.34, "USD") == "212.34 dollars"
    assert _spoken_price(43250.0, "USD") == "43,250 dollars"
    assert _spoken_price(0.5230, "USD") == "0.523 dollars"
    assert _spoken_price(150.0, "EUR") == "150 euros"
    assert _spoken_price(99.5, "SEK") == "99.5 SEK"
    assert _spoken_price(6100.0, "") == "6,100"


def test_spoken_change_directions():
    assert _spoken_change(1.23) == "up 1.2 percent"
    assert _spoken_change(-0.8) == "down 0.8 percent"
    assert _spoken_change(0.01) == "flat"
    assert _spoken_change(None) == "unchanged"


def test_format_quote_stock_uses_currency():
    text = format_quote_for_voice({
        "symbol": "NASDAQ:AAPL", "description": "Apple Inc.",
        "price": 212.34, "change_pct": 1.23, "currency": "USD", "type": "stock",
    })
    assert text == "Apple Inc. is at 212.34 dollars, up 1.2 percent on the day, sir."


def test_format_quote_index_skips_currency():
    text = format_quote_for_voice({
        "symbol": "SP:SPX", "description": "S&P 500",
        "price": 6100.0, "change_pct": -0.4, "currency": "USD", "type": "alias",
    })
    assert "dollars" not in text
    assert "down 0.4 percent" in text


def test_chart_url_for_alias_is_offline():
    url = asyncio.run(resolve_chart_url("bitcoin"))
    assert url == "https://www.tradingview.com/chart/?symbol=BITSTAMP%3ABTCUSD"
