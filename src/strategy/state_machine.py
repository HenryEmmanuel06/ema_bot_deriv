"""Triple EMA Convergence state machine.

One machine per (asset, timeframe); state is never shared between them.
Everything is evaluated on closed candles only.

    WAITING_FOR_CONVERGENCE
        -> CONVERGENCE_DETECTED             (EMAs 7/14/32 intersect)
        -> WAITING_FOR_BULLISH_RESOLUTION   (intersection resolves upward)
        -> BUY_CONFIRMED                    (EMA7 > EMA14 > EMA32)
        -> BUY_OPEN
        -> WAITING_FOR_32_EMA_EXIT_CROSSES  (32 over 14 and 7 since entry)
        -> CLOSE_BUY
        -> WAITING_FOR_CONVERGENCE

The exit crosses are recomputed from the candle history on every candle, so
they survive restarts and reconnects as long as the history covers the
trade.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from config.strategy import STRATEGY_CONFIG, timeframe_seconds
from src.data.models import Candle
from src.signals.models import (
    EXIT_EMA,
    EXIT_STOP,
    Setup,
    SetupStatus,
    SignalEvent,
    SignalKind,
    build_setup_id,
    result_from_move,
)
from src.strategy.ema import EmaSet, compute_emas, slow_crosses_over, slow_crosses_under
from src.strategy.stop_loss import most_recent_swing_low
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SetupState(str, Enum):
    WAITING_FOR_CONVERGENCE = "WAITING_FOR_CONVERGENCE"
    CONVERGENCE_DETECTED = "CONVERGENCE_DETECTED"
    WAITING_FOR_BULLISH_RESOLUTION = "WAITING_FOR_BULLISH_RESOLUTION"
    BUY_CONFIRMED = "BUY_CONFIRMED"
    BUY_OPEN = "BUY_OPEN"
    WAITING_FOR_32_EMA_EXIT_CROSSES = "WAITING_FOR_32_EMA_EXIT_CROSSES"
    CLOSE_BUY = "CLOSE_BUY"
    INVALID = "INVALID"
    EXPIRED = "EXPIRED"


class EmaStateMachine:
    def __init__(
        self,
        asset: str,
        timeframe: str,
        source: str = "unknown",
        history_size: int = 600,
        simulate_stops: bool = True,
        config: dict | None = None,
    ) -> None:
        self.asset = asset
        self.timeframe = timeframe
        self.source = source
        self.history_size = history_size
        #: Close a BUY when a closed candle's low reaches the stop-loss. On
        #: with no broker (signal-only / backtest); off when trading, where
        #: the broker-side SL closes the basket and the manager calls
        #: `release()`.
        self.simulate_stops = simulate_stops
        self.cfg = config or STRATEGY_CONFIG
        self.candles: list[Candle] = []
        #: Convergence setup waiting for the intersection to resolve bullish.
        self.active: Setup | None = None
        #: The open BUY trade.
        self.position: Setup | None = None
        self.data_unreliable = False
        #: New convergence windows must start after this candle, so a setup
        #: that ended (or a closed trade) can never be re-used.
        self._last_end: datetime | None = None

    @property
    def state(self) -> SetupState:
        if self.position is not None:
            return SetupState.WAITING_FOR_32_EMA_EXIT_CROSSES
        if self.active is not None:
            return SetupState.WAITING_FOR_BULLISH_RESOLUTION
        return SetupState.WAITING_FOR_CONVERGENCE

    # ------------------------------------------------------------------ API
    def seed(self, candles: list[Candle]) -> None:
        """Load historical closed candles without generating signals."""
        for candle in candles:
            if self.candles and candle.timestamp <= self.candles[-1].timestamp:
                continue
            self.candles.append(candle)
        self._trim()

    def restore(self, setup: Setup) -> None:
        """Resume exit monitoring of a BUY that was open before a restart."""
        setup.status = SetupStatus.OPEN
        setup.state = SetupState.WAITING_FOR_32_EMA_EXIT_CROSSES.value
        self.position = setup
        self.active = None
        logger.info(
            "%s %s restored open BUY %s (entry %s, SL %s)",
            self.asset,
            self.timeframe,
            setup.setup_id,
            setup.entry_price,
            setup.stop_loss,
        )

    def release(self, setup_id: str) -> bool:
        """Forget the open BUY (closed by the broker, manually, or never
        opened). Returns True when it was the machine's position."""
        if self.position is None or self.position.setup_id != setup_id:
            return False
        self.position = None
        self._last_end = self.candles[-1].timestamp if self.candles else None
        logger.info("%s %s position %s released", self.asset, self.timeframe, setup_id)
        return True

    def on_closed_candle(self, candle: Candle) -> list[SignalEvent]:
        if self.candles and candle.timestamp <= self.candles[-1].timestamp:
            logger.debug("%s %s duplicate/late candle ignored", self.asset, self.timeframe)
            return []
        self.candles.append(candle)
        self._trim()
        if len(self.candles) < int(self.cfg["min_candles_for_analysis"]):
            return []

        emas = compute_emas(
            self.candles,
            self.cfg["ema_fast"],
            self.cfg["ema_mid"],
            self.cfg["ema_slow"],
            self.cfg["ema_price_source"],
        )
        if self.position is not None:
            # Exits are still managed while the stream is unreliable.
            return self._manage_position(candle, emas)

        if self.data_unreliable:
            logger.warning(
                "%s %s DATA_UNRELIABLE - new entries suspended", self.asset, self.timeframe
            )
            return []

        events: list[SignalEvent] = []
        if self.active is not None:
            events.extend(self._advance(candle, emas))
        if self.active is None and self.position is None:
            events.extend(self._scan(candle, emas))
        return events

    # ---------------------------------------------------------- convergence
    def _scan(self, candle: Candle, emas: EmaSet) -> list[SignalEvent]:
        n = len(self.candles)
        lookback = int(self.cfg["convergence_lookback"])
        window = [
            i
            for i in range(max(1, n - lookback), n)
            if self._last_end is None or self.candles[i].timestamp > self._last_end
        ]
        if not window:
            return []
        limit = float(self.cfg["max_ema_spread_pct"])
        converged = [
            i for i in window if emas.spread_pct(i, self.candles[i].close) <= limit
        ]
        if not converged:
            return []
        start = window[0]
        # The 32 EMA must still be able to cross under: it may not already
        # sit below both faster EMAs before the window.
        before = start - 1
        if emas.slow[before] < emas.fast[before] and emas.slow[before] < emas.mid[before]:
            return []

        at = converged[-1]
        setup = Setup(
            setup_id=build_setup_id(self.asset, self.timeframe, candle.timestamp),
            asset=self.asset,
            timeframe=self.timeframe,
            source=self.source,
            state=SetupState.WAITING_FOR_BULLISH_RESOLUTION.value,
            window_start=self.candles[start].timestamp,
            convergence_time=self.candles[at].timestamp,
            convergence_spread_pct=round(emas.spread_pct(at, self.candles[at].close), 5),
            convergence_emas=_rounded(emas.values(at)),
        )
        setup.touch(candle.timestamp)
        self.active = setup
        logger.info(
            "%s %s EMA convergence detected (spread %.4f%%, EMAs %s) id=%s",
            self.asset,
            self.timeframe,
            setup.convergence_spread_pct,
            setup.convergence_emas,
            setup.setup_id,
        )
        events = [
            SignalEvent(
                SignalKind.CONVERGENCE, setup, self._close_time(candle), "EMA convergence"
            )
        ]
        # The intersection may already have resolved bullish on this candle.
        events.extend(self._evaluate(candle, emas))
        return events

    # -------------------------------------------------------- entry trigger
    def _advance(self, candle: Candle, emas: EmaSet) -> list[SignalEvent]:
        setup = self.active
        assert setup is not None
        setup.candles_seen += 1
        setup.touch(candle.timestamp)
        if setup.candles_seen > int(self.cfg["setup_max_age_candles"]):
            return [
                self._end_setup(
                    setup,
                    candle,
                    SetupStatus.EXPIRED,
                    "setup expired without a bullish resolution",
                )
            ]
        return self._evaluate(candle, emas)

    def _evaluate(self, candle: Candle, emas: EmaSet) -> list[SignalEvent]:
        """The BUY executes on the intersection of the 3 EMAs: the confirming
        closed candle must be ordered EMA7 > EMA14 > EMA32 *and* the three
        MAs must have met (spread within MAX_EMA_SPREAD_PCT) on one of the
        last CONVERGENCE_LOOKBACK candles. A bullish ordering without a
        fresh intersection never fires - the MAs must have met first. A
        bearish ordering that has already separated kills the setup."""
        setup = self.active
        assert setup is not None
        last = len(self.candles) - 1
        limit = float(self.cfg["max_ema_spread_pct"])
        tangled = emas.spread_pct(last, candle.close) <= limit
        if not tangled and emas.bearish(last):
            # Away from the intersection a bearish ordering means it
            # resolved downward.
            return [
                self._end_setup(
                    setup, candle, SetupStatus.INVALIDATED,
                    "the 3-EMA intersection resolved bearish before entry",
                )
            ]
        if not emas.bullish(last):
            # The intersection has not resolved into a bullish ordering yet.
            return []
        # The three MAs must actually have met: a clustered candle within
        # the last CONVERGENCE_LOOKBACK candles of this setup's window.
        lookback = int(self.cfg["convergence_lookback"])
        first = max(
            1,
            self._index_at_or_after(setup.window_start),
            last - lookback + 1,
        )
        met = any(
            emas.spread_pct(i, self.candles[i].close) <= limit
            for i in range(first, last + 1)
        )
        if not met:
            # Bullish ordering away from the intersection - the MAs have
            # not met, so this is a runaway move, not an intersection BUY.
            return []
        # Record where the fast EMAs crossed up through the 32 EMA for
        # traceability only - the intersection itself triggers the BUY.
        for attr, series in (
            ("cross_fast_time", emas.fast),
            ("cross_mid_time", emas.mid),
        ):
            index = self._last_cross(emas.slow, series, first, last)
            if index is not None:
                setattr(setup, attr, self.candles[index].timestamp)
        return [self._confirm_buy(setup, candle, emas)]

    def _confirm_buy(self, setup: Setup, candle: Candle, emas: EmaSet) -> SignalEvent:
        last = len(self.candles) - 1
        setup.state = SetupState.BUY_CONFIRMED.value
        setup.entry_confirmation_time = candle.timestamp
        setup.entry_price = candle.close
        setup.entry_emas = _rounded(emas.values(last))
        swing = most_recent_swing_low(
            self.candles,
            candle.close,
            int(self.cfg["swing_lookback"]),
            int(self.cfg["sl_search_candles"]),
        )
        if swing is not None:
            setup.stop_loss, setup.swing_low_time = swing[0], swing[1].timestamp
        setup.status = SetupStatus.OPEN
        setup.state = SetupState.BUY_OPEN.value
        setup.touch(candle.timestamp)
        self.active = None
        self.position = setup
        logger.info(
            "%s %s BUY confirmed - %s | entry %s | SL (swing low) %s | EMAs %s",
            self.asset,
            self.timeframe,
            setup.setup_id,
            setup.entry_price,
            setup.stop_loss,
            setup.entry_emas,
        )
        setup.state = SetupState.WAITING_FOR_32_EMA_EXIT_CROSSES.value
        return SignalEvent(
            SignalKind.EXECUTION, setup, self._close_time(candle), "BUY confirmed"
        )

    # ----------------------------------------------------------------- exit
    def _manage_position(self, candle: Candle, emas: EmaSet) -> list[SignalEvent]:
        setup = self.position
        assert setup is not None
        entry_time = setup.entry_confirmation_time
        if entry_time is not None and candle.timestamp <= entry_time:
            return []
        setup.touch(candle.timestamp)

        if (
            self.simulate_stops
            and setup.stop_loss is not None
            and candle.low <= setup.stop_loss
        ):
            return [self._close_position(setup, candle, setup.stop_loss, EXIT_STOP)]

        last = len(self.candles) - 1
        first = max(1, self._index_after(entry_time))
        over_mid = self._first_cross_over(emas.slow, emas.mid, first, last)
        over_fast = self._first_cross_over(emas.slow, emas.fast, first, last)
        if over_mid is not None and setup.exit_cross_mid_time is None:
            setup.exit_cross_mid_time = self.candles[over_mid].timestamp
        if over_fast is not None and setup.exit_cross_fast_time is None:
            setup.exit_cross_fast_time = self.candles[over_fast].timestamp
        if over_mid is None or over_fast is None:
            return []
        if not (emas.slow[last] > emas.mid[last] and emas.slow[last] > emas.fast[last]):
            return []
        return [self._close_position(setup, candle, candle.close, EXIT_EMA)]

    def _close_position(
        self, setup: Setup, candle: Candle, price: float, reason: str
    ) -> SignalEvent:
        setup.state = SetupState.CLOSE_BUY.value
        setup.exit_signal_time = self._close_time(candle)
        setup.exit_price = price
        setup.exit_reason = reason
        setup.result = result_from_move(setup.move)
        self.position = None
        self._last_end = candle.timestamp
        kind = SignalKind.EXIT if reason == EXIT_EMA else SignalKind.STOPPED
        logger.info(
            "%s %s %s - %s at %s (entry %s)",
            self.asset,
            self.timeframe,
            kind.value,
            setup.setup_id,
            price,
            setup.entry_price,
        )
        return SignalEvent(kind, setup, setup.exit_signal_time, reason)

    # --------------------------------------------------------------- helpers
    def _end_setup(
        self, setup: Setup, candle: Candle, status: SetupStatus, reason: str
    ) -> SignalEvent:
        setup.status = status
        setup.state = (
            SetupState.EXPIRED.value if status is SetupStatus.EXPIRED else SetupState.INVALID.value
        )
        setup.invalidation_reason = reason
        setup.touch(candle.timestamp)
        self.active = None
        self._last_end = candle.timestamp
        logger.info(
            "%s %s setup %s: %s (%s)",
            self.asset,
            self.timeframe,
            status.value.lower(),
            reason,
            setup.setup_id,
        )
        kind = SignalKind.EXPIRED if status is SetupStatus.EXPIRED else SignalKind.INVALIDATED
        return SignalEvent(kind, setup, self._close_time(candle), reason)

    @staticmethod
    def _last_cross(slow: list[float], other: list[float], first: int, last: int) -> int | None:
        for index in range(last, first - 1, -1):
            if slow_crosses_under(slow, other, index):
                return index
        return None

    @staticmethod
    def _first_cross_over(
        slow: list[float], other: list[float], first: int, last: int
    ) -> int | None:
        for index in range(first, last + 1):
            if slow_crosses_over(slow, other, index):
                return index
        return None

    def _index_at_or_after(self, moment: datetime | None) -> int:
        if moment is None:
            return 0
        for index, candle in enumerate(self.candles):
            if candle.timestamp >= moment:
                return index
        return len(self.candles)

    def _index_after(self, moment: datetime | None) -> int:
        if moment is None:
            return 0
        for index, candle in enumerate(self.candles):
            if candle.timestamp > moment:
                return index
        return len(self.candles)

    def _close_time(self, candle: Candle) -> datetime:
        return candle.timestamp + timedelta(seconds=timeframe_seconds(candle.timeframe))

    def _trim(self) -> None:
        if len(self.candles) > self.history_size:
            del self.candles[: len(self.candles) - self.history_size]


def _rounded(values: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 5) for key, value in values.items()}
