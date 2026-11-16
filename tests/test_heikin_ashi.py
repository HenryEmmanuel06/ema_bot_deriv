from __future__ import annotations

from conftest import Feeder, ema_cycle_closes, make_candle

from src.strategy.heikin_ashi import (
    HeikinAshiConverter,
    heikin_ashi_candle,
    heikin_ashi_series,
)


# ------------------------------------------------------------------ formulas
def test_first_candle_seeds_open_from_open_and_close():
    candle = make_candle(1.1600, 1.1620, 1.1590, 1.1610)
    ha = heikin_ashi_candle(candle)
    assert ha.close == (1.1600 + 1.1620 + 1.1590 + 1.1610) / 4
    assert ha.open == (1.1600 + 1.1610) / 2


def test_subsequent_open_is_midpoint_of_previous_heikin_ashi_body():
    first = heikin_ashi_candle(make_candle(1.1600, 1.1620, 1.1590, 1.1610, index=0))
    second = heikin_ashi_candle(make_candle(1.1610, 1.1630, 1.1605, 1.1625, index=1), first)
    assert second.open == (first.open + first.close) / 2


def test_high_and_low_envelop_the_body():
    candles = [
        make_candle(1.1600, 1.1620, 1.1590, 1.1610, index=0),
        make_candle(1.1610, 1.1615, 1.1560, 1.1570, index=1),
    ]
    for ha in heikin_ashi_series(candles):
        assert ha.high >= max(ha.open, ha.close)
        assert ha.low <= min(ha.open, ha.close)


def test_series_preserves_timestamps_and_length():
    feeder = Feeder(1000.0)
    candles = feeder.add_closes(ema_cycle_closes()[:12])
    series = heikin_ashi_series(candles)
    assert len(series) == len(candles)
    assert [c.timestamp for c in series] == [c.timestamp for c in candles]


def test_converter_matches_series():
    feeder = Feeder(1000.0)
    candles = feeder.add_closes(ema_cycle_closes()[:12])
    converter = HeikinAshiConverter()
    rolling = [converter.add(candle) for candle in candles]
    assert rolling == heikin_ashi_series(candles)


def test_smoothing_removes_a_single_noise_candle():
    """A lone counter-candle inside a downtrend stays red in Heikin Ashi."""
    downtrend = [
        make_candle(1.1600, 1.16005, 1.15900, 1.1591, index=0),
        make_candle(1.1591, 1.15915, 1.15800, 1.1581, index=1),
        # Real candle closes above its open -> green, but the bar is still
        # deep inside the falling range.
        make_candle(1.1581, 1.15830, 1.15750, 1.1582, index=2),
        make_candle(1.1582, 1.15825, 1.15700, 1.1571, index=3),
    ]
    assert downtrend[2].is_green
    series = heikin_ashi_series(downtrend)
    assert series[2].is_red
