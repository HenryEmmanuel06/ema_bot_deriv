"""Telegram notification service with duplicate prevention."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from src.signals.models import Setup, SignalKind
from src.telegram.bot import TelegramBot
from src.telegram.formatter import (
    format_buy_opened,
    format_convergence,
    format_execution_signal,
    format_exit_signal,
    format_invalidated_signal,
    format_trade_result,
)
from src.utils.logging import get_logger
from src.utils.time import now_utc

if TYPE_CHECKING:
    from src.execution.mt5_executor import OpenPosition

logger = get_logger(__name__)


class Notifier:
    def __init__(
        self,
        bot: TelegramBot,
        display_timezone: str = "UTC",
        notify_invalidations: bool = False,
        notify_execution: bool = True,
        notify_convergence: bool = True,
        enabled: bool = True,
    ) -> None:
        self.bot = bot
        self.tz = display_timezone
        self.notify_invalidations = notify_invalidations
        self.notify_execution = notify_execution
        self.notify_convergence = notify_convergence
        self.enabled = enabled and bot.configured
        self._sent: set[tuple[str, str]] = set()
        self.outbox: list[tuple[str, str]] = []

    def _dispatch(self, setup_id: str, label: str, text: str, allowed: bool = True) -> bool:
        key = (setup_id, label)
        if key in self._sent:
            logger.debug("duplicate %s suppressed for %s", label, setup_id)
            return False
        self._sent.add(key)
        self.outbox.append((setup_id, text))
        if not allowed:
            logger.info("[telegram %s disabled by env] %s", label, setup_id)
            return False
        if not self.enabled:
            logger.info("[telegram disabled] %s\n%s", label, text)
            return False
        ok = self.bot.try_send_message(text)
        if ok:
            logger.info("Telegram %s sent (%s)", label, setup_id)
        return ok

    # ------------------------------------------------------------ phases
    def send_convergence(self, setup: Setup, timestamp: datetime) -> bool:
        return self._dispatch(
            setup.setup_id,
            SignalKind.CONVERGENCE.value,
            format_convergence(setup, timestamp, self.tz),
            allowed=self.notify_convergence,
        )

    def send_execution_signal(
        self, setup: Setup, timestamp: datetime, trading: bool = False
    ) -> bool:
        return self._dispatch(
            setup.setup_id,
            SignalKind.EXECUTION.value,
            format_execution_signal(setup, timestamp, self.tz, trading=trading),
            allowed=self.notify_execution,
        )

    def send_buy_opened(
        self, setup: Setup, timestamp: datetime, trades: list[OpenPosition]
    ) -> bool:
        return self._dispatch(
            setup.setup_id, "BUY_OPENED", format_buy_opened(setup, trades, timestamp, self.tz)
        )

    def send_exit_signal(self, setup: Setup, timestamp: datetime, trading: bool = False) -> bool:
        return self._dispatch(
            setup.setup_id, "EXIT", format_exit_signal(setup, timestamp, self.tz, trading)
        )

    def send_trade_result(
        self,
        position: OpenPosition,
        profit: float,
        reason: str,
        setup: Setup | None = None,
        exit_price: float | None = None,
    ) -> bool:
        """The single result message, once the whole basket has closed."""
        return self._dispatch(
            position.setup_id,
            "TRADE_RESULT",
            format_trade_result(
                position, profit, reason, now_utc(), self.tz, setup=setup, exit_price=exit_price
            ),
        )

    def send_invalidated_signal(self, setup: Setup, timestamp: datetime) -> bool:
        if not self.notify_invalidations:
            logger.info(
                "setup %s %s: %s",
                setup.setup_id,
                setup.status.value,
                setup.invalidation_reason,
            )
            return False
        return self._dispatch(
            setup.setup_id,
            SignalKind.INVALIDATED.value,
            format_invalidated_signal(setup, timestamp, self.tz),
        )
