"""
JARVIS Revolut Access — READ-ONLY portfolio view.

JARVIS can look at your Revolut investments, but it can never move money or
place trades. Two sources, both strictly read-only:

  • Crypto — official Revolut X REST API. Requests are signed with an Ed25519
             key pair (you keep the private key; Revolut holds the public key).
             Only the balances endpoint is ever called.

  • Stocks — Revolut Invest (shares/ETFs) has NO public API, so holdings are
             read from a local JSON file you maintain or export. JARVIS only
             reads it; it never logs into your account.

Safety by design (mirrors mail_access.py): no order placement, no withdrawals,
no transfers — balance reads only.

Configuration (see .env.example):
  REVOLUT_X_API_KEY       — API key from the Revolut X web app
  REVOLUT_X_PRIVATE_KEY   — path to your Ed25519 private key (PEM)
  REVOLUT_STOCKS_FILE     — path to your stocks JSON (defaults to data/revolut_stocks.json)
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger("jarvis.revolut")

# Revolut X (crypto) — official API
REVX_BASE_URL = os.getenv("REVOLUT_X_BASE_URL", "https://revx.revolut.com").rstrip("/")
REVX_API_KEY = os.getenv("REVOLUT_X_API_KEY", "").strip()
REVX_PRIVATE_KEY_PATH = os.getenv("REVOLUT_X_PRIVATE_KEY", "").strip()
REVX_BALANCES_PATH = "/api/1.0/balances"

# Stocks (Revolut Invest) — manual file, no official API
STOCKS_FILE = os.getenv(
    "REVOLUT_STOCKS_FILE",
    str(Path(__file__).parent / "data" / "revolut_stocks.json"),
)

TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Holding:
    """A single position. `value` is fiat worth when known, else None."""
    symbol: str
    quantity: float
    value: float | None = None
    currency: str = "USD"
    kind: str = "crypto"  # "crypto" | "stock"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "value": self.value,
            "currency": self.currency,
            "kind": self.kind,
        }


@dataclass
class Portfolio:
    holdings: list[Holding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def crypto(self) -> list[Holding]:
        return [h for h in self.holdings if h.kind == "crypto"]

    @property
    def stocks(self) -> list[Holding]:
        return [h for h in self.holdings if h.kind == "stock"]

    def total_value(self) -> float | None:
        """Sum of known fiat values, or None if nothing is priced."""
        priced = [h.value for h in self.holdings if h.value is not None]
        return round(sum(priced), 2) if priced else None

    def to_dict(self) -> dict:
        return {
            "holdings": [h.to_dict() for h in self.holdings],
            "errors": self.errors,
            "total_value": self.total_value(),
        }


# ---------------------------------------------------------------------------
# Revolut X request signing (Ed25519)
# ---------------------------------------------------------------------------
#
# Canonical string to sign (no separators between fields), per Revolut X docs:
#   timestamp + METHOD + path + query + body
# where timestamp matches the X-Revx-Timestamp header (milliseconds), path
# starts at /api, query has no leading '?', and body is the minified JSON
# (empty for GET). The signature is base64-encoded in X-Revx-Signature.

def is_crypto_configured() -> bool:
    """True when both the API key and a readable private key are present."""
    return bool(REVX_API_KEY and REVX_PRIVATE_KEY_PATH and Path(REVX_PRIVATE_KEY_PATH).is_file())


def _load_private_key():
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    pem = Path(REVX_PRIVATE_KEY_PATH).read_bytes()
    return load_pem_private_key(pem, password=None)


def _sign(timestamp: str, method: str, path: str, query: str = "", body: str = "") -> str:
    """Return the base64 Ed25519 signature of the canonical request string."""
    import base64

    message = f"{timestamp}{method.upper()}{path}{query}{body}".encode("utf-8")
    signature = _load_private_key().sign(message)
    return base64.b64encode(signature).decode("ascii")


def _signed_headers(method: str, path: str, query: str = "", body: str = "") -> dict:
    timestamp = str(int(time.time() * 1000))
    return {
        "X-Revx-API-Key": REVX_API_KEY,
        "X-Revx-Timestamp": timestamp,
        "X-Revx-Signature": _sign(timestamp, method, path, query, body),
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Crypto holdings (Revolut X)
# ---------------------------------------------------------------------------

def _coerce_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_balances(raw) -> list[Holding]:
    """Parse the balances payload into Holdings. Defensive about field names.

    Quantity for a position is available + staked + reserved. Amounts arrive as
    strings (to avoid float rounding) — we sum them as floats for display only.
    Zero-balance assets are skipped.
    """
    items = raw.get("balances", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []

    holdings: list[Holding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = item.get("currency") or item.get("asset") or item.get("symbol") or ""
        if not symbol:
            continue
        qty = (
            _coerce_float(item.get("available"))
            + _coerce_float(item.get("staked"))
            + _coerce_float(item.get("reserved"))
        )
        if qty <= 0:
            continue
        holdings.append(Holding(symbol=str(symbol).upper(), quantity=qty, kind="crypto"))
    return holdings


async def get_crypto_holdings() -> tuple[list[Holding], str | None]:
    """Fetch crypto balances from Revolut X. Returns (holdings, error_or_None).

    Never raises — degrades gracefully when unconfigured or the API is down.
    """
    if not is_crypto_configured():
        return [], None  # Not configured is not an error — crypto is optional.

    try:
        headers = _signed_headers("GET", REVX_BALANCES_PATH)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(f"{REVX_BASE_URL}{REVX_BALANCES_PATH}", headers=headers)
        if resp.status_code != 200:
            msg = f"Revolut X returned {resp.status_code}"
            log.warning(f"{msg}: {resp.text[:200]}")
            return [], msg
        return parse_balances(resp.json()), None
    except Exception as e:
        log.warning(f"Revolut X balance fetch failed: {e}")
        return [], f"crypto unavailable ({type(e).__name__})"


# ---------------------------------------------------------------------------
# Stock holdings (manual file — Revolut Invest has no public API)
# ---------------------------------------------------------------------------

def get_stock_holdings() -> tuple[list[Holding], str | None]:
    """Read share/ETF holdings from the local JSON file.

    Expected format — a list of objects, e.g.:
        [{"symbol": "AAPL", "quantity": 10, "value": 1850.0, "currency": "USD"}]
    `quantity`/`shares` and `value`/`amount` aliases are accepted. Returns
    (holdings, error_or_None); a missing file is not an error.
    """
    path = Path(STOCKS_FILE)
    if not path.is_file():
        return [], None

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Could not read stocks file {path}: {e}")
        return [], f"stocks file unreadable ({type(e).__name__})"

    rows = data.get("holdings", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return [], "stocks file must contain a list of holdings"

    holdings: list[Holding] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol") or row.get("ticker") or ""
        if not symbol:
            continue
        qty = _coerce_float(row.get("quantity", row.get("shares", 0)))
        raw_value = row.get("value", row.get("amount"))
        value = _coerce_float(raw_value) if raw_value is not None else None
        holdings.append(Holding(
            symbol=str(symbol).upper(),
            quantity=qty,
            value=value,
            currency=str(row.get("currency", "USD")).upper(),
            kind="stock",
        ))
    return holdings, None


# ---------------------------------------------------------------------------
# Combined portfolio
# ---------------------------------------------------------------------------

async def get_portfolio() -> Portfolio:
    """Combined read-only view of crypto + stock holdings."""
    crypto, crypto_err = await get_crypto_holdings()
    stocks, stock_err = get_stock_holdings()

    portfolio = Portfolio(holdings=[*crypto, *stocks])
    if crypto_err:
        portfolio.errors.append(crypto_err)
    if stock_err:
        portfolio.errors.append(stock_err)
    return portfolio


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_qty(q: float) -> str:
    """Trim trailing zeros — 0.50000000 -> 0.5, 10.0 -> 10."""
    return f"{q:.8f}".rstrip("0").rstrip(".")


def _fmt_money(value: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency.upper(), "")
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency}"


def format_portfolio_for_context(portfolio: Portfolio) -> str:
    """Detailed text block for the LLM context window."""
    if not portfolio.holdings:
        if portfolio.errors:
            return "Revolut: could not read investments (" + "; ".join(portfolio.errors) + ")."
        return "Revolut: no investments configured."

    lines: list[str] = []
    if portfolio.crypto:
        lines.append("Crypto (Revolut X):")
        for h in portfolio.crypto:
            entry = f"  {h.symbol}: {_fmt_qty(h.quantity)}"
            if h.value is not None:
                entry += f" (~{_fmt_money(h.value, h.currency)})"
            lines.append(entry)
    if portfolio.stocks:
        lines.append("Stocks/ETFs (Revolut Invest):")
        for h in portfolio.stocks:
            entry = f"  {h.symbol}: {_fmt_qty(h.quantity)} shares"
            if h.value is not None:
                entry += f" (~{_fmt_money(h.value, h.currency)})"
            lines.append(entry)

    total = portfolio.total_value()
    if total is not None:
        lines.append(f"Known total value: {_fmt_money(total)}")
    if portfolio.errors:
        lines.append("Note: " + "; ".join(portfolio.errors))
    return "\n".join(lines)


def format_portfolio_summary(portfolio: Portfolio) -> str:
    """Brief, voice-friendly summary for JARVIS to speak (1-2 sentences)."""
    if not portfolio.holdings:
        if portfolio.errors:
            return "I couldn't reach your Revolut investments just now, sir."
        return (
            "No Revolut investments are configured yet, sir. "
            "Add your API key for crypto, or a holdings file for stocks."
        )

    n_crypto = len(portfolio.crypto)
    n_stocks = len(portfolio.stocks)
    parts: list[str] = []
    if n_crypto:
        parts.append(f"{n_crypto} crypto position{'s' if n_crypto != 1 else ''}")
    if n_stocks:
        parts.append(f"{n_stocks} stock position{'s' if n_stocks != 1 else ''}")
    holdings_phrase = " and ".join(parts)

    total = portfolio.total_value()
    if total is not None:
        return f"You're holding {holdings_phrase}, sir, worth about {_fmt_money(total)}."

    # No fiat values — name the largest few symbols instead.
    symbols = [h.symbol for h in portfolio.holdings[:3]]
    named = ", ".join(symbols)
    tail = ", among others" if len(portfolio.holdings) > 3 else ""
    return f"You're holding {holdings_phrase}, sir — {named}{tail}."
