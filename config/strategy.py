"""Triple EMA Convergence strategy configuration.

Every numeric parameter is a proposed default that must be validated through
backtesting. Nothing here is a proven value. Each value can be overridden
from the .env file under the same upper-case name.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PRICE_SOURCES = ("standard_close", "heikin_ashi_close")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _str(name: str, default: str) -> str:
    return (os.getenv(name, "").strip() or default).lower()


STRATEGY_CONFIG: dict = {
    # --- EMAs ----------------------------------------------------------
    "ema_fast": _int("EMA_FAST", 7),
    "ema_mid": _int("EMA_MID", 14),
    "ema_slow": _int("EMA_SLOW", 32),
    # "standard_close" (recommended) or "heikin_ashi_close".
    "ema_price_source": _str("EMA_PRICE_SOURCE", "standard_close"),
    # --- convergence ---------------------------------------------------
    # spread = max(EMAs) - min(EMAs); converged when spread / close * 100
    # is at most this value on one of the last CONVERGENCE_LOOKBACK candles.
    "max_ema_spread_pct": _float("MAX_EMA_SPREAD_PCT", 0.10),
    "convergence_lookback": _int("CONVERGENCE_LOOKBACK", 3),
    # --- entry -----------------------------------------------------------
    # A convergence setup expires after this many closed candles without a
    # BUY confirmation.
    "setup_max_age_candles": _int("SETUP_MAX_AGE_CANDLES", 8),
    # --- stop-loss: most recent swing low ------------------------------
    # A swing low is a candle whose low is below the `swing_lookback`
    # candles on its left and not above the `swing_lookback` candles on its
    # right (fractal). The SL sits at the most recent one below the entry,
    # searched within the last `sl_search_candles` closed candles.
    "swing_lookback": _int("SWING_LOOKBACK", 2),
    "sl_search_candles": _int("SL_SEARCH_CANDLES", 50),
    # --- data health ---------------------------------------------------
    # Closed candles required before signals are allowed (EMA warm-up).
    "min_candles_for_analysis": _int("MIN_CANDLES_FOR_ANALYSIS", 100),
    # Candles that may be missing in a row before the stream is flagged
    # DATA_UNRELIABLE (new entries are suspended).
    "max_missing_candles": 2,
}


#: Deriv candle granularities are fixed by the API (multiples of one minute).
TIMEFRAME_CONFIG: dict = {
    "1m": {"seconds": 60},
    "2m": {"seconds": 120},
    "3m": {"seconds": 180},
    "5m": {"seconds": 300},
    "10m": {"seconds": 600},
    "15m": {"seconds": 900},
    "30m": {"seconds": 1800},
}


def timeframe_seconds(timeframe: str) -> int:
    return int(TIMEFRAME_CONFIG[timeframe]["seconds"])


def validate_strategy_config(config: dict | None = None) -> list[str]:
    """Configuration problems in the strategy parameters."""
    cfg = config or STRATEGY_CONFIG
    problems: list[str] = []
    if not (0 < cfg["ema_fast"] < cfg["ema_mid"] < cfg["ema_slow"]):
        problems.append("EMA periods must satisfy 0 < EMA_FAST < EMA_MID < EMA_SLOW")
    if cfg["ema_price_source"] not in PRICE_SOURCES:
        problems.append(f"EMA_PRICE_SOURCE must be one of {PRICE_SOURCES}")
    if cfg["max_ema_spread_pct"] <= 0:
        problems.append("MAX_EMA_SPREAD_PCT must be positive")
    for key in (
        "convergence_lookback",
        "setup_max_age_candles",
        "swing_lookback",
        "sl_search_candles",
    ):
        if cfg[key] < 1:
            problems.append(f"{key.upper()} must be at least 1")
    return problems
