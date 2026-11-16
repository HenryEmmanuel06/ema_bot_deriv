"""Triple EMA state machine - acceptance tests.

Most tests script the EMA values candle by candle (so every crossover is
exact) while real candles carry the prices used for the spread % and the
swing-low stop. `test_real_emas_*` run the machine on genuine EMAs.
"""

from __future__ import annotations

import pytest
from conftest import Feeder, ema_cycle_closes

import src.strategy.state_machine as sm
from config.strategy import STRATEGY_CONFIG
from src.signals.models import EXIT_EMA, EXIT_STOP, Result, SetupStatus, SignalKind
from src.strategy.ema import EmaSet
from src.strategy.state_machine import EmaStateMachine, SetupState

#: Spread limit 0.10% of ~100 = 0.10 price units.
DIVERGED_BEAR = (99.0, 99.5, 100.5)
CONVERGED = (99.98, 99.99, 100.02)
UNDER_7 = (100.05, 100.00, 100.02)  # 32 crosses under 7 only
UNDER_14 = (100.10, 100.06, 100.04)  # then under 14: 7 > 14 > 32
BULL = (100.30, 100.20, 100.10)
ABOVE_14 = (100.30, 100.15, 100.18)  # 32 crosses back above 14 only
ABOVE_7 = (100.12, 100.14, 100.20)  # then above 7: 32 above both

#: Closes with a clear swing low at 99.50 (index 2) for the stop-loss.
WARMUP_CLOSES = [100.5, 100.0, 99.5, 99.8, 100.0]


class Harness:
    def __init__(self, monkeypatch, simulate_stops: bool = True, **overrides) -> None:
        config = {**STRATEGY_CONFIG, "min_candles_for_analysis": 3, **overrides}
        self.machine = EmaStateMachine(
            "EUR/USD", "5m", source="test", simulate_stops=simulate_stops, config=config
        )
        self.feeder = Feeder(100.5)
        self.triples: list[tuple[float, float, float]] = []
        monkeypatch.setattr(sm, "compute_emas", self._emas)

    def _emas(self, candles, *_args) -> EmaSet:
        rows = self.triples[-len(candles):]
        return EmaSet([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])

    def step(self, triple, close: float = 100.2, low: float | None = None) -> list:
        self.triples.append(triple)
        open_ = self.feeder.price
        candle = self.feeder.add(
            open_,
            max(open_, close) + 0.01,
            low if low is not None else min(open_, close) - 0.01,
            close,
        )
        return self.machine.on_closed_candle(candle)

    def run(self, triples, close: float = 100.2) -> list:
        events: list = []
        for triple in triples:
            events.extend(self.step(triple, close))
        return events

    def warm_up(self) -> list:
        events: list = []
        for close in WARMUP_CLOSES:
            events.extend(self.step(DIVERGED_BEAR, close))
        return events

    def open_buy(self) -> list:
        events = self.warm_up()
        events += self.run([CONVERGED, UNDER_7, UNDER_14])
        return events


def kinds(events) -> list[SignalKind]:
    return [e.kind for e in events]


# --------------------------------------------------------------- entries
def test_1_convergence_then_both_crosses_and_bullish_order_gives_one_buy(monkeypatch):
    h = Harness(monkeypatch)
    events = h.open_buy()
    assert kinds(events) == [SignalKind.CONVERGENCE, SignalKind.EXECUTION]
    setup = events[-1].setup
    assert setup.status is SetupStatus.OPEN
    assert setup.entry_price == 100.2
    assert setup.cross_fast_time is not None and setup.cross_mid_time is not None
    assert setup.entry_emas == {"fast": 100.10, "mid": 100.06, "slow": 100.04}
    assert h.machine.state is SetupState.WAITING_FOR_32_EMA_EXIT_CROSSES

    # Staying bullish never produces a second BUY for the same event.
    assert h.run([BULL] * 10) == []


def test_convergence_alone_emits_only_the_phase_1_event(monkeypatch):
    h = Harness(monkeypatch)
    events = h.warm_up() + h.run([CONVERGED])
    assert kinds(events) == [SignalKind.CONVERGENCE]
    setup = events[0].setup
    assert setup.convergence_spread_pct == pytest.approx(0.04 / 100.2 * 100, abs=1e-4)
    assert h.machine.state is SetupState.WAITING_FOR_BULLISH_RESOLUTION


def test_2_crosses_without_recent_convergence_give_no_buy(monkeypatch):
    h = Harness(monkeypatch)
    events = h.warm_up()
    # Same crossing sequence, but the EMAs never cluster within 0.10%.
    events += h.run([(99.6, 99.8, 100.4), (100.6, 100.0, 100.3), (101.2, 100.8, 100.5)])
    assert SignalKind.EXECUTION not in kinds(events)
    assert SignalKind.CONVERGENCE not in kinds(events)


def test_3_intersection_that_never_resolves_bullish_gives_no_buy(monkeypatch):
    h = Harness(monkeypatch)
    tangled = (100.06, 99.99, 100.02)  # mid below slow - not bullish, not bearish
    events = h.warm_up() + h.run([CONVERGED, UNDER_7] + [tangled] * 10)
    assert SignalKind.EXECUTION not in kinds(events)
    assert SignalKind.EXPIRED in kinds(events)


def test_4_intrabar_wicks_never_create_a_signal():
    """Only closed candles are evaluated and EMAs use the close: a candle
    that spikes intrabar but closes flat changes nothing."""
    machine = EmaStateMachine("EUR/USD", "5m", source="test")
    feeder = Feeder(100.0)
    events = []
    for _ in range(150):
        events += machine.on_closed_candle(feeder.add(100.0, 100.01, 99.99, 100.0))
    convergence_only = kinds(events)
    events = machine.on_closed_candle(feeder.add(100.0, 105.0, 95.0, 100.0))
    assert SignalKind.EXECUTION not in convergence_only + kinds(events)


def test_5_slow_bullish_resolution_still_buys_within_setup_age(monkeypatch):
    h = Harness(monkeypatch)
    hold = (100.06, 99.99, 100.02)  # tangled - 32 between 7 and 14
    events = h.warm_up() + h.run([CONVERGED, UNDER_7, hold, hold, hold, hold, UNDER_14])
    assert kinds(events) == [SignalKind.CONVERGENCE, SignalKind.EXECUTION]


def test_5b_bullish_intersection_on_the_convergence_candle_buys_at_once(monkeypatch):
    h = Harness(monkeypatch, convergence_lookback=6)
    events = h.warm_up()
    # The fast EMAs drift up through the 32 EMA and all three intersect on
    # the last candle, already ordered bullish - the BUY fires on the spot.
    events += h.run(
        [
            (100.20, 99.80, 100.00),  # 32 under 7
            (100.20, 99.85, 100.00),
            (100.20, 99.90, 100.00),
            (100.20, 100.05, 100.01),  # 32 under 14
            (100.03, 100.02, 100.01),  # converged + bullish
        ]
    )
    assert kinds(events) == [SignalKind.CONVERGENCE, SignalKind.EXECUTION]


def test_bullish_resolution_right_after_the_intersection_buys(monkeypatch):
    h = Harness(monkeypatch)
    events = h.warm_up() + h.run([CONVERGED, UNDER_14])
    assert kinds(events) == [SignalKind.CONVERGENCE, SignalKind.EXECUTION]


def test_bullish_ordering_without_a_fresh_intersection_gives_no_buy(monkeypatch):
    """The three MAs must have met: once the intersection has gone stale
    (no clustered candle within CONVERGENCE_LOOKBACK), a bullish ordering
    on separated EMAs never fires a BUY."""
    h = Harness(monkeypatch)
    separated = (100.20, 99.95, 100.10)  # spread 0.25% - not met, not resolved
    events = h.warm_up() + h.run([CONVERGED] + [separated] * 4 + [BULL] * 10)
    assert SignalKind.EXECUTION not in kinds(events)
    assert SignalKind.EXPIRED in kinds(events)


def test_bearish_resolution_of_the_intersection_resets_the_setup(monkeypatch):
    h = Harness(monkeypatch)
    # (99.80, 99.95, 100.10): fast < mid < slow separated well past 0.10%.
    events = h.warm_up() + h.run([CONVERGED, UNDER_7, (99.80, 99.95, 100.10)])
    invalid = [e for e in events if e.kind is SignalKind.INVALIDATED]
    assert invalid and "bearish" in invalid[0].detail
    assert h.machine.state is SetupState.WAITING_FOR_CONVERGENCE


def test_setup_expires_after_max_age(monkeypatch):
    h = Harness(monkeypatch)
    events = h.warm_up() + h.run([CONVERGED] * 10)
    assert SignalKind.EXPIRED in kinds(events)


def test_32_already_below_both_creates_no_setup(monkeypatch):
    h = Harness(monkeypatch)
    events = h.run([BULL] * 5 + [(100.03, 100.02, 99.99)])
    assert events == []


# ------------------------------------------------------------- stop-loss
def test_buy_sl_sits_at_the_most_recent_swing_low(monkeypatch):
    h = Harness(monkeypatch)
    setup = h.open_buy()[-1].setup
    # Swing low = warm-up candle 2 (close 99.5, low 99.49).
    assert setup.stop_loss == pytest.approx(99.49)
    assert setup.stop_loss < setup.entry_price
    assert setup.swing_low_time == h.feeder.candles[2].timestamp


def test_virtual_stop_closes_the_buy_in_signal_only_mode(monkeypatch):
    h = Harness(monkeypatch)
    setup = h.open_buy()[-1].setup
    events = h.step(BULL, close=99.6, low=setup.stop_loss - 0.01)
    assert kinds(events) == [SignalKind.STOPPED]
    assert setup.exit_price == setup.stop_loss
    assert setup.exit_reason == EXIT_STOP
    assert setup.result is Result.LOSS
    assert h.machine.position is None


def test_broker_owns_the_stop_when_trading(monkeypatch):
    h = Harness(monkeypatch, simulate_stops=False)
    setup = h.open_buy()[-1].setup
    assert h.step(BULL, close=99.6, low=setup.stop_loss - 0.01) == []
    assert h.machine.position is setup
    assert h.machine.release(setup.setup_id)
    assert h.machine.state is SetupState.WAITING_FOR_CONVERGENCE


# ----------------------------------------------------------------- exits
def test_6_32_crossing_above_only_14_keeps_the_buy_open(monkeypatch):
    h = Harness(monkeypatch)
    h.open_buy()
    assert h.run([BULL, ABOVE_14]) == []
    assert h.machine.position is not None
    assert h.machine.position.exit_cross_mid_time is not None
    assert h.machine.position.exit_cross_fast_time is None


def test_7_32_above_both_14_and_7_exits_the_buy(monkeypatch):
    h = Harness(monkeypatch)
    setup = h.open_buy()[-1].setup
    events = h.run([BULL, ABOVE_14]) + h.step(ABOVE_7, close=100.6)
    assert kinds(events) == [SignalKind.EXIT]
    assert setup.exit_reason == EXIT_EMA
    assert setup.exit_price == 100.6
    assert setup.result is Result.WIN
    assert h.machine.state is SetupState.WAITING_FOR_CONVERGENCE


def test_exit_crosses_on_different_candles_wait_until_32_is_above_both(monkeypatch):
    h = Harness(monkeypatch)
    h.open_buy()
    # 32 crosses above 7 first, then dips back under 7 before crossing 14.
    events = h.run([(100.10, 100.30, 100.20), (100.30, 100.40, 100.25)])
    assert events == []
    events = h.run([(100.20, 100.24, 100.26)])
    assert kinds(events) == [SignalKind.EXIT]


def test_a_new_cycle_after_the_exit_opens_a_new_buy(monkeypatch):
    h = Harness(monkeypatch)
    first = h.open_buy()[-1].setup
    h.run([BULL, ABOVE_14, ABOVE_7])
    events = h.run([CONVERGED, UNDER_14])
    assert kinds(events) == [SignalKind.CONVERGENCE, SignalKind.EXECUTION]
    assert events[-1].setup.setup_id != first.setup_id


# ---------------------------------------------------- restart / data health
def test_9_restored_buy_continues_exit_monitoring(monkeypatch):
    h = Harness(monkeypatch)
    setup = h.open_buy()[-1].setup
    # Simulate a restart: a fresh machine with the same history.
    fresh = EmaStateMachine("EUR/USD", "5m", source="test", config=h.machine.cfg)
    fresh.seed(list(h.feeder.candles))
    fresh.restore(setup)
    h.machine = fresh
    events = h.run([BULL, ABOVE_14, ABOVE_7])
    assert kinds(events) == [SignalKind.EXIT]


def test_10_unreliable_stream_suspends_entries_but_not_exits(monkeypatch):
    h = Harness(monkeypatch)
    h.warm_up()
    h.machine.data_unreliable = True
    assert h.run([CONVERGED, UNDER_7, UNDER_14]) == []

    h2 = Harness(monkeypatch)
    h2.open_buy()
    h2.machine.data_unreliable = True
    assert kinds(h2.run([BULL, ABOVE_14, ABOVE_7])) == [SignalKind.EXIT]


def test_duplicate_candles_are_ignored(monkeypatch):
    h = Harness(monkeypatch)
    h.warm_up()
    before = len(h.machine.candles)
    assert h.machine.on_closed_candle(h.feeder.candles[-1]) == []
    assert len(h.machine.candles) == before


# --------------------------------------------------------- real EMAs
def test_real_emas_run_one_full_cycle(cycle_feeder):
    machine = EmaStateMachine("Volatility 25 Index", "5m", source="test")
    events = []
    for close in ema_cycle_closes():
        events += machine.on_closed_candle(cycle_feeder.add_close(close, pad=0.3))
    assert kinds(events) == [SignalKind.CONVERGENCE, SignalKind.EXECUTION, SignalKind.EXIT]
    buy, exit_ = events[1].setup, events[2].setup
    assert buy is exit_
    assert buy.stop_loss < buy.entry_price
    assert buy.exit_reason == EXIT_EMA


def test_real_emas_with_heikin_ashi_closes_also_work(cycle_feeder):
    config = {**STRATEGY_CONFIG, "ema_price_source": "heikin_ashi_close"}
    machine = EmaStateMachine("Volatility 25 Index", "5m", source="test", config=config)
    events = []
    for close in ema_cycle_closes():
        events += machine.on_closed_candle(cycle_feeder.add_close(close, pad=0.3))
    assert SignalKind.EXECUTION in kinds(events)
    assert SignalKind.EXIT in kinds(events)
