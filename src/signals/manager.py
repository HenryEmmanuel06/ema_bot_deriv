"""Routes signal events to Telegram + storage and manages trade exits.

The state machine decides *when* a BUY opens and closes; this manager turns
those decisions into MT5 orders (when TAKE_TRADE=true), notifications and
database rows. A trade's realised result is recorded only once its MT5
basket is flat - closed by the EMA exit, the swing-low stop-loss or by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

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
from src.telegram.notifier import Notifier
from src.utils.logging import get_logger
from src.utils.time import now_utc

if TYPE_CHECKING:
    from src.execution.mt5_executor import MT5Executor

logger = get_logger(__name__)


class SignalManager:
    def __init__(
        self,
        notifier: Notifier,
        database: Database | None = None,
        executor: MT5Executor | None = None,
        on_trade_closed: Callable[[Setup], object] | None = None,
    ) -> None:
        self.notifier = notifier
        self.database = database
        self.executor = executor
        #: Called when a BUY ends outside the state machine (broker SL,
        #: manual close, failed order) so the machine stops monitoring it.
        self._on_trade_closed = on_trade_closed
        self.executed: list[Setup] = []
        #: setup_id -> setup with an open MT5 basket.
        self.watched: dict[str, Setup] = {}
        #: setup_ids whose EMA exit fired and are being closed (retried
        #: until the basket is flat).
        self._closing: set[str] = set()

    @property
    def trading(self) -> bool:
        return self.executor is not None

    def handle(self, events: list[SignalEvent]) -> None:
        for event in events:
            self.handle_event(event)

    def handle_event(self, event: SignalEvent) -> None:
        setup = event.setup
        handler = {
            SignalKind.CONVERGENCE: self._on_convergence,
            SignalKind.EXECUTION: self._on_execution,
            SignalKind.EXIT: self._on_exit,
            SignalKind.STOPPED: self._on_stopped,
        }.get(event.kind, self._on_invalidated)
        handler(event)
        self._save(setup, event.kind, event.detail)

    # ------------------------------------------------------------ phases
    def _on_convergence(self, event: SignalEvent) -> None:
        self.notifier.send_convergence(event.setup, event.timestamp)

    def _on_execution(self, event: SignalEvent) -> None:
        setup = event.setup
        self.executed.append(setup)
        logger.info("signal explanation %s: %s", setup.setup_id, setup.explanation())
        self.notifier.send_execution_signal(setup, event.timestamp, trading=self.trading)
        if self.executor is None:
            return
        trades = self.executor.execute(setup)
        self.notifier.send_buy_opened(setup, event.timestamp, trades)
        if trades:
            setup.tickets = [t.ticket for t in trades]
            self.watched[setup.setup_id] = setup
            return
        # Nothing opened: there is no BUY to manage.
        setup.status = SetupStatus.CLOSED
        setup.exit_reason = EXIT_ORDER_FAILED
        setup.exit_time = now_utc()
        self._release(setup)

    def _on_exit(self, event: SignalEvent) -> None:
        """32 EMA crossed above 14 and 7: close the strategy's BUY."""
        setup = event.setup
        if self.executor is None:
            self._finish_virtual(setup, event)
            return
        if setup.setup_id not in self.watched or not self.executor.basket_is_open(
            setup.setup_id
        ):
            logger.info(
                "EMA exit signal for %s but no strategy BUY position is open - ignored",
                setup.setup_id,
            )
            return
        logger.info(
            "32 EMA above 7 and 14 on %s %s - closing basket %s",
            setup.asset,
            setup.timeframe,
            setup.setup_id,
        )
        self._closing.add(setup.setup_id)
        self.executor.close_basket(setup.setup_id)
        self.notifier.send_exit_signal(setup, event.timestamp, trading=True)

    def _on_stopped(self, event: SignalEvent) -> None:
        """Virtual stop-loss hit (signal-only mode; the broker owns real SLs)."""
        if self.executor is None:
            self._finish_virtual(event.setup, event)

    def _on_invalidated(self, event: SignalEvent) -> None:
        self.notifier.send_invalidated_signal(event.setup, event.timestamp)

    def _finish_virtual(self, setup: Setup, event: SignalEvent) -> None:
        setup.status = SetupStatus.CLOSED
        setup.exit_time = event.timestamp
        self.notifier.send_exit_signal(setup, event.timestamp, trading=False)

    # ---------------------------------------------------------- restart
    def restore(self, setups: list[Setup]) -> list[Setup]:
        """Reconcile BUYs saved as open with MT5 after a restart.

        Returns the setups that are still open and must be monitored. With
        trading on, a saved BUY without MT5 positions closed while the bot
        was down; it is recorded as closed and not monitored.
        """
        if self.executor is None:
            return list(setups)
        alive = self.executor.reconcile(setups)
        still_open: list[Setup] = []
        for setup in setups:
            if setup.setup_id in alive:
                self.watched[setup.setup_id] = setup
                still_open.append(setup)
                continue
            logger.warning(
                "saved BUY %s has no MT5 positions - closed while the bot was offline",
                setup.setup_id,
            )
            setup.status = SetupStatus.CLOSED
            setup.exit_reason = "Closed while the bot was offline"
            setup.exit_time = now_utc()
            if self.database is not None:
                self.database.save_setup(setup)
        return still_open

    # ---------------------------------------------------------- outcomes
    def on_tick(self) -> None:
        """Reports trade lifecycle events and retries pending basket closes."""
        if self.executor is None:
            return
        # `_closing` is cleared by `_finish_trade` once the basket-closed event
        # arrives, so the exit reason survives until the result is reported.
        for setup_id in list(self._closing):
            if self.executor.basket_is_open(setup_id):
                self.executor.close_basket(setup_id)
        for event in self.executor.manage():
            if event.kind == "basket_closed":
                self._finish_trade(
                    event.position.setup_id,
                    event.position,
                    event.profit,
                    getattr(event, "exit_price", None),
                )

    def _finish_trade(
        self, setup_id: str, position, profit: float, exit_price: float | None = None
    ) -> None:
        """The basket is flat: record the result and send it once."""
        closed_by_target = getattr(
            self.executor, "closed_by_target", lambda _sid: False
        )
        reason = (
            EXIT_EMA
            if setup_id in self._closing
            else EXIT_TARGET
            if closed_by_target(setup_id)
            else EXIT_STOP
        )
        self._closing.discard(setup_id)
        setup = self.watched.pop(setup_id, None)
        if setup is not None:
            setup.status = SetupStatus.CLOSED
            setup.result = result_from_profit(profit)
            setup.profit = profit
            setup.exit_reason = reason
            if exit_price is not None:
                setup.exit_price = exit_price
            setup.exit_time = now_utc()
            setup.touch(setup.exit_time)
            # A broker SL or manual close: the machine still holds the BUY.
            self._release(setup)
            logger.info(
                "trade closed %s %s BUY -> %s %.2f (%s)",
                setup_id,
                setup.asset,
                setup.result.value,
                profit,
                reason,
            )
            if self.database is not None:
                self.database.save_setup(setup)
        self.notifier.send_trade_result(position, profit, reason, setup, exit_price)

    # ---------------------------------------------------------- helpers
    def _release(self, setup: Setup) -> None:
        if self._on_trade_closed is not None:
            self._on_trade_closed(setup)

    def _save(self, setup: Setup, kind: SignalKind, detail: str) -> None:
        if self.database is not None:
            self.database.save_setup(setup)
            self.database.save_signal(setup, kind, detail)


def result_from_profit(profit: float) -> Result:
    """WIN/LOSS from the realised basket P/L (break-even counts as UNKNOWN)."""
    if profit > 0.01:
        return Result.WIN
    if profit < -0.01:
        return Result.LOSS
    return Result.UNKNOWN
