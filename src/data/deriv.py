"""Deriv market-data provider (the ONLY live data source).

Uses Deriv's public WebSocket, which needs no API key, app id or login:
``wss://api.derivws.com/trading/v1/options/ws/public``
(developers.deriv.com -> Options Setup -> WebSocket public).

Calls used (developers.deriv.com -> Market Data / System):

- ``{"active_symbols": "brief"}`` -> rows carrying ``underlying_symbol`` and
  ``underlying_symbol_name``; used to verify and name the configured assets
- ``{"ticks": <symbol>, "subscribe": 1}`` -> ``msg_type: "tick"`` with
  ``{"epoch", "quote", "symbol"}``; candles are built locally from these ticks
- ``{"ticks_history": <symbol>, "style": "candles", "granularity": <s>,
  "count": <n>, "end": "latest"}`` -> ``msg_type: "candles"`` rows of
  ``{"epoch", "open", "high", "low", "close"}``; ``epoch`` is the open time and
  the newest row is the candle that is still forming
- ``{"time": 1}`` -> server epoch, used to tell closed candles from forming ones
- ``{"ping": 1}`` -> keep-alive (outside the rate-limit budget)

This provider is read-only. It never sends a trading call (``buy``,
``proposal``, ...) - the bot only consumes market data.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, AsyncIterator

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from config.assets import deriv_symbol
from config.strategy import timeframe_seconds
from src.data.base import MarketDataProvider, ProviderError
from src.data.models import Candle, Tick
from src.utils.logging import get_logger
from src.utils.time import UTC

logger = get_logger(__name__)

#: Every candle/tick produced from this provider carries this source tag.
DERIV_SOURCE = "DERIV"

#: Public Deriv WebSocket: market data without any API key or login.
DERIV_PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"

KEEP_ALIVE_SECONDS = 30.0
REQUEST_TIMEOUT = 20.0
#: How often the tick queue is polled while waiting for a tick.
SILENCE_POLL_SECONDS = 1.0
#: Synthetic indices tick every 1-2 seconds around the clock, so a minute of
#: silence means the stream is dead.
MAX_SILENCE_SECONDS = 60.0


class DerivAuthError(ProviderError):
    """Authentication failed. Unused on the public endpoint (no key needed)."""


@dataclass(frozen=True)
class DerivSymbol:
    """One entry of Deriv's `active_symbols` list."""

    symbol: str
    name: str
    is_open: bool
    pip_size: float | None = None


def parse_active_symbols(rows: Any) -> dict[str, DerivSymbol]:
    """`active_symbols` rows -> {symbol: DerivSymbol}."""
    catalogue: dict[str, DerivSymbol] = {}
    if not isinstance(rows, list):
        return catalogue
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("underlying_symbol") or row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        catalogue[symbol] = DerivSymbol(
            symbol=symbol,
            name=str(row.get("underlying_symbol_name") or row.get("display_name") or symbol),
            is_open=bool(row.get("exchange_is_open", 1)) and not row.get("is_trading_suspended"),
            pip_size=row.get("pip_size"),
        )
    return catalogue


def candles_from_history(
    rows: Any, asset: str, timeframe: str, server_epoch: float
) -> tuple[list[Candle], Candle | None]:
    """Split a `ticks_history` candles reply into closed candles and the forming one."""
    seconds = timeframe_seconds(timeframe)
    closed: list[Candle] = []
    forming: Candle | None = None
    if not isinstance(rows, list):
        return closed, forming
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            epoch = int(row["epoch"])
            open_, high = float(row["open"]), float(row["high"])
            low, close = float(row["low"]), float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if epoch % seconds:
            continue
        candle = Candle(
            asset=asset,
            timeframe=timeframe,
            timestamp=datetime.fromtimestamp(epoch, tz=UTC),
            open=open_,
            high=max(high, open_, close),
            low=min(low, open_, close),
            close=close,
            volume=None,
            source=DERIV_SOURCE,
        )
        if epoch + seconds > server_epoch:
            forming = candle
        else:
            closed.append(candle)
    closed.sort(key=lambda c: c.timestamp)
    return closed, forming


class DerivClient:
    """Minimal JSON request/response + tick-stream client for Deriv."""

    def __init__(
        self,
        url: str = DERIV_PUBLIC_WS_URL,
        connect_timeout: float = 15.0,
        queue_size: int = 20_000,
    ) -> None:
        self.url = url
        self.connect_timeout = connect_timeout
        self.ticks: asyncio.Queue[Tick] = asyncio.Queue(maxsize=queue_size)
        self.status = "DISCONNECTED"
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._keep_alive: asyncio.Task | None = None
        self._closing = False
        self._req_ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._symbol_to_asset: dict[str, str] = {}
        self._last_tick: dict[str, tuple[int, float]] = {}
        #: Server clock minus local clock, in seconds.
        self.clock_offset = 0.0
        self.ticks_received = 0

    @property
    def connected(self) -> bool:
        return self.status == "CONNECTED"

    def server_time(self) -> float:
        return time.time() + self.clock_offset

    # ------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        self._closing = False
        logger.info("Connecting to Deriv (%s)", self.url.split("//", 1)[-1].split("/", 1)[0])
        try:
            self._ws = await ws_connect(
                self.url,
                open_timeout=self.connect_timeout,
                close_timeout=5,
                ping_interval=None,  # application-level {"ping": 1} instead
                max_size=None,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Could not connect to Deriv: {exc}") from exc
        self.status = "CONNECTED"
        self._reader = asyncio.create_task(self._read_loop(), name="deriv-reader")
        self._keep_alive = asyncio.create_task(self._keep_alive_loop(), name="deriv-keep-alive")
        await self.sync_clock()
        logger.info("Deriv connected (clock offset %+.1fs)", self.clock_offset)

    async def sync_clock(self) -> None:
        try:
            reply = await self.request({"time": 1})
            self.clock_offset = float(reply["time"]) - time.time()
        except (ProviderError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Deriv server time unavailable, using local clock: %s", exc)
            self.clock_offset = 0.0

    async def disconnect(self) -> None:
        self._closing = True
        for task in (self._keep_alive, self._reader):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._keep_alive = self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        self.status = "DISCONNECTED"
        self._fail_pending("Deriv connection closed")

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ProviderError(reason))
        self._pending.clear()

    async def _keep_alive_loop(self) -> None:
        try:
            while not self._closing and self._ws is not None:
                await asyncio.sleep(KEEP_ALIVE_SECONDS)
                try:
                    await self._send({"ping": 1})
                except Exception as exc:  # noqa: BLE001 - the reader reports the loss
                    logger.debug("Deriv keep-alive could not be sent: %s", exc)
                    return
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------- messages
    async def _send(self, payload: dict) -> None:
        if self._ws is None:
            raise ProviderError("Deriv socket is not connected")
        await self._ws.send(json.dumps(payload))

    async def request(self, payload: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
        """Send one request and wait for the reply carrying its `req_id`."""
        req_id = next(self._req_ids)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._send({**payload, "req_id": req_id})
            reply = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ProviderError(f"Deriv did not answer {_describe(payload)} in {timeout:.0f}s") from exc
        finally:
            self._pending.pop(req_id, None)
        error = reply.get("error")
        if error:
            raise ProviderError(
                f"Deriv rejected {_describe(payload)}: "
                f"{error.get('code', 'Error')} - {error.get('message', error)}"
            )
        return reply

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    self._handle(json.loads(raw))
                except Exception as exc:  # noqa: BLE001 - malformed message guard
                    logger.warning("Discarded malformed Deriv message: %s", exc)
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Deriv reader stopped: %s", exc)
        finally:
            self.status = "DISCONNECTED"
            self._fail_pending("Deriv connection lost")
            if not self._closing:
                logger.warning("Deriv connection lost - DATA_STATUS=DISCONNECTED")

    def _handle(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        if message.get("msg_type") == "tick" and isinstance(message.get("tick"), dict):
            self._handle_tick(message["tick"])
        req_id = message.get("req_id")
        future = self._pending.pop(req_id, None) if isinstance(req_id, int) else None
        if future is not None and not future.done():
            future.set_result(message)

    def _handle_tick(self, body: dict) -> None:
        symbol = body.get("symbol")
        try:
            epoch = int(body["epoch"])
            price = float(body["quote"])
        except (KeyError, TypeError, ValueError):
            return
        if not isinstance(symbol, str):
            return
        if self._last_tick.get(symbol) == (epoch, price):  # duplicate tick protection
            return
        self._last_tick[symbol] = (epoch, price)
        tick = Tick(
            asset=self._symbol_to_asset.get(symbol, symbol),
            timestamp=datetime.fromtimestamp(epoch, tz=UTC),
            price=price,
            source=DERIV_SOURCE,
        )
        self.ticks_received += 1
        try:
            self.ticks.put_nowait(tick)
        except asyncio.QueueFull:
            logger.warning("Deriv tick queue full - dropping oldest tick")
            try:
                self.ticks.get_nowait()
                self.ticks.put_nowait(tick)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    # ---------------------------------------------------------------- public
    async def active_symbols(self) -> dict[str, DerivSymbol]:
        reply = await self.request({"active_symbols": "brief"})
        return parse_active_symbols(reply.get("active_symbols"))

    async def subscribe_ticks(self, symbol: str, asset: str) -> None:
        self._symbol_to_asset[symbol] = asset
        await self.request({"ticks": symbol, "subscribe": 1})
        logger.info("Subscribed to Deriv ticks for %s (%s)", asset, symbol)

    async def candles(self, symbol: str, granularity: int, count: int) -> list[dict]:
        reply = await self.request(
            {
                "ticks_history": symbol,
                "style": "candles",
                "granularity": int(granularity),
                "count": int(count),
                "end": "latest",
            }
        )
        return reply.get("candles") or []


class DerivDataProvider(MarketDataProvider):
    """Live market data straight from Deriv. Read-only, never trades."""

    name = "deriv"
    source = DERIV_SOURCE
    #: Candles are always built from Deriv's own tick stream.
    native_timeframes = ()
    supports_streaming = True

    def __init__(self, url: str = DERIV_PUBLIC_WS_URL) -> None:
        self.client = DerivClient(url)
        self.assets: list[str] = []
        #: display name -> verified Deriv symbol
        self.symbols: dict[str, str] = {}
        self.catalogue: dict[str, DerivSymbol] = {}
        self._forming: dict[tuple[str, str], Candle] = {}

    @property
    def data_status(self) -> str:
        return self.client.status

    async def connect(self) -> None:
        await self.client.connect()

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def subscribe(self, assets: list[str]) -> None:
        """Subscribe to each asset's ticks, skipping symbols Deriv does not list."""
        try:
            self.catalogue = await self.client.active_symbols()
        except ProviderError as exc:
            logger.warning("Deriv symbol list unavailable, symbols unverified: %s", exc)
            self.catalogue = {}
        self.assets = []
        for asset in assets:
            symbol = self._resolve_symbol(asset)
            if symbol is None:
                continue
            try:
                await self.client.subscribe_ticks(symbol, asset)
            except ProviderError as exc:
                logger.error("Skipping %s: %s", asset, exc)
                continue
            self.symbols[asset] = symbol
            self.assets.append(asset)
        if not self.assets:
            raise ProviderError("No configured asset could be subscribed on Deriv")

    def _resolve_symbol(self, asset: str) -> str | None:
        """Configured symbol (or the asset itself as a symbol), verified by Deriv."""
        wanted = deriv_symbol(asset) or asset.strip()
        if not self.catalogue:
            return wanted
        info = self.catalogue.get(wanted) or next(
            (
                entry
                for entry in self.catalogue.values()
                if wanted.lower() in (entry.symbol.lower(), entry.name.lower())
            ),
            None,
        )
        if info is None:
            logger.error(
                "Skipping %s: %r is not in Deriv's active symbols. "
                "Check DERIV_ASSETS in .env or DERIV_SYMBOLS in config/assets.py",
                asset,
                wanted,
            )
            return None
        if not info.is_open:
            logger.warning("%s (%s) is currently closed on Deriv", asset, info.symbol)
        else:
            logger.info("%s -> %s", asset, info.symbol)
        return info.symbol

    async def get_candles(self, asset: str, timeframe: str, limit: int) -> list[Candle]:
        """Closed Deriv candles; the still-forming one is kept for `forming_candle`."""
        seconds = timeframe_seconds(timeframe)
        rows = await self.client.candles(self.symbols.get(asset, asset), seconds, limit + 1)
        closed, forming = candles_from_history(rows, asset, timeframe, self.client.server_time())
        if forming is not None:
            self._forming[(asset, timeframe)] = forming
        if not closed:
            raise ProviderError(f"Deriv returned no usable {timeframe} history for {asset}")
        return closed[-limit:]

    def forming_candle(self, asset: str, timeframe: str) -> Candle | None:
        """The forming candle from the last history request, if still current."""
        candle = self._forming.pop((asset, timeframe), None)
        if candle is None:
            return None
        ends = candle.timestamp + timedelta(seconds=timeframe_seconds(timeframe))
        return candle if ends.timestamp() > self.client.server_time() else None

    async def stream_prices(self) -> AsyncIterator[Tick]:
        if not self.client.connected:
            raise ProviderError("Deriv provider is not connected")
        silent_for = 0.0
        while True:
            if not self.client.connected:
                raise ProviderError("Deriv connection lost (DATA_STATUS=DISCONNECTED)")
            try:
                tick = await asyncio.wait_for(self.client.ticks.get(), timeout=SILENCE_POLL_SECONDS)
            except asyncio.TimeoutError as exc:
                silent_for += SILENCE_POLL_SECONDS
                if silent_for >= MAX_SILENCE_SECONDS:
                    raise ProviderError(
                        f"No Deriv price updates received for {int(MAX_SILENCE_SECONDS)}s"
                    ) from exc
                continue
            silent_for = 0.0
            yield tick


def _describe(payload: dict) -> str:
    """Short request label for errors, e.g. `ticks_history R_75`."""
    key = next(iter(payload), "request")
    value = payload.get(key)
    return f"{key} {value}" if isinstance(value, str) else key
