from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.strategy import timeframe_seconds  # noqa: E402
from src.data.models import Candle  # noqa: E402

START = datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc)
PAD = 0.00002


def make_candle(
    open_: float,
    high: float,
    low: float,
    close: float,
    index: int = 0,
    asset: str = "EUR/USD",
    timeframe: str = "5m",
    start: datetime = START,
) -> Candle:
    seconds = timeframe_seconds(timeframe)
    return Candle(
        asset=asset,
        timeframe=timeframe,
        timestamp=start + timedelta(seconds=seconds * index),
        open=round(open_, 5),
        high=round(high, 5),
        low=round(low, 5),
        close=round(close, 5),
        volume=1.0,
        source="test",
    )


class Feeder:
    """Builds a candle series where each candle opens at the previous close."""

    def __init__(self, start_price: float, asset: str = "EUR/USD", timeframe: str = "5m") -> None:
        self.price = start_price
        self.asset = asset
        self.timeframe = timeframe
        self.index = 0
        self.candles: list[Candle] = []

    def add_close(self, close: float, pad: float = PAD) -> Candle:
        open_ = self.price
        high = max(open_, close) + pad
        low = min(open_, close) - pad
        return self.add(open_, high, low, close)

    def add(self, open_: float, high: float, low: float, close: float) -> Candle:
        candle = make_candle(
            open_, high, low, close, index=self.index, asset=self.asset, timeframe=self.timeframe
        )
        self.candles.append(candle)
        self.price = close
        self.index += 1
        return candle

    def add_closes(self, closes: list[float], pad: float = PAD) -> list[Candle]:
        return [self.add_close(close, pad) for close in closes]

    def filler(self, count: int, base: float | None = None) -> list[Candle]:
        base = self.price if base is None else base
        closes = [base + (0.00001 if i % 2 else -0.00001) for i in range(count)]
        return self.add_closes(closes)


def ema_cycle_closes(start: float = 1000.0) -> list[float]:
    """A price path that drives one full Triple EMA cycle with real EMAs:
    downtrend (32 EMA on top) -> flat (EMAs converge) -> uptrend (32 EMA
    crosses under 7 and 14 -> BUY) -> downtrend (32 EMA crosses back above
    14 and 7 -> exit)."""
    closes: list[float] = []
    price = start
    for _ in range(110):
        price -= 0.6
        closes.append(price)
    for i in range(30):
        closes.append(price + (0.05 if i % 2 else -0.05))
    for _ in range(25):
        price += 0.8
        closes.append(price)
    for _ in range(40):
        price -= 0.8
        closes.append(price)
    return closes


@pytest.fixture
def cycle_feeder() -> Feeder:
    return Feeder(1000.0, asset="Volatility 25 Index")
