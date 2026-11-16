"""Setup and signal models for the Triple EMA Convergence strategy."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from config.assets import asset_slug, deriv_symbol
from src.utils.time import ensure_utc, now_utc


class Direction(str, Enum):
    BUY = "BUY"


class SetupStatus(str, Enum):
    #: The 3 EMAs intersected; waiting for a bullish resolution.
    ACTIVE = "ACTIVE"
    #: BUY confirmed - position open, waiting for the exit crosses.
    OPEN = "OPEN"
    #: The BUY has been closed (EMA exit, stop-loss or manual close).
    CLOSED = "CLOSED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class SignalKind(str, Enum):
    CONVERGENCE = "CONVERGENCE"
    EXECUTION = "EXECUTION"
    EXIT = "EXIT"
    STOPPED = "STOPPED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class Result(str, Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    UNKNOWN = "UNKNOWN"


#: Exit reasons reported in result messages and stored in the database.
EXIT_EMA = "BUY EXIT - EMA REVERSAL (32 EMA above 7 and 14)"
EXIT_STOP = "Stop-loss (swing low) or manual close"
EXIT_TARGET = "Profit target reached (PERCENTAGE_PROFIT % of BASE_AMOUNT)"
EXIT_ORDER_FAILED = "MT5 order failed - no position opened"


def build_setup_id(asset: str, timeframe: str, moment: datetime) -> str:
    """e.g. R25-5M-EMA-20261021-120500 (<= 31 chars, fits the MT5 comment)."""
    return (
        f"{asset_slug(deriv_symbol(asset))}-{timeframe.upper()}-EMA-"
        f"{moment.strftime('%Y%m%d-%H%M%S')}"
    )


@dataclass
class Setup:
    """One convergence setup, and - once confirmed - its BUY trade."""

    setup_id: str
    asset: str
    timeframe: str
    source: str
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)
    state: str = "WAITING_FOR_BULLISH_RESOLUTION"
    status: SetupStatus = SetupStatus.ACTIVE
    #: Crosses are only recorded on candles at/after this timestamp.
    window_start: datetime | None = None
    convergence_time: datetime | None = None
    convergence_spread_pct: float | None = None
    convergence_emas: dict[str, float] = field(default_factory=dict)
    #: Candles at which the 32 EMA crossed under the 7 / 14 EMA
    #: (informational - the bullish intersection itself triggers the BUY).
    cross_fast_time: datetime | None = None
    cross_mid_time: datetime | None = None
    #: Open time of the closed candle that confirmed the BUY.
    entry_confirmation_time: datetime | None = None
    #: Proposed entry: the confirming candle's close.
    entry_price: float | None = None
    entry_emas: dict[str, float] = field(default_factory=dict)
    #: Stop-loss at the most recent swing low below the entry.
    stop_loss: float | None = None
    swing_low_time: datetime | None = None
    #: Candles at which the 32 EMA crossed back above the 14 / 7 EMA.
    exit_cross_mid_time: datetime | None = None
    exit_cross_fast_time: datetime | None = None
    exit_signal_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    exit_time: datetime | None = None
    profit: float | None = None
    result: Result = Result.UNKNOWN
    invalidation_reason: str | None = None
    candles_seen: int = 0
    #: MT5 position tickets of the basket.
    tickets: list[int] = field(default_factory=list)

    @property
    def direction(self) -> Direction:
        return Direction.BUY

    @property
    def is_open(self) -> bool:
        return self.status is SetupStatus.OPEN

    def touch(self, moment: datetime | None = None) -> None:
        self.updated_at = moment or now_utc()

    @property
    def move(self) -> float | None:
        """Price distance from entry to exit (positive = in profit)."""
        if self.entry_price is None or self.exit_price is None:
            return None
        return self.exit_price - self.entry_price

    def explanation(self) -> dict:
        """Answers 'why did this signal occur?'."""
        return {
            "convergence": _iso(self.convergence_time),
            "spread %": self.convergence_spread_pct,
            "32 under 7": _iso(self.cross_fast_time),
            "32 under 14": _iso(self.cross_mid_time),
            "EMAs at entry": self.entry_emas,
            "entry price": self.entry_price,
            "stop-loss (swing low)": self.stop_loss,
            "swing low candle": _iso(self.swing_low_time),
            "data source": self.source,
        }

    def as_row(self) -> dict:
        return {
            "setup_id": self.setup_id,
            "asset": self.asset,
            "timeframe": self.timeframe,
            "direction": self.direction.value,
            "state": self.state,
            "status": self.status.value,
            "window_start": _iso(self.window_start),
            "convergence_time": _iso(self.convergence_time),
            "convergence_spread_pct": self.convergence_spread_pct,
            "convergence_emas": json.dumps(self.convergence_emas),
            "cross_fast_time": _iso(self.cross_fast_time),
            "cross_mid_time": _iso(self.cross_mid_time),
            "entry_confirmation_time": _iso(self.entry_confirmation_time),
            "entry_price": self.entry_price,
            "entry_emas": json.dumps(self.entry_emas),
            "stop_loss": self.stop_loss,
            "swing_low_time": _iso(self.swing_low_time),
            "exit_cross_mid_time": _iso(self.exit_cross_mid_time),
            "exit_cross_fast_time": _iso(self.exit_cross_fast_time),
            "exit_signal_time": _iso(self.exit_signal_time),
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "exit_time": _iso(self.exit_time),
            "profit": self.profit,
            "result": self.result.value,
            "invalidation_reason": self.invalidation_reason,
            "tickets": json.dumps(self.tickets),
            "source": self.source,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

    @classmethod
    def from_row(cls, row: dict) -> "Setup":
        """Rebuild a setup saved with `as_row` (used to restore open trades)."""
        return cls(
            setup_id=row["setup_id"],
            asset=row["asset"],
            timeframe=row["timeframe"],
            source=row.get("source") or "unknown",
            created_at=_dt(row.get("created_at")) or now_utc(),
            updated_at=_dt(row.get("updated_at")) or now_utc(),
            state=row.get("state") or "",
            status=SetupStatus(row["status"]),
            window_start=_dt(row.get("window_start")),
            convergence_time=_dt(row.get("convergence_time")),
            convergence_spread_pct=row.get("convergence_spread_pct"),
            convergence_emas=json.loads(row.get("convergence_emas") or "{}"),
            cross_fast_time=_dt(row.get("cross_fast_time")),
            cross_mid_time=_dt(row.get("cross_mid_time")),
            entry_confirmation_time=_dt(row.get("entry_confirmation_time")),
            entry_price=row.get("entry_price"),
            entry_emas=json.loads(row.get("entry_emas") or "{}"),
            stop_loss=row.get("stop_loss"),
            swing_low_time=_dt(row.get("swing_low_time")),
            exit_cross_mid_time=_dt(row.get("exit_cross_mid_time")),
            exit_cross_fast_time=_dt(row.get("exit_cross_fast_time")),
            exit_signal_time=_dt(row.get("exit_signal_time")),
            exit_price=row.get("exit_price"),
            exit_reason=row.get("exit_reason"),
            exit_time=_dt(row.get("exit_time")),
            profit=row.get("profit"),
            result=Result(row.get("result") or "UNKNOWN"),
            invalidation_reason=row.get("invalidation_reason"),
            tickets=json.loads(row.get("tickets") or "[]"),
        )


@dataclass
class SignalEvent:
    kind: SignalKind
    setup: Setup
    timestamp: datetime
    detail: str = ""


def result_from_move(move: float | None) -> Result:
    if move is None or move == 0:
        return Result.UNKNOWN
    return Result.WIN if move > 0 else Result.LOSS


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return ensure_utc(datetime.fromisoformat(value)) if value else None
