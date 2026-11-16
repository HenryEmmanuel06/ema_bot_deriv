"""Telegram message formatting for the Triple EMA Convergence strategy.

Messages stay concise: technical detail belongs in the logs/database.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from config.strategy import STRATEGY_CONFIG
from src.signals.models import EXIT_EMA, Setup
from src.utils.time import format_display

if TYPE_CHECKING:
    from src.execution.mt5_executor import OpenPosition


def format_price(asset: str, price: float | None) -> str:
    if price is None:
        return "-"
    # Forex pairs keep their conventional quoting; Deriv indices print 3 dp.
    if "/" in asset:
        decimals = 3 if "JPY" in asset.upper() else 5
    else:
        decimals = 3
    return f"{price:.{decimals}f}"


def _tf(timeframe: str) -> str:
    return timeframe.upper()


def _labels() -> tuple[str, str, str]:
    cfg = STRATEGY_CONFIG
    return f"EMA {cfg['ema_fast']}", f"EMA {cfg['ema_mid']}", f"EMA {cfg['ema_slow']}"


def _ema_block(setup: Setup, emas: dict[str, float]) -> str:
    fast, mid, slow = _labels()
    return (
        f"{fast}: {format_price(setup.asset, emas.get('fast'))}\n"
        f"{mid}: {format_price(setup.asset, emas.get('mid'))}\n"
        f"{slow}: {format_price(setup.asset, emas.get('slow'))}\n"
    )


def _time(moment: datetime | None, tz: str) -> str:
    return format_display(moment, tz) if moment else "-"


def _footer(setup: Setup, timestamp: datetime, tz: str) -> str:
    return (
        f"Setup ID:\n{setup.setup_id}\n\n"
        f"Data Source: {setup.source}\n"
        f"Time: {format_display(timestamp, tz)}"
    )


def _header(setup: Setup) -> str:
    return f"Asset: {setup.asset}\nTimeframe: {_tf(setup.timeframe)}\n\n"


# ------------------------------------------------------------ phase 1
def format_convergence(setup: Setup, timestamp: datetime, tz: str = "UTC") -> str:
    return (
        "🔵 EMA CONVERGENCE\n\n"
        f"{_header(setup)}"
        f"{_ema_block(setup, setup.convergence_emas)}"
        f"Spread: {setup.convergence_spread_pct or 0:.4f}% "
        f"(max {STRATEGY_CONFIG['max_ema_spread_pct']}%)\n\n"
        "Waiting for the intersection to resolve into 7 > 14 > 32.\n\n"
        f"{_footer(setup, timestamp, tz)}"
    )


# ------------------------------------------------------------ phase 2
def format_execution_signal(
    setup: Setup, timestamp: datetime, tz: str = "UTC", trading: bool = False
) -> str:
    fast, mid, slow = _labels()
    outcome = (
        "⏳ Opening the BUY basket on MT5..."
        if trading
        else "⚠️ Strategy signal only. No trade is executed by this bot."
    )
    return (
        "🟢 BUY EXECUTION SIGNAL\n\n"
        f"{_header(setup)}"
        f"32 under {fast.split()[1]}: {_time(setup.cross_fast_time, tz)}\n"
        f"32 under {mid.split()[1]}: {_time(setup.cross_mid_time, tz)}\n\n"
        f"{fast} > {mid} > {slow} ✓\n"
        f"{_ema_block(setup, setup.entry_emas)}\n"
        f"Entry Price:\n{format_price(setup.asset, setup.entry_price)}\n\n"
        f"🛑 SL (swing low): {format_price(setup.asset, setup.stop_loss)}\n"
        f"🏁 Exit: 32 EMA crosses above {fast.split()[1]} and {mid.split()[1]}\n\n"
        "➡️ Direction: BUY\n\n"
        f"{_footer(setup, timestamp, tz)}\n\n"
        f"{outcome}"
    )


# ------------------------------------------------------------ phase 3
def format_buy_opened(
    setup: Setup, trades: list[OpenPosition], timestamp: datetime, tz: str = "UTC"
) -> str:
    if not trades:
        return (
            "❌ BUY ORDER FAILED\n\n"
            f"{_header(setup)}"
            "No MT5 position was opened - see logs.\n\n"
            f"{_footer(setup, timestamp, tz)}"
        )
    first = trades[0]
    count = len(trades)
    tickets = ", ".join(str(t.ticket) for t in trades)
    average = sum(t.entry * t.volume for t in trades) / sum(t.volume for t in trades)
    return (
        "✅ BUY OPENED\n\n"
        f"{_header(setup)}"
        f"📦 Positions: {count} x {first.volume}\n"
        f"Actual Entry: {format_price(setup.asset, average)}\n"
        f"🛑 SL (swing low): {format_price(setup.asset, first.sl)}\n"
        f"🎫 Tickets: {tickets}\n\n"
        f"{_footer(setup, timestamp, tz)}"
    )


# ------------------------------------------------------------- exit
def format_exit_signal(
    setup: Setup, timestamp: datetime, tz: str = "UTC", trading: bool = False
) -> str:
    fast, mid, _slow = _labels()
    is_ema = setup.exit_reason == EXIT_EMA
    header = "🔴 BUY EXIT — EMA REVERSAL" if is_ema else "🛑 BUY STOP-LOSS HIT"
    crosses = (
        f"32 above {mid.split()[1]}: {_time(setup.exit_cross_mid_time, tz)}\n"
        f"32 above {fast.split()[1]}: {_time(setup.exit_cross_fast_time, tz)}\n\n"
        if is_ema
        else ""
    )
    if trading:
        tickets = ", ".join(str(t) for t in setup.tickets) or "-"
        detail = (
            f"🎫 Tickets: {tickets}\n\n"
            "Closing all strategy BUY positions at market - the realised P/L "
            "follows in the TRADE RESULT message."
        )
    else:
        move = setup.move
        sign = "+" if move is not None and move >= 0 else "-"
        detail = (
            f"Move: {sign}{format_price(setup.asset, abs(move)) if move is not None else '-'}"
            f" ({setup.result.value})\n\n"
            "⚠️ Strategy signal only. No trade is executed by this bot."
        )
    return (
        f"{header}\n\n"
        f"{_header(setup)}"
        f"{crosses}"
        f"Entry: {format_price(setup.asset, setup.entry_price)}\n"
        f"SL: {format_price(setup.asset, setup.stop_loss)}\n"
        f"Exit Price: {format_price(setup.asset, setup.exit_price)}\n"
        f"Exit Signal: {_time(setup.exit_signal_time, tz)}\n\n"
        f"{detail}\n\n"
        f"Setup ID:\n{setup.setup_id}"
    )


def format_trade_result(
    position: OpenPosition,
    profit: float,
    reason: str,
    timestamp: datetime,
    tz: str = "UTC",
    setup: Setup | None = None,
    exit_price: float | None = None,
) -> str:
    """The one result message, sent when the whole basket has closed."""
    amount = f"{'+' if profit >= 0 else '-'}${abs(profit):.2f}"
    if profit > 0.01:
        header = "✅ TRADE RESULT: WIN"
    elif profit < -0.01:
        header = "❌ TRADE RESULT: LOSS"
    else:
        header = "⚪ TRADE RESULT: BREAK-EVEN"
    timeframe = f"Timeframe: {_tf(setup.timeframe)}\n" if setup is not None else ""
    tickets = (
        f"Tickets: {', '.join(str(t) for t in setup.tickets)}\n"
        if setup is not None and setup.tickets
        else ""
    )
    return (
        f"{header} {amount}\n\n"
        f"Asset: {position.symbol}\n"
        f"{timeframe}"
        f"Direction: {'BUY' if position.is_buy else 'SELL'}\n"
        f"Positions closed: {position.basket_size}\n"
        f"Entry: {format_price(position.symbol, position.entry)}\n"
        f"SL: {format_price(position.symbol, position.sl)}\n"
        f"Exit Fill: {format_price(position.symbol, exit_price)}\n"
        f"{tickets}\n"
        f"Closed by: {reason}\n\n"
        f"Setup ID:\n{position.setup_id}\n\n"
        f"Time: {format_display(timestamp, tz)}"
    )


def format_invalidated_signal(setup: Setup, timestamp: datetime, tz: str = "UTC") -> str:
    return (
        f"⚪ SETUP {setup.status.value}\n\n"
        f"{_header(setup)}"
        f"Reason: {setup.invalidation_reason}\n\n"
        f"{_footer(setup, timestamp, tz)}"
    )
