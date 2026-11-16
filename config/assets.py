"""Configurable asset lists.

The bot consumes Deriv Volatility Indices. `DERIV_SYMBOLS` maps the display
name used in signals/logs to the Deriv API symbol. `DERIV_ASSETS` in the
.env file may hold a comma-separated list of Deriv symbols; when empty all
13 volatility indices are watched.
"""

from __future__ import annotations

import os

#: Display name -> Deriv API symbol.
DERIV_SYMBOLS: dict[str, str] = {
    "Volatility 10 Index": "R_10",
    "Volatility 25 Index": "R_25",
    "Volatility 50 Index": "R_50",
    "Volatility 75 Index": "R_75",
    "Volatility 100 Index": "R_100",
    "Volatility 10 (1s) Index": "1HZ10V",
    "Volatility 15 (1s) Index": "1HZ15V",
    "Volatility 25 (1s) Index": "1HZ25V",
    "Volatility 30 (1s) Index": "1HZ30V",
    "Volatility 50 (1s) Index": "1HZ50V",
    "Volatility 75 (1s) Index": "1HZ75V",
    "Volatility 100 (1s) Index": "1HZ100V",
    "Volatility 150 (1s) Index": "1HZ150V",
    "Volatility 250 (1s) Index": "1HZ250V",
    "Volatility 300 (1s) Index": "1HZ300V",
}

#: Deriv symbol -> display name (reverse of DERIV_SYMBOLS).
DERIV_NAMES: dict[str, str] = {symbol: name for name, symbol in DERIV_SYMBOLS.items()}


def all_assets() -> list[str]:
    """Configured assets as display names; unknown symbols pass through."""
    raw = os.getenv("DERIV_ASSETS", "").strip()
    if not raw:
        return list(DERIV_SYMBOLS)
    return [
        DERIV_NAMES.get(symbol.strip(), symbol.strip())
        for symbol in raw.split(",")
        if symbol.strip()
    ]


def deriv_symbol(asset: str) -> str:
    """Deriv API symbol for `asset` (display name or raw symbol)."""
    return DERIV_SYMBOLS.get(asset.strip(), asset.strip())


def asset_slug(asset: str) -> str:
    """Volatility 10 Index -> VOLATILITY10INDEX ; R_100 -> R100"""
    cleaned = asset.replace("/", "").replace(" ", "").replace("&", "AND")
    return "".join(char for char in cleaned if char.isalnum()).upper()
