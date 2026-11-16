"""Verifies the Telegram configuration and the signal message pipeline.

Usage:
    python scripts/test_telegram.py
    python scripts/test_telegram.py --signals
    python scripts/test_telegram.py --signals --asset "EUR/USD" --timeframe 1m

`--signals` sends the real execution message built from a simulated setup
that uses the LIVE Deriv price, so the exact Telegram output can be
reviewed before real signals appear. No trade is ever placed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import load_settings  # noqa: E402
from src.signals.models import Setup, SetupStatus, build_setup_id  # noqa: E402
from src.telegram.bot import TelegramError, build_bot  # noqa: E402
from src.telegram.notifier import Notifier  # noqa: E402
from src.utils.logging import setup_logging  # noqa: E402
from src.utils.time import now_utc  # noqa: E402


def pip_size(asset: str) -> float:
    if "/" not in asset:
        return 0.001
    return 0.01 if "JPY" in asset.upper() else 0.0001


async def live_price(settings, asset: str) -> tuple[float, str]:
    """One real Deriv price. Falls back to a static price if offline."""
    from src.data.base import ProviderError
    from src.data.deriv import DERIV_SOURCE, DerivDataProvider

    provider = DerivDataProvider(settings.deriv_ws_url)
    try:
        await provider.connect()
        await provider.subscribe([asset])
        stream = provider.stream_prices()
        try:
            tick = await stream.__anext__()
        finally:
            await stream.aclose()
        return (tick.price, DERIV_SOURCE)
    except ProviderError as exc:
        print(f"Warning: live price unavailable ({exc}); using a simulated price")
    finally:
        await provider.disconnect()
    return (1.16717, "SIMULATED (Deriv unavailable)")


def build_demo_setup(asset: str, timeframe: str, price: float, source: str) -> Setup:
    """A realistic EMA BUY setup around `price` (nothing is traded)."""
    pip = pip_size(asset)
    moment = now_utc()
    setup = Setup(
        setup_id=build_setup_id(asset, timeframe, moment),
        asset=asset,
        timeframe=timeframe,
        source=source,
    )
    setup.status = SetupStatus.OPEN
    setup.convergence_time = moment - timedelta(minutes=10)
    setup.cross_fast_time = moment - timedelta(minutes=5)
    setup.cross_mid_time = moment
    setup.entry_confirmation_time = moment
    setup.entry_price = round(price, 5)
    setup.entry_emas = {
        "fast": round(price - 2 * pip, 5),
        "mid": round(price - 4 * pip, 5),
        "slow": round(price - 6 * pip, 5),
    }
    setup.stop_loss = round(price - 15 * pip, 5)
    return setup


def send_signal_sequence(settings, bot, asset: str, timeframe: str) -> int:
    price, source = asyncio.run(live_price(settings, asset))
    print(f"Reference price: {price} (source: {source})")

    setup = build_demo_setup(asset, timeframe, price, source)
    notifier = Notifier(
        bot=bot,
        display_timezone=settings.display_timezone,
        notify_invalidations=settings.notify_invalidations,
        enabled=True,
    )

    bot.try_send_message(
        "🧪 TEST SEQUENCE\n\n"
        "The next message shows the real execution signal format\n"
        "using a simulated setup.\n\n"
        f"Asset: {asset}\nTimeframe: {timeframe.upper()}\nPrice source: {source}\n\n"
        "This is NOT a live signal and no trade is executed."
    )

    moment = now_utc()
    results = {
        "EXECUTION": notifier.send_execution_signal(setup, moment),
    }
    duplicate_blocked = not notifier.send_execution_signal(setup, moment)

    print("\n--- SIGNAL DELIVERY ---")
    for label, ok in results.items():
        print(f"{label}: {'sent' if ok else 'FAILED'}")
    print(f"Duplicate suppression: {'working' if duplicate_blocked else 'NOT WORKING'}")
    print(f"Setup ID: {setup.setup_id}")
    return 0 if all(results.values()) and duplicate_blocked else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram delivery test")
    parser.add_argument(
        "--signals",
        action="store_true",
        help="also send the execution signal message using a live Deriv price",
    )
    parser.add_argument("--asset", default="Volatility 25 Index")
    parser.add_argument(
        "--timeframe",
        default="5m",
        choices=["1m", "2m", "3m", "5m", "10m", "15m", "30m"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    bot = build_bot(settings)

    if not bot.configured:
        print("Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return 1

    try:
        me = bot.get_me()
    except TelegramError as exc:
        print(f"Telegram connection failed: {exc}")
        return 1

    print("Telegram connection successful.")
    print(f"Bot: @{me.get('username')} (id {me.get('id')})")
    print(f"Target chat id: {bot.chat_id}")

    try:
        chat = bot.get_chat()
        print(f"Chat: {chat.get('title') or chat.get('username')} ({chat.get('type')})")
    except TelegramError as exc:
        print(f"Warning: could not read chat info: {exc}")

    try:
        bot.send_message(
            "✅ Deriv Triple EMA Signal Bot\n\n"
            "Telegram connection test successful.\n"
            "This bot only sends analysis signals and never executes trades."
        )
    except TelegramError as exc:
        print(f"Test message failed: {exc}")
        return 1

    print("Test message sent successfully.")

    if args.signals:
        print("\n--- SENDING EXECUTION SIGNAL ---")
        return send_signal_sequence(settings, bot, args.asset, args.timeframe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
