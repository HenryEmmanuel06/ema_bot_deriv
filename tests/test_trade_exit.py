from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import START, make_candle

from config.settings import _env_pct
from src.execution.mt5_executor import _stop_level, trail_lock_level
from src.signals.manager import SignalManager
from src.signals.models import (
    EXIT_EMA,
    EXIT_ORDER_FAILED,
    EXIT_STOP,
    EXIT_TARGET,
    Result,
    Setup,
    SetupStatus,
    SignalEvent,
    SignalKind,
)
from src.storage.database import Database
from src.strategy.stop_loss import most_recent_swing_low
from src.telegram.notifier import Notifier

SETUP_ID = "EURUSD-5M-EMA-20260825-182000"


class FakeBot:
    configured = True

    def __init__(self) -> None:
        self.sent: list[str] = []

    def try_send_message(self, text: str) -> bool:
        self.sent.append(text)
        return True


class FakeExecutor:
    """Pretends `positions` BUYs were opened; records basket close requests."""

    def __init__(self, positions: int = 2) -> None:
        self.positions = positions
        self.closed: list[str] = []
        self.open = True
        self.reconciled: set[str] = set()
        #: Pretend the basket was closed at the profit target.
        self.by_target = False
        #: Events the next manage() call reports.
        self.events: list = []

    def execute(self, setup: Setup) -> list:
        return [
            SimpleNamespace(
                ticket=101 + i, setup_id=setup.setup_id, volume=0.5, sl=setup.stop_loss,
                entry=100.21, is_buy=True,
            )
            for i in range(self.positions)
        ]

    def basket_is_open(self, setup_id: str) -> bool:
        return self.open

    def close_basket(self, setup_id: str) -> int:
        self.closed.append(setup_id)
        return self.positions

    def closed_by_target(self, setup_id: str) -> bool:
        by_target, self.by_target = self.by_target, False
        return by_target

    def reconcile(self, setups: list) -> set[str]:
        return self.reconciled

    def manage(self) -> list:
        events, self.events = self.events, []
        return events

    def report_basket_closed(self, setup_id: str, profit: float, exit_price: float) -> None:
        """Simulate every position of the basket being gone on the broker."""
        self.open = False
        position = SimpleNamespace(
            setup_id=setup_id, symbol="Volatility 25 Index", is_buy=True, basket_size=self.positions,
            entry=100.21, sl=99.49, ticket=101,
        )
        self.events = [
            SimpleNamespace(
                kind="basket_closed", position=position, profit=profit, exit_price=exit_price
            )
        ]


def make_setup() -> Setup:
    setup = Setup(setup_id=SETUP_ID, asset="Volatility 25 Index", timeframe="5m", source="test")
    setup.status = SetupStatus.OPEN
    setup.entry_confirmation_time = START
    setup.entry_price = 100.2
    setup.stop_loss = 99.49
    setup.cross_fast_time = START - timedelta(minutes=5)
    setup.cross_mid_time = START
    setup.entry_emas = {"fast": 100.1, "mid": 100.06, "slow": 100.04}
    return setup


def build(executor=None) -> tuple[SignalManager, list]:
    released: list = []
    manager = SignalManager(
        Notifier(FakeBot(), display_timezone="UTC"),
        executor=executor,
        on_trade_closed=released.append,
    )
    return manager, released


def buy(manager: SignalManager) -> Setup:
    setup = make_setup()
    manager.handle_event(SignalEvent(SignalKind.EXECUTION, setup, START))
    return setup


def exit_event(setup: Setup, reason: str = EXIT_EMA, price: float = 100.6) -> SignalEvent:
    setup.exit_reason = reason
    setup.exit_price = price
    setup.exit_signal_time = START + timedelta(minutes=30)
    kind = SignalKind.EXIT if reason == EXIT_EMA else SignalKind.STOPPED
    return SignalEvent(kind, setup, setup.exit_signal_time, reason)


# -------------------------------------------------------- swing-low SL
def test_sl_is_the_most_recent_swing_low_below_entry():
    lows = [10.0, 9.0, 8.0, 9.0, 9.5, 9.2, 8.5, 9.1, 9.6, 9.8]
    candles = [make_candle(low + 0.5, low + 1, low, low + 0.5, index=i) for i, low in enumerate(lows)]
    price, candle = most_recent_swing_low(candles, entry=10.0, lookback=2)
    # Swing lows at 8.0 (index 2) and 8.5 (index 6): the most recent wins.
    assert price == 8.5
    assert candle is candles[6]


def test_unconfirmed_lows_at_the_edge_are_not_swings():
    lows = [10.0, 9.5, 9.0, 8.5, 8.0]  # still falling: no confirmed swing
    candles = [make_candle(low + 0.5, low + 1, low, low + 0.5, index=i) for i, low in enumerate(lows)]
    price, candle = most_recent_swing_low(candles, entry=10.0, lookback=2)
    assert price == 8.0  # falls back to the lowest low of the window
    assert candle is candles[-1]


def test_swing_lows_above_the_entry_are_skipped():
    lows = [10.0, 9.0, 8.0, 9.0, 9.5, 9.2, 8.5, 9.1, 9.6, 9.8]
    candles = [make_candle(low + 0.5, low + 1, low, low + 0.5, index=i) for i, low in enumerate(lows)]
    price, _ = most_recent_swing_low(candles, entry=8.4, lookback=2)
    assert price == 8.0


def info(**kwargs) -> SimpleNamespace:
    defaults = {"trade_stops_level": 10, "point": 0.001, "digits": 3}
    return SimpleNamespace(**(defaults | kwargs))


def test_broker_sl_sits_at_the_swing_low():
    assert _stop_level(info(), entry=100.21, swing_level=99.49, is_buy=True) == 99.49


def test_sl_clamps_to_broker_minimum_when_the_swing_low_is_too_close():
    # min distance = 10 * 0.001 = 0.010 -> SL 0.010 under entry.
    assert _stop_level(info(), entry=100.21, swing_level=100.205, is_buy=True) == 100.2


# --------------------------------------------------------- execution
def test_buy_opens_positions_and_sends_signal_then_opened_message():
    manager, _ = build(FakeExecutor(positions=3))
    setup = buy(manager)
    sent = manager.notifier.bot.sent
    assert "BUY EXECUTION SIGNAL" in sent[0]
    assert "BUY OPENED" in sent[1]
    assert "Positions: 3 x 0.5" in sent[1]
    assert setup.tickets == [101, 102, 103]
    assert SETUP_ID in manager.watched


def test_failed_order_releases_the_machine_and_reports_it():
    manager, released = build(FakeExecutor(positions=0))
    setup = buy(manager)
    assert "BUY ORDER FAILED" in manager.notifier.bot.sent[-1]
    assert released == [setup]
    assert setup.status is SetupStatus.CLOSED
    assert setup.exit_reason == EXIT_ORDER_FAILED


def test_signal_only_mode_places_nothing():
    manager, _ = build(executor=None)
    buy(manager)
    sent = manager.notifier.bot.sent
    assert len(sent) == 1
    assert "No trade is executed" in sent[0]


# ------------------------------------------------------------- exits
def test_ema_exit_closes_the_basket_and_reports_the_result_once():
    executor = FakeExecutor()
    manager, released = build(executor)
    setup = buy(manager)

    manager.handle_event(exit_event(setup))
    assert executor.closed == [SETUP_ID]
    assert "BUY EXIT — EMA REVERSAL" in manager.notifier.bot.sent[-1]

    executor.report_basket_closed(SETUP_ID, profit=12.5, exit_price=100.58)
    manager.on_tick()
    text = manager.notifier.bot.sent[-1]
    assert "TRADE RESULT: WIN +$12.50" in text
    assert EXIT_EMA in text
    assert "Exit Fill: 100.580" in text
    assert setup.result is Result.WIN
    assert setup.status is SetupStatus.CLOSED
    assert setup.exit_price == 100.58
    assert manager.watched == {}
    assert released == [setup]

    count = len(manager.notifier.bot.sent)
    manager.on_tick()
    assert len(manager.notifier.bot.sent) == count


def test_8_exit_signal_without_a_strategy_position_does_nothing():
    executor = FakeExecutor()
    manager, _ = build(executor)
    setup = make_setup()  # never opened by this manager
    manager.handle_event(exit_event(setup))
    assert executor.closed == []
    assert manager.notifier.bot.sent == []

    # Basket already closed by the broker SL: still nothing to close.
    setup = buy(manager)
    executor.open = False
    sent = len(manager.notifier.bot.sent)
    manager.handle_event(exit_event(setup))
    assert executor.closed == []
    assert len(manager.notifier.bot.sent) == sent


def test_closing_retries_until_the_basket_is_flat():
    executor = FakeExecutor()
    manager, _ = build(executor)
    setup = buy(manager)
    manager.handle_event(exit_event(setup))
    manager.on_tick()  # close orders failed once -> retried
    assert executor.closed == [SETUP_ID] * 2
    executor.open = False
    manager.on_tick()
    assert executor.closed == [SETUP_ID] * 2


def test_broker_stop_loss_reports_a_loss_and_releases_the_machine():
    executor = FakeExecutor()
    manager, released = build(executor)
    setup = buy(manager)
    executor.report_basket_closed(SETUP_ID, profit=-8.0, exit_price=99.49)
    manager.on_tick()
    text = manager.notifier.bot.sent[-1]
    assert "TRADE RESULT: LOSS -$8.00" in text
    assert EXIT_STOP in text
    assert setup.result is Result.LOSS
    assert released == [setup]


# ------------------------------------------------- profit target + trail
def test_trail_lock_level_halves_then_lags_the_profit_by_ten_percent():
    assert trail_lock_level(9.9) == (None, None)
    # First milestone: SL -> 50% of the entry->initial-SL distance.
    assert trail_lock_level(10.0) == (1, None)
    assert trail_lock_level(19.9) == (1, None)
    # Then the SL locks the profit one milestone behind.
    assert trail_lock_level(20.0) == (2, 10.0)
    assert trail_lock_level(35.7) == (3, 20.0)
    assert trail_lock_level(50.0) == (5, 40.0)
    assert trail_lock_level(25.0, trail_step_pct=5.0) == (5, 20.0)


def test_percentage_profit_env_accepts_a_percent_sign(monkeypatch):
    monkeypatch.setenv("PERCENTAGE_PROFIT", "40%")
    assert _env_pct("PERCENTAGE_PROFIT") == 40.0
    monkeypatch.setenv("PERCENTAGE_PROFIT", "60")
    assert _env_pct("PERCENTAGE_PROFIT") == 60.0
    monkeypatch.delenv("PERCENTAGE_PROFIT")
    assert _env_pct("PERCENTAGE_PROFIT") == 0.0


def test_profit_target_close_reports_the_target_reason():
    executor = FakeExecutor()
    executor.by_target = True
    manager, released = build(executor)
    setup = buy(manager)
    executor.report_basket_closed(SETUP_ID, profit=40.0, exit_price=101.5)
    manager.on_tick()
    text = manager.notifier.bot.sent[-1]
    assert "TRADE RESULT: WIN +$40.00" in text
    assert "Profit target reached" in text
    assert setup.exit_reason == EXIT_TARGET
    assert setup.result is Result.WIN
    assert released == [setup]


def test_signal_only_exit_and_stop_messages():
    manager, _ = build(executor=None)
    setup = buy(manager)
    setup.result = Result.WIN
    manager.handle_event(exit_event(setup))
    text = manager.notifier.bot.sent[-1]
    assert "BUY EXIT — EMA REVERSAL" in text
    assert "Move: +0.400" in text
    assert setup.status is SetupStatus.CLOSED

    manager, _ = build(executor=None)
    setup = buy(manager)
    setup.result = Result.LOSS
    manager.handle_event(exit_event(setup, EXIT_STOP, 99.49))
    assert "BUY STOP-LOSS HIT" in manager.notifier.bot.sent[-1]


# ----------------------------------------------------------- restart
def test_9_open_buy_is_restored_from_the_database(tmp_path):
    db = Database(tmp_path / "signals.db")
    db.connect()
    setup = make_setup()
    setup.tickets = [101, 102]
    db.save_setup(setup)
    closed = Setup(setup_id="X-5M-EMA-1", asset="Volatility 25 Index", timeframe="5m", source="test")
    closed.status = SetupStatus.CLOSED
    db.save_setup(closed)

    [restored] = db.open_setups()
    assert restored.setup_id == SETUP_ID
    assert restored.entry_price == 100.2
    assert restored.stop_loss == 99.49
    assert restored.entry_confirmation_time == START
    assert restored.entry_emas == setup.entry_emas
    assert restored.tickets == [101, 102]
    db.close()


def test_9_restart_reconciles_with_mt5_positions(tmp_path):
    executor = FakeExecutor()
    executor.reconciled = {SETUP_ID}
    manager, _ = build(executor)
    alive, gone = make_setup(), make_setup()
    gone.setup_id = "EURUSD-5M-EMA-20260825-190000"
    still_open = manager.restore([alive, gone])
    assert still_open == [alive]
    assert SETUP_ID in manager.watched
    assert gone.status is SetupStatus.CLOSED

    # The restored basket is closed by the next EMA exit.
    manager.handle_event(exit_event(alive))
    assert executor.closed == [SETUP_ID]


@pytest.mark.parametrize("kind", [SignalKind.CONVERGENCE, SignalKind.EXECUTION])
def test_events_are_persisted(tmp_path, kind):
    db = Database(tmp_path / "signals.db")
    db.connect()
    manager = SignalManager(Notifier(FakeBot(), display_timezone="UTC"), database=db)
    manager.handle_event(SignalEvent(kind, make_setup(), START))
    rows = db.conn.execute("SELECT kind FROM ema_signals").fetchall()
    assert [r["kind"] for r in rows] == [kind.value]
    db.close()
