"""Exponential moving averages and the 32-EMA crossover rules.

EMAs are computed from the close of standard OHLC candles by default; the
Heikin Ashi close is used only when EMA_PRICE_SOURCE=heikin_ashi_close.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.data.models import Candle
from src.strategy.heikin_ashi import heikin_ashi_series


def ema_series(values: list[float], period: int) -> list[float]:
    """Standard EMA (alpha = 2 / (period + 1)) seeded with the first value."""
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def price_series(candles: list[Candle], source: str = "standard_close") -> list[float]:
    if source == "heikin_ashi_close":
        return [c.close for c in heikin_ashi_series(candles)]
    return [c.close for c in candles]


@dataclass(frozen=True)
class EmaSet:
    """Fast / mid / slow EMA series aligned index-for-index with the candles."""

    fast: list[float]
    mid: list[float]
    slow: list[float]

    def values(self, index: int) -> dict[str, float]:
        return {"fast": self.fast[index], "mid": self.mid[index], "slow": self.slow[index]}

    def spread(self, index: int) -> float:
        triple = (self.fast[index], self.mid[index], self.slow[index])
        return max(triple) - min(triple)

    def spread_pct(self, index: int, close: float) -> float:
        return self.spread(index) / close * 100 if close else float("inf")

    def bullish(self, index: int) -> bool:
        """EMA fast > EMA mid > EMA slow."""
        return self.fast[index] > self.mid[index] > self.slow[index]

    def bearish(self, index: int) -> bool:
        """EMA fast < EMA mid < EMA slow."""
        return self.fast[index] < self.mid[index] < self.slow[index]


def compute_emas(
    candles: list[Candle], fast: int, mid: int, slow: int, source: str = "standard_close"
) -> EmaSet:
    prices = price_series(candles, source)
    return EmaSet(ema_series(prices, fast), ema_series(prices, mid), ema_series(prices, slow))


def slow_crosses_under(slow: list[float], other: list[float], index: int) -> bool:
    """EMA32[t-1] >= A[t-1] and EMA32[t] < A[t]."""
    return index >= 1 and slow[index - 1] >= other[index - 1] and slow[index] < other[index]


def slow_crosses_over(slow: list[float], other: list[float], index: int) -> bool:
    """EMA32[t-1] <= A[t-1] and EMA32[t] > A[t]."""
    return index >= 1 and slow[index - 1] <= other[index - 1] and slow[index] > other[index]
