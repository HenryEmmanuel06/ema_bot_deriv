"""MetaTrader5 execution: Deriv MT5 synthetic-index CFDs.

Each BUY execution signal opens a basket of ``positions`` (POSITIONS_PER_TRADE)
market BUY orders. Every position carries a broker-side stop-loss at the most
recent swing low below the entry, so the stop still fires while the bot is
offline. Two exits exist: the bot closes the whole basket at market once the
32 EMA has crossed back above both the 14 EMA and the 7 EMA (see
``SignalManager``), and - when BASE_AMOUNT + PERCENTAGE_PROFIT are set - a
profit target closes the basket once its floating profit reaches
PERCENTAGE_PROFIT % of BASE_AMOUNT. The first 10% of floating profit moves
the SL to the level halfway between the entry and the initial stop (the
risk is halved); every further 10% milestone trails the SL to lock the
profit 10% behind (20% -> +10%, 30% -> +20%, ...). Only positions tagged
with this strategy's magic number and tracked by ticket are ever closed.
Deriv MT5 lists
synthetic indices under the bot's display names ("Volatility 75 Index"),
so assets map 1:1.

Requires the Windows-only ``MetaTrader5`` package and an MT5 terminal that is
installed, running and logged into a Deriv MT5 account with Algo Trading on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from config.assets import DERIV_NAMES
from src.signals.models import Direction, Setup
from src.utils.logging import get_logger

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

logger = get_logger(__name__)

#: Tags every order so this strategy's positions are identifiable in MT5.
MAGIC = 20261021

#: Ticks take a moment to flow after subscribing to a symbol that was not in
#: Market Watch; poll this long before declaring "no quote".
QUOTE_WAIT_SECONDS = 5.0


@dataclass
class OpenPosition:
    """An MT5 position opened for a signal, managed to its exit."""

    ticket: int
    symbol: str
    volume: float
    is_buy: bool
    entry: float
    sl: float
    setup_id: str
    #: 1-based index within the basket, for "Position 2/5" messages.
    index: int = 1
    basket_size: int = 1


@dataclass
class TradeEvent:
    """A lifecycle event on an open position, reported to Telegram."""

    kind: str  # "closed" | "basket_closed"
    position: OpenPosition
    profit: float
    #: Closing fill price (volume-weighted over the basket for "basket_closed").
    exit_price: float | None = None


class MT5Executor:
    def __init__(
        self,
        login: int,
        password: str,
        server: str,
        positions: int = 5,
        lot: float = 0.0,
        deviation: int = 30,
        path: str = "",
        base_amount: float = 0.0,
        profit_target_pct: float = 0.0,
        trail_step_pct: float = 10.0,
    ) -> None:
        self.login = login
        self.password = password
        self.server = server
        self.lot = lot
        self.deviation = deviation
        self.path = path
        self.positions_per_trade = max(1, positions)
        #: Floating profit is measured as a % of base_amount; the basket
        #: closes at profit_target_pct and the SL locks trail_step_pct
        #: behind each milestone. Disabled when either value is <= 0.
        self.base_amount = base_amount
        self.profit_target_pct = profit_target_pct
        self.trail_step_pct = trail_step_pct
        self._open: dict[int, OpenPosition] = {}
        #: setup_id -> still-open tickets, and realised P/L already seen.
        self._baskets: dict[str, set[int]] = {}
        self._basket_profits: dict[str, float] = {}
        #: setup_id -> [(exit price, volume)] of positions already closed.
        self._basket_exits: dict[str, list[tuple[float, float]]] = {}
        #: setup_id -> the profit milestone the trailed SL already reached
        #: (1 = SL halved to the entry, 2 = +10% locked, 3 = +20%, ...).
        self._basket_locks: dict[str, float] = {}
        #: setup_ids whose basket was closed at the profit target (the
        #: manager consumes this once for the result's "Closed by" line).
        self._target_closed: set[str] = set()

    @property
    def open_count(self) -> int:
        return len(self._open)

    # ------------------------------------------------------------ lifecycle
    def connect(self) -> bool:
        """Attach to the running MT5 terminal and log in to the account."""
        if mt5 is None:
            logger.error(
                "MetaTrader5 package is not installed - run: pip install MetaTrader5"
            )
            return False
        kwargs = {"path": self.path} if self.path else {}
        if not mt5.initialize(**kwargs):
            logger.error(
                "MT5 initialize failed: %s - is the MT5 terminal installed?",
                mt5.last_error(),
            )
            return False
        if not mt5.login(self.login, password=self.password, server=self.server):
            logger.error(
                "MT5 login failed: %s - check MT5_LOGIN / MT5_PASSWORD / MT5_SERVER",
                mt5.last_error(),
            )
            mt5.shutdown()
            return False
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        if account is not None:
            logger.info(
                "MT5 connected | login=%s server=%s balance=%s %s",
                account.login,
                account.server,
                account.balance,
                account.currency,
            )
        if terminal is not None and not terminal.trade_allowed:
            logger.warning(
                "MT5 algo trading is disabled - enable 'Algo Trading' in the terminal"
            )
        return True

    def shutdown(self) -> None:
        """Detach from the terminal; open positions keep their broker SL.

        The EMA exit needs the bot running - positions left open are only
        protected by their swing-low stop until the bot is restarted, when
        `reconcile()` picks them up again.
        """
        if mt5 is None:
            return
        if self._open:
            logger.warning(
                "stopping with %s open MT5 positions (SL remains active on "
                "the broker, the EMA exit resumes after restart): %s",
                len(self._open),
                ", ".join(str(ticket) for ticket in self._open),
            )
        mt5.shutdown()

    # -------------------------------------------------------------- trading
    def execute(self, setup: Setup) -> list[OpenPosition]:
        """Open the POSITIONS_PER_TRADE market positions for one signal."""
        if mt5 is None:
            return []
        if setup.stop_loss is None:
            logger.error(
                "no swing-low stop-loss for %s - refusing to trade without an SL",
                setup.setup_id,
            )
            return []
        opened: list[OpenPosition] = []
        for index in range(1, self.positions_per_trade + 1):
            position = self._open_one(setup, index)
            if position is not None:
                opened.append(position)
        if opened:
            self._track_basket(setup.setup_id, opened)
        return opened

    def _track_basket(self, setup_id: str, positions: list[OpenPosition]) -> None:
        self._baskets[setup_id] = {p.ticket for p in positions}
        self._basket_profits[setup_id] = 0.0
        self._basket_exits[setup_id] = []
        self._basket_locks[setup_id] = 0.0  # last profit milestone applied

    def _resolve_mt5_symbol(self, asset: str):
        """Return (symbol, symbol_info) trying the display name first, then
        the raw asset string. Some Deriv MT5 servers list symbols as
        'Volatility 75 Index', others as 'R_75'.  Returns (candidate, None)
        with an error log when neither form is found in the terminal."""
        if mt5 is None:
            return asset, None
        primary = mt5_symbol(asset)
        info = mt5.symbol_info(primary)
        if info is not None:
            return primary, info
        if primary != asset:
            info = mt5.symbol_info(asset)
            if info is not None:
                logger.info(
                    "MT5 symbol: display name %r not found, using raw symbol %r",
                    primary,
                    asset,
                )
                return asset, info
        logger.error(
            "MT5 symbol not found for asset %r - tried %r and %r. "
            "Check that the symbol is visible in your MT5 Market Watch.",
            asset,
            primary,
            asset,
        )
        return primary, None

    def _open_one(self, setup: Setup, index: int) -> OpenPosition | None:
        """Open a single market BUY with its SL at the most recent swing low."""
        if mt5 is None:
            return None
        symbol, info = self._resolve_mt5_symbol(setup.asset)
        if info is None:
            return None
        if info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
            logger.error(
                "MT5 %s is not tradable on this account - cannot execute %s",
                symbol,
                setup.setup_id,
            )
            return None
        tick = self._quote(info)
        if tick is None:
            logger.error(
                "MT5 no quote for %s within %ss (%s) - cannot execute %s",
                symbol,
                QUOTE_WAIT_SECONDS,
                mt5.last_error(),
                setup.setup_id,
            )
            return None
        is_buy = setup.direction is Direction.BUY
        volume = self._volume(info)
        entry = tick.ask if is_buy else tick.bid
        sl = _stop_level(info, entry, setup.stop_loss, is_buy)
        result = mt5.order_send(
            {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                "price": entry,
                "sl": sl,
                "tp": 0.0,
                "deviation": self.deviation,
                "magic": MAGIC,
                "comment": setup.setup_id[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": _filling(info),
            }
        )
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                "MT5 order failed %s %s %s: retcode=%s comment=%s",
                setup.direction.value,
                volume,
                symbol,
                getattr(result, "retcode", None),
                getattr(result, "comment", ""),
            )
            return None
        position_id = self._position_id(result)
        position = OpenPosition(
            ticket=position_id,
            symbol=symbol,
            volume=volume,
            is_buy=is_buy,
            entry=result.price,
            sl=sl,
            setup_id=setup.setup_id,
            index=index,
            basket_size=self.positions_per_trade,
        )
        self._open[position_id] = position
        logger.info(
            "MT5 OPEN %s %s %s @ %s (quote %s, slippage %s, spread %s) | sl=%s | "
            "ticket=%s | setup=%s",
            setup.direction.value,
            volume,
            symbol,
            result.price,
            entry,
            round(result.price - entry, info.digits),
            round(tick.ask - tick.bid, info.digits),
            sl,
            position_id,
            setup.setup_id,
        )
        return position

    # ------------------------------------------------------------ restart
    def reconcile(self, setups: list[Setup]) -> set[str]:
        """Re-attach to this strategy's MT5 positions after a restart.

        Positions are matched by magic number and comment (= setup_id) to the
        BUYs saved as open. Returns the setup_ids that still have positions;
        saved BUYs without any position were closed while the bot was down.
        """
        if mt5 is None:
            return set()
        wanted = {s.setup_id[:31]: s for s in setups}
        found: dict[str, list[OpenPosition]] = {}
        for row in mt5.positions_get() or ():
            if row.magic != MAGIC:
                continue
            setup = wanted.get(row.comment)
            if setup is None:
                logger.warning(
                    "MT5 position %s (%s, comment %r) carries this strategy's magic "
                    "but matches no saved BUY - left untouched",
                    row.ticket,
                    row.symbol,
                    row.comment,
                )
                continue
            found.setdefault(setup.setup_id, []).append(
                OpenPosition(
                    ticket=int(row.ticket),
                    symbol=row.symbol,
                    volume=row.volume,
                    is_buy=row.type == mt5.POSITION_TYPE_BUY,
                    entry=row.price_open,
                    sl=row.sl,
                    setup_id=setup.setup_id,
                )
            )
        for setup_id, positions in found.items():
            for index, position in enumerate(positions, start=1):
                position.index, position.basket_size = index, len(positions)
                self._open[position.ticket] = position
            self._track_basket(setup_id, positions)
            logger.info(
                "MT5 reconciled %s open positions for %s: %s",
                len(positions),
                setup_id,
                ", ".join(str(p.ticket) for p in positions),
            )
        return set(found)

    # --------------------------------------------------------------- exits
    def basket_is_open(self, setup_id: str) -> bool:
        """True while at least one position of the basket is still open."""
        return bool(self._baskets.get(setup_id))

    def close_basket(self, setup_id: str) -> int:
        """Market-close every open position of one basket (EMA exit rule).

        Returns the number of close orders the broker accepted. Realised P/L
        is still picked up by `manage()` once the positions disappear.
        """
        if mt5 is None:
            return 0
        positions = [p for p in self._open.values() if p.setup_id == setup_id]
        closed = 0
        for position in positions:
            if self._close_one(position):
                closed += 1
        return closed

    def _close_one(self, position: OpenPosition) -> bool:
        info = mt5.symbol_info(position.symbol)
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None or not tick.ask or not tick.bid:
            logger.error(
                "MT5 no quote to close %s ticket=%s - will retry",
                position.symbol,
                position.ticket,
            )
            return False
        result = mt5.order_send(
            {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": position.ticket,
                "symbol": position.symbol,
                "volume": position.volume,
                "type": mt5.ORDER_TYPE_SELL if position.is_buy else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if position.is_buy else tick.ask,
                "deviation": self.deviation,
                "magic": MAGIC,
                "comment": f"ema-exit {position.setup_id}"[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": _filling(info),
            }
        )
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "MT5 CLOSE %s %s ticket=%s | setup=%s",
                position.symbol,
                position.volume,
                position.ticket,
                position.setup_id,
            )
            return True
        logger.error(
            "MT5 close failed ticket=%s: retcode=%s comment=%s - will retry",
            position.ticket,
            getattr(result, "retcode", None),
            getattr(result, "comment", ""),
        )
        return False

    def closed_by_target(self, setup_id: str) -> bool:
        """True once if this basket was closed at the profit target."""
        if setup_id in self._target_closed:
            self._target_closed.discard(setup_id)
            return True
        return False

    def manage(self) -> list[TradeEvent]:
        """Report positions that closed since the last poll - SL hit, manual
        close, an EMA exit or a profit-target close - plus one aggregate
        event once every position of a basket has closed. While positions
        are open the profit target and trailing SL are managed here."""
        if mt5 is None:
            return []
        events: list[TradeEvent] = []
        live: dict[str, list[tuple[OpenPosition, object]]] = {}
        for ticket, position in list(self._open.items()):
            rows = mt5.positions_get(ticket=ticket)
            if rows:
                live.setdefault(position.setup_id, []).append((position, rows[0]))
                continue
            profit, exit_price = self._realized(ticket)
            logger.info(
                "MT5 position %s closed (SL/manual/EMA-exit) | setup=%s | exit=%s | "
                "profit=%.2f",
                ticket,
                position.setup_id,
                exit_price,
                profit,
            )
            del self._open[ticket]
            events.append(TradeEvent("closed", position, profit, exit_price))
            setup_id = position.setup_id
            basket = self._baskets.get(setup_id)
            if basket is None:
                continue
            basket.discard(ticket)
            total = self._basket_profits[setup_id] + profit
            self._basket_profits[setup_id] = total
            if exit_price is not None:
                self._basket_exits[setup_id].append((exit_price, position.volume))
            if not basket:
                del self._baskets[setup_id]
                del self._basket_profits[setup_id]
                self._basket_locks.pop(setup_id, None)
                exits = self._basket_exits.pop(setup_id)
                volume = sum(v for _, v in exits)
                average = sum(p * v for p, v in exits) / volume if volume else None
                events.append(
                    TradeEvent("basket_closed", position, round(total, 2), average)
                )
        if self.base_amount > 0 and self.profit_target_pct > 0:
            for setup_id, members in live.items():
                self._manage_profit(setup_id, members)
        return events

    # ---------------------------------------------------- profit management
    def _manage_profit(
        self, setup_id: str, members: list[tuple[OpenPosition, object]]
    ) -> None:
        """Close at PERCENTAGE_PROFIT of BASE_AMOUNT; before that, trail the
        SL so each 10% profit milestone locks the level 10% behind."""
        floating = sum(row.profit + row.swap for _, row in members)
        pct = floating / self.base_amount * 100
        if pct >= self.profit_target_pct:
            logger.info(
                "MT5 profit target hit: floating %.2f = %.1f%% of %.2f "
                "(target %.1f%%) - closing basket %s",
                floating,
                pct,
                self.base_amount,
                self.profit_target_pct,
                setup_id,
            )
            self._target_closed.add(setup_id)
            self.close_basket(setup_id)
            return
        milestone, lock_pct = trail_lock_level(pct, self.trail_step_pct)
        if milestone is None or milestone <= self._basket_locks.get(setup_id, 0.0):
            return
        if lock_pct is None:
            # First milestone: halve the SL->entry distance per position.
            if not self._halve_risk(members):
                return
            label = "halfway to the entry"
        else:
            price = self._lock_price(members, lock_pct)
            if price is None:
                return
            if not self._move_basket_sl(members, price):
                return
            label = f"locks +{lock_pct:.0f}% (price {price})"
        self._basket_locks[setup_id] = float(milestone)
        logger.info(
            "MT5 trailed SL for %s: floating %.1f%% of base -> SL %s",
            setup_id,
            pct,
            label,
        )

    def _basket_profit_at(
        self, members: list[tuple[OpenPosition, object]], price: float
    ) -> float | None:
        """What the whole basket would realise if closed at `price`."""
        total = 0.0
        for position, _row in members:
            action = (
                mt5.ORDER_TYPE_BUY if position.is_buy else mt5.ORDER_TYPE_SELL
            )
            value = mt5.order_calc_profit(
                action, position.symbol, position.volume, position.entry, price
            )
            if value is None:
                return None
            total += value
        return total

    def _lock_price(
        self, members: list[tuple[OpenPosition, object]], lock_pct: float
    ) -> float | None:
        """The shared SL price at which the basket locks `lock_pct` % of
        base_amount. Profit is linear in the close price, so two
        order_calc_profit evaluations solve it exactly."""
        first_row = members[0][1]
        tick = mt5.symbol_info_tick(first_row.symbol)
        if tick is None or not tick.bid or not tick.ask:
            return None
        ref = tick.bid if members[0][0].is_buy else tick.ask
        p0 = self._basket_profit_at(members, ref)
        p1 = self._basket_profit_at(members, ref + 1.0)
        if p0 is None or p1 is None or p1 == p0:
            return None
        target = lock_pct / 100.0 * self.base_amount
        return ref + (target - p0) / (p1 - p0)

    def _halve_risk(self, members: list[tuple[OpenPosition, object]]) -> bool:
        """First profit milestone: move every SL to the 50% level between its
        initial stop and the entry, so the remaining risk is halved."""
        moved = False
        for position, row in members:
            half = (position.entry + row.sl) / 2 if row.sl else position.entry
            if self._set_sl(position, row, half):
                moved = True
        return moved

    def _move_basket_sl(
        self, members: list[tuple[OpenPosition, object]], price: float
    ) -> bool:
        """Move every basket position's SL to `price` (SL only moves toward
        more profit - a BUY's stop is never lowered, a SELL's never raised)."""
        moved = False
        for position, row in members:
            if self._set_sl(position, row, price):
                moved = True
        return moved

    def _set_sl(self, position: OpenPosition, row, price: float) -> bool:
        """Send one TRADE_ACTION_SLTP modify; True when the broker accepted."""
        info = mt5.symbol_info(position.symbol)
        tick = mt5.symbol_info_tick(position.symbol)
        if info is None or tick is None:
            return False
        buffer = max(info.trade_stops_level * info.point, info.point)
        if position.is_buy:
            sl = round(min(price, tick.bid - buffer), info.digits)
            if sl <= row.sl + info.point * 0.5:
                return False  # nothing gained - keep the current stop
        else:
            sl = round(max(price, tick.ask + buffer), info.digits)
            if row.sl and sl >= row.sl - info.point * 0.5:
                return False
        result = mt5.order_send(
            {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": position.ticket,
                "symbol": position.symbol,
                "sl": sl,
                "tp": getattr(row, "tp", 0.0) or 0.0,
            }
        )
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                "MT5 SL modify failed ticket=%s sl=%s: retcode=%s "
                "comment=%s - will retry on the next poll",
                position.ticket,
                sl,
                getattr(result, "retcode", None),
                getattr(result, "comment", ""),
            )
            return False
        position.sl = sl
        return True

    # --------------------------------------------------------------- helpers
    def _volume(self, info) -> float:
        """Trade volume: MT5_LOT clamped to the symbol's limits, or the
        symbol's own minimum when MT5_LOT=0."""
        lot = self.lot if self.lot > 0 else info.volume_min
        volume = min(max(lot, info.volume_min), info.volume_max)
        step = info.volume_step or 0.001
        return round(round(volume / step) * step, 8)

    @staticmethod
    def _quote(info):
        """Latest tick, subscribing to the symbol first if it is not in
        Market Watch and waiting briefly for the quote stream to start."""
        if not info.visible and not mt5.symbol_select(info.name, True):
            logger.error("MT5 cannot subscribe to %s: %s", info.name, mt5.last_error())
            return None
        deadline = time.monotonic() + QUOTE_WAIT_SECONDS
        while True:
            tick = mt5.symbol_info_tick(info.name)
            if tick is not None and tick.ask and tick.bid:
                return tick
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)

    @staticmethod
    def _position_id(result) -> int:
        """Position ticket for the fill (differs from the order id on netting)."""
        deals = mt5.history_deals_get(ticket=result.deal)
        if deals:
            return int(deals[0].position_id)
        return int(result.order)

    @staticmethod
    def _realized(ticket: int) -> tuple[float, float | None]:
        """(closed P/L, closing fill price) for a position from deal history."""
        deals = mt5.history_deals_get(position=ticket)
        if not deals:
            return 0.0, None
        profit = round(sum(d.profit + d.commission + d.swap for d in deals), 2)
        closing = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
        return profit, (closing[-1].price if closing else None)


def trail_lock_level(
    floating_pct: float, trail_step_pct: float = 10.0
) -> tuple[int | None, float | None]:
    """(milestone, lock %) for a floating-profit % of base_amount.

    Milestone 1 (floating >= one step) returns ``lock=None``: the SL moves
    to the 50% level between the entry and the initial stop, halving the
    risk. Milestone 2+ locks ``(milestone - 1) * step`` % of the base -
    the SL trails the profit one step behind (20% -> +10%, 30% -> +20%,
    ...) until PERCENTAGE_PROFIT closes the trade. ``(None, None)`` below
    the first step.
    """
    if trail_step_pct <= 0:
        return None, None
    milestone = int(floating_pct // trail_step_pct)
    if milestone < 1:
        return None, None
    if milestone == 1:
        return milestone, None
    return milestone, (milestone - 1) * trail_step_pct


def _stop_level(info, entry: float, swing_level: float, is_buy: bool) -> float:
    """SL at the most recent swing low (BUY).

    When the swing level sits too close to the entry (or, pathologically, on
    the wrong side) it is clamped to the broker's minimum stop distance so
    the order is not rejected with "invalid stops".
    """
    buffer = max(info.trade_stops_level * info.point, info.point)
    if is_buy:
        return round(min(swing_level, entry - buffer), info.digits)
    return round(max(swing_level, entry + buffer), info.digits)


def mt5_symbol(asset: str) -> str:
    """Primary MT5 symbol candidate for `asset`.

    Deriv MT5 servers differ: some list symbols as display names
    ("Volatility 75 Index"), others as Deriv API symbols ("R_75"). This
    returns the display-name form first; ``_resolve_mt5_symbol`` falls
    back to the raw asset string when that name is not in the terminal.
    """
    return DERIV_NAMES.get(asset, asset)


def _filling(info) -> int:
    """Pick a filling mode the symbol allows (SYMBOL_FILLING_MODE bitmask)."""
    if info is None:
        return mt5.ORDER_FILLING_IOC
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN
