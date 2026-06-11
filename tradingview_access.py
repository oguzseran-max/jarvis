"""
JARVIS TradingView Access — live market data via TradingView's public endpoints.

No account or API key required: uses the unauthenticated symbol-search and
scanner endpoints that power tradingview.com itself. Quotes are cached
briefly so repeated voice queries don't hammer the API.
"""

import asyncio
import logging
import time as _time
from urllib.parse import quote as _urlquote

import httpx

log = logging.getLogger("jarvis.tradingview")

_SEARCH_URL = "https://symbol-search.tradingview.com/symbol_search/"
_SCANNER_URL = "https://scanner.tradingview.com/symbol"
_CHART_URL = "https://www.tradingview.com/chart/?symbol={symbol}"

# TradingView blocks requests without browser-like headers
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}

_QUOTE_FIELDS = "close,change,change_abs,high,low,volume,currency_code"

# Spoken names that symbol search resolves poorly — map straight to tickers.
# Voice queries arrive lowercased and fuzzy ("the s and p", "bitcoin").
SYMBOL_ALIASES: dict[str, str] = {
    "s&p": "SP:SPX",
    "s&p 500": "SP:SPX",
    "sp 500": "SP:SPX",
    "s and p": "SP:SPX",
    "s and p 500": "SP:SPX",
    "spx": "SP:SPX",
    "nasdaq": "NASDAQ:IXIC",
    "dow": "TVC:DJI",
    "dow jones": "TVC:DJI",
    "cac": "EURONEXT:PX1",
    "cac 40": "EURONEXT:PX1",
    "cac40": "EURONEXT:PX1",
    "dax": "XETR:DAX",
    "ftse": "TVC:UKX",
    "bitcoin": "BITSTAMP:BTCUSD",
    "btc": "BITSTAMP:BTCUSD",
    "ethereum": "BITSTAMP:ETHUSD",
    "eth": "BITSTAMP:ETHUSD",
    "gold": "TVC:GOLD",
    "oil": "TVC:USOIL",
    "crude oil": "TVC:USOIL",
}

# Symbols quoted in points, not currency
_INDEX_SYMBOLS = {
    "SP:SPX", "NASDAQ:IXIC", "TVC:DJI", "EURONEXT:PX1", "XETR:DAX", "TVC:UKX",
}

# Indices shown in the market overview, in speaking order
_OVERVIEW_SYMBOLS = [
    ("the S&P 500", "SP:SPX"),
    ("the Nasdaq", "NASDAQ:IXIC"),
    ("the Dow", "TVC:DJI"),
    ("Bitcoin", "BITSTAMP:BTCUSD"),
]

_CURRENCY_WORDS = {
    "USD": "dollars",
    "EUR": "euros",
    "GBP": "pounds",
    "JPY": "yen",
    "CHF": "francs",
}

# Quote cache: full symbol -> (fetched_at, quote dict)
_quote_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 60.0


def resolve_alias(query: str) -> str | None:
    """Map a spoken name to a full TradingView symbol, or None if unknown."""
    return SYMBOL_ALIASES.get(query.lower().strip().rstrip("?.!,"))


async def search_symbol(query: str) -> dict | None:
    """Resolve a company name or ticker to a TradingView symbol.

    Returns {"symbol": "NASDAQ:AAPL", "description": "Apple Inc.", "type": "stock"}
    or None if nothing matched.
    """
    alias = resolve_alias(query)
    if alias:
        return {"symbol": alias, "description": query.strip().title(), "type": "alias"}

    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=10) as client:
            resp = await client.get(
                _SEARCH_URL,
                params={"text": query, "hl": "0", "lang": "en", "domain": "production"},
            )
            resp.raise_for_status()
            results = resp.json()
    except Exception as e:
        log.warning(f"Symbol search failed for '{query}': {e}")
        return None

    if not results:
        return None

    best = results[0]
    symbol = best.get("symbol", "")
    exchange = best.get("exchange", "")
    if not symbol:
        return None
    full = f"{exchange}:{symbol}" if exchange and ":" not in symbol else symbol
    return {
        "symbol": full,
        "description": best.get("description", symbol),
        "type": best.get("type", ""),
    }


async def fetch_quote(full_symbol: str) -> dict | None:
    """Fetch a quote from the scanner endpoint. Returns cached data within TTL."""
    cached = _quote_cache.get(full_symbol)
    if cached and _time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=10) as client:
            resp = await client.get(
                _SCANNER_URL,
                params={"symbol": full_symbol, "fields": _QUOTE_FIELDS, "no_404": "true"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.warning(f"Quote fetch failed for {full_symbol}: {e}")
        return None

    quote = parse_quote_payload(full_symbol, data)
    if quote:
        _quote_cache[full_symbol] = (_time.time(), quote)
    return quote


def parse_quote_payload(full_symbol: str, data: dict) -> dict | None:
    """Normalize a scanner payload into a quote dict."""
    if not isinstance(data, dict) or data.get("close") is None:
        return None
    return {
        "symbol": full_symbol,
        "price": data["close"],
        "change_pct": data.get("change"),
        "change_abs": data.get("change_abs"),
        "high": data.get("high"),
        "low": data.get("low"),
        "volume": data.get("volume"),
        "currency": data.get("currency_code") or "",
    }


async def get_quote(query: str) -> dict | None:
    """Resolve a name/ticker and fetch its quote. Returns quote + description."""
    match = await search_symbol(query)
    if not match:
        return None
    quote = await fetch_quote(match["symbol"])
    if not quote:
        return None
    quote["description"] = match["description"]
    quote["type"] = match.get("type", "")
    return quote


def _spoken_price(price: float, currency: str) -> str:
    """Format a price for TTS: '212.34 dollars', '43,250 dollars' for big numbers."""
    if price >= 1000:
        num = f"{price:,.0f}"
    elif price >= 1:
        num = f"{price:,.2f}".rstrip("0").rstrip(".")
    else:
        num = f"{price:.4f}".rstrip("0").rstrip(".")
    word = _CURRENCY_WORDS.get(currency.upper())
    if word:
        return f"{num} {word}"
    if currency:
        return f"{num} {currency.upper()}"
    return num


def _spoken_change(change_pct: float | None) -> str:
    """Format a percent change for TTS: 'up 1.2 percent' / 'flat'."""
    if change_pct is None:
        return "unchanged"
    if abs(change_pct) < 0.05:
        return "flat"
    direction = "up" if change_pct > 0 else "down"
    return f"{direction} {abs(change_pct):.1f} percent"


def format_quote_for_voice(quote: dict) -> str:
    """One-sentence voice summary of a quote, JARVIS style."""
    name = quote.get("description") or quote["symbol"]
    is_index = quote.get("type") == "index" or quote["symbol"] in _INDEX_SYMBOLS
    price = _spoken_price(quote["price"], "" if is_index else quote.get("currency", ""))
    change = _spoken_change(quote.get("change_pct"))
    return f"{name} is at {price}, {change} on the day, sir."


async def get_quote_summary(query: str) -> str:
    """Voice-ready quote lookup for a single symbol or company name."""
    quote = await get_quote(query)
    if not quote:
        return f"Couldn't find market data for {query}, sir. TradingView may be unreachable."
    return format_quote_for_voice(quote)


async def get_market_overview() -> str:
    """Voice-ready summary of the major indices plus Bitcoin."""
    results = await asyncio.gather(
        *[fetch_quote(sym) for _, sym in _OVERVIEW_SYMBOLS],
        return_exceptions=True,
    )
    parts = []
    for (name, _), quote in zip(_OVERVIEW_SYMBOLS, results):
        if isinstance(quote, dict) and quote:
            parts.append(f"{name} is {_spoken_change(quote.get('change_pct'))}")
    if not parts:
        return "Couldn't reach TradingView for market data at the moment, sir."
    return f"Markets, sir: {', '.join(parts)}."


async def resolve_chart_url(query: str) -> str:
    """Build a TradingView chart URL for a name/ticker. Falls back to search page."""
    match = await search_symbol(query)
    if match:
        return _CHART_URL.format(symbol=_urlquote(match["symbol"], safe=""))
    return f"https://www.tradingview.com/symbols/?query={_urlquote(query)}"
