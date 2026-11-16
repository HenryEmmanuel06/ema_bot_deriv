"""Stop-loss placement: the most recent swing low below the entry."""

from __future__ import annotations

from src.data.models import Candle


def is_swing_low(candles: list[Candle], index: int, lookback: int) -> bool:
    """Fractal swing low: lower than the `lookback` candles on its left and
    not above the `lookback` candles on its right (which must be closed)."""
    if index - lookback < 0 or index + lookback >= len(candles):
        return False
    low = candles[index].low
    left = candles[index - lookback : index]
    right = candles[index + 1 : index + 1 + lookback]
    return low < min(c.low for c in left) and low <= min(c.low for c in right)


def most_recent_swing_low(
    candles: list[Candle], entry: float, lookback: int = 2, search: int = 50
) -> tuple[float, Candle] | None:
    """(price, candle) of the latest confirmed swing low below `entry`.

    Only the last `search` closed candles are scanned. When no confirmed
    swing low sits below the entry, the lowest low of that window is used
    instead (if it is below the entry). Returns None when nothing qualifies.
    """
    window = candles[-search:] if search > 0 else list(candles)
    for index in range(len(window) - 1, -1, -1):
        if window[index].low < entry and is_swing_low(window, index, lookback):
            return window[index].low, window[index]
    if not window:
        return None
    lowest = min(window, key=lambda c: c.low)
    return (lowest.low, lowest) if lowest.low < entry else None
