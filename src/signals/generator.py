"""Signal generation engine: candles in, signal events out."""

from __future__ import annotations

from src.data.models import Candle
from src.signals.models import Setup, SignalEvent
from src.strategy.state_machine import EmaStateMachine
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SignalEngine:
    """Owns one EMA state machine per (asset, signal timeframe)."""

    def __init__(
        self,
        assets: list[str],
        timeframes: list[str],
        source: str = "unknown",
        history_size: int = 600,
        required_source: str | None = None,
        simulate_stops: bool = True,
    ) -> None:
        self.assets = list(assets)
        self.analysis_timeframes = list(timeframes)
        self.source = source
        self.history_size = history_size
        #: When set, candles from any other data source are rejected.
        self.required_source = required_source
        self.rejected_candles = 0
        self.history: dict[tuple[str, str], list[Candle]] = {}
        self.machines: dict[tuple[str, str], EmaStateMachine] = {
            (asset, tf): EmaStateMachine(
                asset,
                tf,
                source=source,
                history_size=history_size,
                simulate_stops=simulate_stops,
            )
            for asset in self.assets
            for tf in self.analysis_timeframes
        }

    # ------------------------------------------------------------- helpers
    @property
    def required_timeframes(self) -> list[str]:
        return list(self.analysis_timeframes)

    def _candles(self, asset: str, timeframe: str) -> list[Candle]:
        return self.history.get((asset, timeframe), [])

    def _store(self, candle: Candle) -> bool:
        key = (candle.asset, candle.timeframe)
        bucket = self.history.setdefault(key, [])
        if bucket and candle.timestamp <= bucket[-1].timestamp:
            return False
        bucket.append(candle)
        if len(bucket) > self.history_size:
            del bucket[: len(bucket) - self.history_size]
        return True

    def _is_source_allowed(self, candle: Candle) -> bool:
        if self.required_source is None or candle.source == self.required_source:
            return True
        self.rejected_candles += 1
        logger.error(
            "Rejected %s %s candle from source %r (only %r is allowed)",
            candle.asset,
            candle.timeframe,
            candle.source,
            self.required_source,
        )
        return False

    # ---------------------------------------------------------------- API
    def seed(self, candles: list[Candle]) -> None:
        candles = [c for c in candles if self._is_source_allowed(c)]
        for candle in candles:
            self._store(candle)
        if candles:
            key = (candles[0].asset, candles[0].timeframe)
            machine = self.machines.get(key)
            if machine is not None:
                machine.seed(self.history[key])

    def restore(self, setups: list[Setup]) -> None:
        """Hand BUYs that were open before a restart back to their machine."""
        for setup in setups:
            machine = self.machines.get((setup.asset, setup.timeframe))
            if machine is None:
                logger.warning(
                    "open BUY %s on %s %s is not monitored (asset/timeframe not enabled)",
                    setup.setup_id,
                    setup.asset,
                    setup.timeframe,
                )
                continue
            machine.restore(setup)

    def release_position(self, setup: Setup) -> bool:
        machine = self.machines.get((setup.asset, setup.timeframe))
        return machine is not None and machine.release(setup.setup_id)

    def mark_unreliable(self, asset: str, timeframe: str, unreliable: bool) -> None:
        machine = self.machines.get((asset, timeframe))
        if machine is not None and machine.data_unreliable != unreliable:
            machine.data_unreliable = unreliable
            logger.warning(
                "%s %s data stream marked %s",
                asset,
                timeframe,
                "DATA_UNRELIABLE" if unreliable else "HEALTHY",
            )

    def on_closed_candle(self, candle: Candle) -> list[SignalEvent]:
        if not self._is_source_allowed(candle):
            return []
        if not self._store(candle):
            return []
        machine = self.machines.get((candle.asset, candle.timeframe))
        if machine is None:
            return []
        return machine.on_closed_candle(candle)

    def active_setups(self) -> list[Setup]:
        return [m.active for m in self.machines.values() if m.active is not None]

    def open_positions(self) -> list[Setup]:
        return [m.position for m in self.machines.values() if m.position is not None]
