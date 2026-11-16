"""End-to-end proof of the signal pipeline.

Feeds a scripted price path through the REAL components:

    candles -> SignalEngine -> EmaStateMachine -> SignalManager
            -> Notifier -> Telegram

The path is a downtrend (32 EMA on top), a flat stretch where the EMAs
converge, an uptrend (32 EMA crosses under 7 and 14 -> BUY) and a downtrend
(32 EMA crosses back above 14 and 7 -> BUY EXIT). No order is ever placed.

Dry run (prints the exact Telegram text, sends nothing):
    python scripts/test_signal_pipeline.py

Really send the messages to the channel:
    python scripts/test_signal_pipeline.py --send
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):  # emoji-safe output on Windows pipes
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.settings import load_settings  # noqa: E402
from config.strategy import timeframe_seconds  # noqa: E402
from src.data.deriv import DERIV_SOURCE  # noqa: E402
from src.data.models import Candle  # noqa: E402
from src.signals.generator import SignalEngine  # noqa: E402
from src.signals.manager import SignalManager  # noqa: E402
from src.signals.models import SignalKind  # noqa: E402
from src.telegram.bot import build_bot  # noqa: E402
from src.telegram.notifier import Notifier  # noqa: E402
from src.utils.logging import setup_logging  # noqa: E402
from src.utils.time import floor_to_timeframe, now_utc  # noqa: E402

ASSET = "Volatility 25 Index"
PAD = 0.3


def cycle_closes(start: float = 1000.0) -> list[float]:
    closes: list[float] = []
    price = start
    for _ in range(110):
        price -= 0.6
        closes.append(price)
    for i in range(30):
        closes.append(price + (0.05 if i % 2 else -0.05))
    for _ in range(25):
        price += 0.8
        closes.append(price)
    for _ in range(40):
        price -= 0.8
        closes.append(price)
    return closes


def build_candles(timeframe: str, closes: list[float]) -> list[Candle]:
    seconds = timeframe_seconds(timeframe)
    start = floor_to_timeframe(now_utc(), seconds) - timedelta(seconds=seconds * len(closes))
    candles: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        candles.append(
            Candle(
                asset=ASSET,
                timeframe=timeframe,
                timestamp=start + timedelta(seconds=seconds * index),
                open=round(previous, 3),
                high=round(max(previous, close) + PAD, 3),
                low=round(min(previous, close) - PAD, 3),
                close=round(close, 3),
                volume=12.0,
                source=DERIV_SOURCE,
            )
        )
        previous = close
    return candles


def main() -> int:
    parser = argparse.ArgumentParser(description="Signal pipeline end-to-end test")
    parser.add_argument("--send", action="store_true", help="really send to Telegram")
    parser.add_argument(
        "--timeframe", choices=["1m", "2m", "3m", "5m", "10m", "15m", "30m"], default="5m"
    )
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings.log_level)

    bot = build_bot(settings)
    if args.send and not bot.configured:
        print("Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return 1

    notifier = Notifier(
        bot=bot,
        display_timezone=settings.display_timezone,
        notify_invalidations=settings.notify_invalidations,
        notify_convergence=settings.notify_convergence,
        enabled=args.send,
    )
    engine = SignalEngine(
        assets=[ASSET],
        timeframes=[args.timeframe],
        source=DERIV_SOURCE,
        required_source=DERIV_SOURCE,
    )
    manager = SignalManager(notifier, database=None, on_trade_closed=engine.release_position)

    if args.send:
        bot.try_send_message(
            "🧪 PIPELINE TEST\n\n"
            "Driving the Triple EMA strategy with a scripted price path.\n"
            "The next messages are produced by the real signal logic.\n\n"
            "No trade is executed."
        )

    seen: list[SignalKind] = []
    for candle in build_candles(args.timeframe, cycle_closes()):
        events = engine.on_closed_candle(candle)
        for event in events:
            seen.append(event.kind)
            print(f">>> {event.kind.value}: {event.detail} ({event.setup.setup_id})")
        manager.handle(events)

    if not notifier.enabled:
        print("\n--- TELEGRAM MESSAGES THAT WOULD BE SENT ---")
        for _, text in notifier.outbox:
            print(f"\n{'-' * 50}\n{text}")

    expected = [SignalKind.CONVERGENCE, SignalKind.EXECUTION, SignalKind.EXIT]
    ok = all(kind in seen for kind in expected)
    print(f"\n{'=' * 62}")
    print(f"PIPELINE: {'SIGNAL WORKING' if ok else 'INCOMPLETE'}")
    print(f"Telegram delivery: {'live' if args.send else 'dry run (use --send)'}")
    print("Trades placed: 0 (this script never trades)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
