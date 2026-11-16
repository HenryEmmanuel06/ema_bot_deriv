"""Application entrypoint.

Modes (README 62):
  BACKTEST    - historical candles, no live signals
  PAPER       - live data, real-time signals, hypothetical outcomes
  LIVE_SIGNAL - live data, signals sent to Telegram

With TAKE_TRADE=false (the default) no orders are placed. With
TAKE_TRADE=true, BUY execution signals open POSITIONS_PER_TRADE positions on
the configured Deriv MT5 account through a running MetaTrader5 terminal.
"""

from __future__ import annotations

import argparse
import asyncio

from config.assets import all_assets
from config.settings import Settings, load_settings
from config.strategy import STRATEGY_CONFIG
from src.data.base import MarketDataProvider, ProviderError
from src.data.candle_builder import MultiTimeframeCandleBuilder
from src.data.deriv import DERIV_SOURCE, DerivAuthError
from src.data.provider import build_provider
from src.execution.mt5_executor import MT5Executor
from src.signals.generator import SignalEngine
from src.signals.manager import SignalManager
from src.storage.database import Database
from src.telegram.bot import build_bot
from src.telegram.notifier import Notifier
from src.utils.logging import get_logger, setup_logging
from src.utils.time import now_utc

logger = get_logger(__name__)

RECONNECT_DELAYS = (5, 10, 20, 30, 60)
#: How often the "still alive and waiting" line is logged.
HEARTBEAT_SECONDS = 300


class SignalBot:
    def __init__(self, settings: Settings, assets: list[str] | None = None) -> None:
        self.settings = settings
        self.assets = assets or all_assets()
        self.provider: MarketDataProvider = build_provider(
            settings.market_data_provider, settings=settings
        )
        self.engine = SignalEngine(
            assets=self.assets,
            timeframes=list(settings.enabled_timeframes),
            source=self.provider.source,
            required_source=DERIV_SOURCE if settings.is_live_data_mode else None,
            # With MT5 trading the broker-side SL closes the trade; without
            # it the swing-low stop is simulated on closed candles.
            simulate_stops=not settings.take_trade,
        )
        self.builders = MultiTimeframeCandleBuilder(
            self.assets, self.engine.required_timeframes, source=self.provider.source
        )
        self.database = Database(settings.database_path)
        self.notifier = Notifier(
            bot=build_bot(settings),
            display_timezone=settings.display_timezone,
            notify_invalidations=settings.notify_invalidations,
            notify_execution=settings.notify_execution,
            notify_convergence=settings.notify_convergence,
            enabled=settings.app_mode in ("PAPER", "LIVE_SIGNAL"),
        )

        self.executor = (
            MT5Executor(
                login=settings.mt5_login,
                password=settings.mt5_password,
                server=settings.mt5_server,
                positions=settings.positions_per_trade,
                lot=settings.mt5_lot,
                deviation=settings.mt5_deviation,
                path=settings.mt5_path,
                base_amount=settings.base_amount,
                profit_target_pct=settings.percentage_profit,
            )
            if settings.take_trade
            else None
        )
        self.manager = SignalManager(
            self.notifier,
            self.database,
            self.executor,
            on_trade_closed=self.engine.release_position,
        )
        self._running = False
        self._started_at = now_utc()
        self._ticks = 0
        self._candles = 0
        self._signals = 0
        self._heartbeat: asyncio.Task | None = None

    # ------------------------------------------------------------- startup
    def _validate(self) -> None:
        problems = self.settings.validate()
        if self.settings.is_live_data_mode and self.provider.source != DERIV_SOURCE:
            problems.append(
                f"Provider {self.provider.name!r} is not Deriv; "
                "live signals require Deriv market data"
            )
        unsupported = [
            tf
            for tf in self.engine.required_timeframes
            if self.provider.native_timeframes and tf not in self.provider.native_timeframes
        ]
        if unsupported and not self.provider.supports_streaming:
            problems.append(
                f"Provider {self.provider.name} cannot supply {unsupported}. "
                "Disable those timeframes or use another provider."
            )
        if problems:
            for problem in problems:
                logger.error("configuration problem: %s", problem)
            raise SystemExit(1)

    def _restore_open_trades(self) -> None:
        """Reconcile saved open BUYs with MT5 before any new trade."""
        saved = self.database.open_setups()
        if not saved:
            return
        still_open = self.manager.restore(saved)
        self.engine.restore(still_open)
        logger.info(
            "restored %s of %s saved open BUY(s) for exit monitoring",
            len(still_open),
            len(saved),
        )

    async def _seed_history(self) -> None:
        limit = int(STRATEGY_CONFIG["min_candles_for_analysis"]) * 3
        for asset in self.assets:
            for timeframe in self.engine.required_timeframes:
                try:
                    candles = await self.provider.get_candles(asset, timeframe, limit)
                except (ProviderError, Exception) as exc:  # noqa: BLE001
                    logger.warning("history unavailable for %s %s: %s", asset, timeframe, exc)
                    continue
                self.engine.seed(candles)
                logger.info("seeded %s %s with %s candles", asset, timeframe, len(candles))

    # ----------------------------------------------------------------- run
    async def run(self) -> None:
        self._validate()
        if self.executor is not None and not self.executor.connect():
            raise SystemExit(1)
        self.database.connect()
        cfg = STRATEGY_CONFIG
        logger.info(
            "starting in %s mode | provider=%s | assets=%s | timeframes=%s | "
            "EMA %s/%s/%s (%s) spread<=%s%% lookback=%s max-age=%s",
            self.settings.app_mode,
            self.provider.name,
            len(self.assets),
            list(self.settings.enabled_timeframes),
            cfg["ema_fast"],
            cfg["ema_mid"],
            cfg["ema_slow"],
            cfg["ema_price_source"],
            cfg["max_ema_spread_pct"],
            cfg["convergence_lookback"],
            cfg["setup_max_age_candles"],
        )
        if self.settings.take_trade:
            logger.info(
                "TAKE_TRADE=true - BUY signals will open %s MT5 positions each "
                "(lot=%s, SL at the most recent swing low, exit when the 32 EMA "
                "crosses above the 14 and 7 EMAs, deviation=%s)",
                self.settings.positions_per_trade,
                self.settings.mt5_lot or "symbol-minimum",
                self.settings.mt5_deviation,
            )
            if self.settings.percentage_profit > 0:
                logger.info(
                    "profit target: close at %.1f%% of base %.2f; SL trails "
                    "10%% behind each 10%% profit milestone",
                    self.settings.percentage_profit,
                    self.settings.base_amount,
                )
        else:
            logger.info("signals only - no orders will be placed")
        self._restore_open_trades()
        self._running = True
        self._started_at = now_utc()
        self._heartbeat = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        attempt = 0
        while self._running:
            try:
                await self.provider.connect()
                await self.provider.subscribe(self.assets)
                await self._seed_history()
                # Providers that pause the stream during history requests
                # re-issue their live subscriptions here (Deriv is a no-op).
                await self.provider.resubscribe()
                attempt = 0
                await self._consume()
            except asyncio.CancelledError:
                raise
            except DerivAuthError as exc:
                logger.error("Deriv authentication failed: %s", exc)
                logger.error("Check DERIV_API_KEY in your .env file and restart.")
                self._running = False
                return
            except Exception as exc:  # noqa: BLE001 - resilience requirement
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                attempt += 1
                logger.error(
                    "market data error (DATA_STATUS=DISCONNECTED): %s - reconnecting in %ss",
                    exc,
                    delay,
                )
                for asset in self.assets:
                    for tf in self.settings.enabled_timeframes:
                        self.engine.mark_unreliable(asset, tf, True)
                # Resynchronise candle state: partially built candles from before
                # the disconnect must never reach the strategy.
                self.builders.reset()
                await asyncio.sleep(delay)
            finally:
                await self.provider.disconnect()

    async def _consume(self) -> None:
        for asset in self.assets:
            for tf in self.settings.enabled_timeframes:
                self.engine.mark_unreliable(asset, tf, False)
        async for tick in self.provider.stream_prices():
            self._ticks += 1
            for candle in self.builders.add_tick(tick):
                self._process_candle(candle)
            self.manager.on_tick()

    def _process_candle(self, candle) -> None:
        unreliable = self.builders.is_unreliable(candle.asset, candle.timeframe)
        self.engine.mark_unreliable(candle.asset, candle.timeframe, unreliable)
        self.database.save_candle(candle)
        self._candles += 1
        self._dispatch(self.engine.on_closed_candle(candle))

    def _dispatch(self, events) -> None:
        if not events:
            return
        self._signals += len(events)
        self.manager.handle(events)

    # --------------------------------------------------------- heartbeat
    async def _heartbeat_loop(self) -> None:
        """Periodic proof of life: quiet channels are normal, silence is not."""
        while self._running:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if not self._running:
                return
            uptime = now_utc() - self._started_at
            active = self.engine.active_setups()
            positions = self.engine.open_positions()
            status = getattr(self.provider, "data_status", "UNKNOWN")
            logger.info(
                "heartbeat | uptime %s | DATA_STATUS=%s | ticks=%s candles=%s "
                "signals=%s | setups=%s open BUYs=%s%s",
                _duration(uptime),
                status,
                self._ticks,
                self._candles,
                self._signals,
                len(active),
                len(positions),
                "".join(
                    f"\n    {s.asset} {s.timeframe} {s.state} ({s.setup_id})"
                    for s in active + positions
                ),
            )

    async def stop(self) -> None:
        self._running = False
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            self._heartbeat = None
        await self.provider.disconnect()
        if self.executor is not None:
            self.executor.shutdown()
        self.database.close()


def _duration(delta) -> str:
    total = int(delta.total_seconds())
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h{minutes:02d}m{seconds:02d}s"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Triple EMA Convergence signal + MT5 bot")
    parser.add_argument("--mode", choices=["BACKTEST", "PAPER", "LIVE_SIGNAL"], default=None)
    parser.add_argument("--provider", default=None, help="deriv | mock | csv")
    parser.add_argument("--assets", nargs="*", default=None)
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    settings = load_settings()
    overrides = {}
    if args.mode:
        overrides["app_mode"] = args.mode
    if args.provider:
        overrides["market_data_provider"] = args.provider
    if overrides:
        from dataclasses import replace

        settings = replace(settings, **overrides)

    setup_logging(settings.log_level, settings.log_dir)

    if settings.app_mode == "BACKTEST":
        logger.info("BACKTEST mode: run scripts/backtest.py instead")
        return

    bot = SignalBot(settings, assets=args.assets)
    try:
        await bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("stopped by user")


if __name__ == "__main__":
    main()
