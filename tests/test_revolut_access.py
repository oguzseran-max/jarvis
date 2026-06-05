"""Unit tests for revolut_access — read-only Revolut portfolio view.

No network: crypto parsing is tested against canned payloads, signing is
verified against the public key, and stock reading uses a temp file.
"""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import revolut_access as ra
from revolut_access import (
    Holding,
    Portfolio,
    format_portfolio_for_context,
    format_portfolio_summary,
    get_stock_holdings,
    parse_balances,
)


# --- parse_balances --------------------------------------------------------

def test_parse_balances_sums_available_staked_reserved():
    raw = [{"currency": "BTC", "available": "0.5", "staked": "0.1", "reserved": "0.0"}]
    holdings = parse_balances(raw)
    assert len(holdings) == 1
    assert holdings[0].symbol == "BTC"
    assert abs(holdings[0].quantity - 0.6) < 1e-9
    assert holdings[0].kind == "crypto"


def test_parse_balances_skips_zero_balances():
    raw = [
        {"currency": "ETH", "available": "0", "staked": "0", "reserved": "0"},
        {"currency": "SOL", "available": "3", "staked": "0", "reserved": "0"},
    ]
    holdings = parse_balances(raw)
    assert [h.symbol for h in holdings] == ["SOL"]


def test_parse_balances_accepts_dict_wrapper_and_alias_fields():
    raw = {"balances": [{"asset": "ada", "available": "100"}]}
    holdings = parse_balances(raw)
    assert holdings[0].symbol == "ADA"
    assert holdings[0].quantity == 100.0


def test_parse_balances_handles_garbage():
    assert parse_balances("nonsense") == []
    assert parse_balances([{"available": "1"}]) == []  # no symbol -> skipped


# --- get_stock_holdings ----------------------------------------------------

def test_get_stock_holdings_reads_file(tmp_path, monkeypatch):
    f = tmp_path / "stocks.json"
    f.write_text(json.dumps([
        {"symbol": "AAPL", "quantity": 10, "value": 1850.0, "currency": "USD"},
        {"ticker": "vwrp", "shares": 25, "amount": 3120.5, "currency": "gbp"},
    ]))
    monkeypatch.setattr(ra, "STOCKS_FILE", str(f))

    holdings, err = get_stock_holdings()
    assert err is None
    assert {h.symbol for h in holdings} == {"AAPL", "VWRP"}
    aapl = next(h for h in holdings if h.symbol == "AAPL")
    assert aapl.quantity == 10 and aapl.value == 1850.0 and aapl.kind == "stock"
    vwrp = next(h for h in holdings if h.symbol == "VWRP")
    assert vwrp.quantity == 25 and vwrp.currency == "GBP"


def test_get_stock_holdings_missing_file_is_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "STOCKS_FILE", str(tmp_path / "nope.json"))
    holdings, err = get_stock_holdings()
    assert holdings == [] and err is None


def test_get_stock_holdings_bad_json_reports_error(tmp_path, monkeypatch):
    f = tmp_path / "stocks.json"
    f.write_text("{ not valid json")
    monkeypatch.setattr(ra, "STOCKS_FILE", str(f))
    holdings, err = get_stock_holdings()
    assert holdings == [] and err is not None


# --- Portfolio aggregation -------------------------------------------------

def test_portfolio_total_value_sums_known_only():
    p = Portfolio(holdings=[
        Holding("BTC", 0.5, value=None, kind="crypto"),
        Holding("AAPL", 10, value=1850.0, kind="stock"),
        Holding("TSLA", 4, value=980.0, kind="stock"),
    ])
    assert p.total_value() == 2830.0
    assert [h.symbol for h in p.crypto] == ["BTC"]
    assert [h.symbol for h in p.stocks] == ["AAPL", "TSLA"]


def test_portfolio_total_value_none_when_unpriced():
    p = Portfolio(holdings=[Holding("BTC", 0.5, value=None)])
    assert p.total_value() is None


# --- Formatting ------------------------------------------------------------

def test_summary_unconfigured_is_helpful():
    msg = format_portfolio_summary(Portfolio())
    assert "configured" in msg.lower()


def test_summary_with_total_value():
    p = Portfolio(holdings=[Holding("AAPL", 10, value=1850.0, kind="stock")])
    msg = format_portfolio_summary(p)
    assert "$1,850.00" in msg and "1 stock position" in msg


def test_summary_without_values_names_symbols():
    p = Portfolio(holdings=[Holding("BTC", 0.5, value=None), Holding("ETH", 2, value=None)])
    msg = format_portfolio_summary(p)
    assert "BTC" in msg and "2 crypto positions" in msg


def test_context_format_lists_sections():
    p = Portfolio(holdings=[
        Holding("BTC", 0.5, value=None, kind="crypto"),
        Holding("AAPL", 10, value=1850.0, kind="stock"),
    ])
    ctx = format_portfolio_for_context(p)
    assert "Crypto" in ctx and "BTC" in ctx
    assert "Stocks" in ctx and "AAPL" in ctx


def test_context_format_trims_trailing_zeros():
    p = Portfolio(holdings=[Holding("BTC", 0.5, value=None, kind="crypto")])
    ctx = format_portfolio_for_context(p)
    assert "0.5" in ctx and "0.50000000" not in ctx


# --- Signing ---------------------------------------------------------------

def test_sign_produces_verifiable_ed25519_signature(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(pem)
    monkeypatch.setattr(ra, "REVX_PRIVATE_KEY_PATH", str(key_file))

    timestamp, method, path = "1765360896219", "GET", "/api/1.0/balances"
    sig_b64 = ra._sign(timestamp, method, path)

    # The canonical message must be timestamp+METHOD+path+query+body, no separators.
    expected_message = f"{timestamp}{method}{path}".encode("utf-8")
    public_key = private_key.public_key()
    public_key.verify(base64.b64decode(sig_b64), expected_message)  # raises if invalid


def test_is_crypto_configured_false_without_keys(monkeypatch):
    monkeypatch.setattr(ra, "REVX_API_KEY", "")
    monkeypatch.setattr(ra, "REVX_PRIVATE_KEY_PATH", "")
    assert ra.is_crypto_configured() is False
