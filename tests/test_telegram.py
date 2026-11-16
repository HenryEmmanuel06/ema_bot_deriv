from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from config.settings import normalize_chat_id
from src.signals.models import Setup, SetupStatus, build_setup_id
from src.telegram.formatter import (
    format_buy_opened,
    format_convergence,
    format_execution_signal,
)
from src.telegram.notifier import Notifier

NOW = datetime(2026, 8, 25, 18, 20, tzinfo=timezone.utc)
ASSET = "Volatility 25 Index"


class FakeBot:
    configured = True

    def __init__(self) -> None:
        self.sent: list[str] = []

    def try_send_message(self, text: str) -> bool:
        self.sent.append(text)
        return True


def make_setup() -> Setup:
    setup = Setup(
        setup_id=build_setup_id(ASSET, "5m", NOW),
        asset=ASSET,
        timeframe="5m",
        source="test",
    )
    setup.convergence_spread_pct = 0.0412
    setup.convergence_emas = {"fast": 939.1, "mid": 939.2, "slow": 939.4}
    setup.cross_fast_time = NOW - timedelta(minutes=5)
    setup.cross_mid_time = NOW
    setup.entry_emas = {"fast": 940.1, "mid": 939.8, "slow": 939.5}
    setup.entry_price = 940.6
    setup.stop_loss = 933.65
    setup.status = SetupStatus.OPEN
    return setup


def test_setup_id_fits_the_mt5_comment():
    assert build_setup_id(ASSET, "5m", NOW) == "R25-5M-EMA-20260825-182000"
    assert len(build_setup_id("Volatility 100 (1s) Index", "30m", NOW)) <= 31


def test_convergence_message():
    text = format_convergence(make_setup(), NOW)
    assert "EMA CONVERGENCE" in text
    assert ASSET in text and "5M" in text
    assert "EMA 7: 939.100" in text
    assert "EMA 32: 939.400" in text
    assert "Spread: 0.0412%" in text


def test_execution_message_contains_required_fields():
    text = format_execution_signal(make_setup(), NOW)
    assert text.startswith("\U0001f7e2 BUY EXECUTION SIGNAL")
    assert "32 under 7:" in text and "32 under 14:" in text
    assert "EMA 7 > EMA 14 > EMA 32 ✓" in text
    assert "Entry Price:\n940.600" in text
    assert "SL (swing low): 933.650" in text
    assert "Direction: BUY" in text
    assert "Strategy signal only" in text
    assert "R25-5M-EMA-20260825-182000" in text


def test_execution_message_when_trading():
    text = format_execution_signal(make_setup(), NOW, trading=True)
    assert "Opening the BUY basket on MT5" in text
    assert "Strategy signal only" not in text


def test_buy_opened_message_lists_every_ticket():
    trades = [
        SimpleNamespace(ticket=t, volume=0.5, entry=940.7, sl=933.65) for t in (11, 12, 13)
    ]
    text = format_buy_opened(make_setup(), trades, NOW)
    assert "✅ BUY OPENED" in text
    assert "Positions: 3 x 0.5" in text
    assert "Actual Entry: 940.700" in text
    assert "Tickets: 11, 12, 13" in text


def test_duplicate_messages_are_suppressed():
    bot = FakeBot()
    notifier = Notifier(bot, display_timezone="UTC")
    setup = make_setup()
    assert notifier.send_execution_signal(setup, NOW)
    assert not notifier.send_execution_signal(setup, NOW)
    assert len(bot.sent) == 1


def test_execution_toggle_off_still_records_the_outbox():
    bot = FakeBot()
    notifier = Notifier(bot, display_timezone="UTC", notify_execution=False)
    setup = make_setup()
    assert not notifier.send_execution_signal(setup, NOW)
    assert bot.sent == []
    assert len(notifier.outbox) == 1


def test_convergence_toggle():
    bot = FakeBot()
    notifier = Notifier(bot, display_timezone="UTC", notify_convergence=False)
    assert not notifier.send_convergence(make_setup(), NOW)
    assert bot.sent == []


def test_invalidations_are_silent_by_default():
    bot = FakeBot()
    notifier = Notifier(bot, display_timezone="UTC", notify_invalidations=False)
    setup = make_setup()
    setup.invalidation_reason = "EMA ordering turned bearish before entry"
    assert not notifier.send_invalidated_signal(setup, NOW)
    assert bot.sent == []


def test_channel_chat_id_is_normalized():
    assert normalize_chat_id("1004313011768") == "-1004313011768"
    assert normalize_chat_id("-1004313011768") == "-1004313011768"
    assert normalize_chat_id("123456789") == "123456789"
    assert normalize_chat_id("@mychannel") == "@mychannel"
