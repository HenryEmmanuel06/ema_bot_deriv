# Deriv Triple EMA Signal + MT5 Execution Bot

A Python bot that streams Deriv Synthetic (Volatility) Indices over the
public Deriv WebSocket, detects a **Triple EMA Convergence** setup on
EMA 7 / 14 / 32, sends phased Telegram signals, and — optionally — opens a
basket of BUY positions on a Deriv MT5 account through a locally running
MetaTrader 5 terminal.

```text
Deriv WebSocket (public, no API key)
        ↓ ticks
Candle builder (1m … 30m, one engine per asset × timeframe)
        ↓ closed candles
EMA 7/14/32 → intersection → bullish 7>14>32 resolution → BUY signal
        ↓                                     ↓
   Telegram phases                  TAKE_TRADE=true → MT5 BUY basket
        ↓                                     ↓
   SQLite (signals.db)              SL at most recent swing low
                                    exit on 32-above-7/14 reversal
```

The strategy is **BUY-only**. There is no SELL path — a mirror setup is
never inferred.

---

## 1. The strategy

Every asset × timeframe runs its own independent state machine — a setup on
`R_75 5m` never interferes with `1HZ10V 1m`. All EMA decisions are made on
**closed candles** (`CONFIRM_ON_CANDLE_CLOSE` behaviour — the forming candle
is never used), and by default the EMAs are computed on **standard candle
closes** (`EMA_PRICE_SOURCE=standard_close`). Heikin Ashi is kept as the
chart/display convention; `EMA_PRICE_SOURCE=heikin_ashi_close` switches the
EMA input to HA closes if you want to experiment.

### 1.1 Phase 1 — the 3-EMA intersection

The three EMAs are said to intersect (converge) when they cluster tightly
together:

```text
spread     = max(EMA7, EMA14, EMA32) - min(EMA7, EMA14, EMA32)
spread_pct = spread / close × 100
converged  = spread_pct <= MAX_EMA_SPREAD_PCT   (default 0.10%)
```

The intersection must occur on one of the last `CONVERGENCE_LOOKBACK`
closed candles (default 3). When detected, a `🔵 EMA CONVERGENCE` message
is sent (if `NOTIFY_CONVERGENCE=true`) and the setup starts watching for
the bullish resolution. A setup expires after `SETUP_MAX_AGE_CANDLES`
closed candles (default 8) without a BUY confirmation.

### 1.2 Phase 2 — the bullish resolution (BUY on the intersection)

Once the three EMAs have intersected, the BUY executes on the **first
closed candle that is ordered bullish while the intersection is still
fresh**:

```text
spread_pct <= MAX_EMA_SPREAD_PCT on one of the last
             CONVERGENCE_LOOKBACK candles   (the three MAs have met)
EMA7 > EMA14 > EMA32 on the confirming candle (resolved upward)
```

— i.e. the intersection resolving upward, with the fast EMAs back on top
of the 32 EMA. A bullish ordering on EMAs that separated **before ever
meeting** — or long after the intersection went stale — never fires a
trade: the three moving averages must have met within the lookback. That
candle's close is the moment the `🟢 BUY EXECUTION SIGNAL` fires; the
proposed entry is the next market quote after confirmation (the
confirming candle's close is used in signal-only mode).
The points where the 32 EMA crossed under the 7 and 14 EMAs are recorded
for the signal message, but they are **not** required — the intersection
itself is the trigger. Exactly **one** BUY signal is produced per
intersection event — a confirmed setup can never fire twice, and new
entries are ignored while a BUY from this asset × timeframe is open.

### 1.3 The exit — EMA reversal

The exit is the mirror of the entry. After the BUY opens, the bot waits
for the 32 EMA to cross back **above** the 14 EMA and **above** the 7 EMA.
Again the two crosses are tracked independently and must both occur *after*
the entry. The `🔴 BUY EXIT — EMA REVERSAL` signal fires only when the
latest closed candle still has `EMA32 > EMA14` **and** `EMA32 > EMA7` — a
single cross is not enough, and the trade stays open between the two
crosses.

The EMA exit is signal-based: it can lag fast reversals, and it is **not
guaranteed to be profitable**. That is why every position also carries a
broker-side stop-loss.

### 1.4 Emergency stop-loss — most recent swing low

Every BUY gets a real broker-side SL at the **most recent confirmed swing
low** below the entry: a candle whose low is lower than the
`SWING_LOOKBACK` (default 2) candles on each side, searched within the
last `SL_SEARCH_CANDLES` (default 50) closed candles. If that level is
closer than the broker's minimum stop distance, the SL is placed at the
minimum distance instead so MT5 accepts it. The unconfirmed forming
candle is never used as a swing low.

Because the SL lives on the broker's server, the protection survives a bot
crash, disconnect or PC shutdown. The EMA exit, however, needs the bot
running.

### 1.5 Profit target + trailing SL (MT5 only)

When `TAKE_TRADE=true` and `PERCENTAGE_PROFIT` is set, the basket's total
floating profit is measured against `BASE_AMOUNT` (account currency):

```text
floating % = basket floating profit / BASE_AMOUNT × 100
```

- **Close at the target** — floating profit reaches `PERCENTAGE_PROFIT`%
  of `BASE_AMOUNT` → the whole basket is closed at market.
- **First milestone halves the risk** — at 10% floating profit each SL
  moves to the 50% level between the entry and its initial stop.
- **Then trail the SL 10% behind** — every further 10% milestone of
  floating profit locks the profit level 10% behind:

```text
floating profit   SL moved to
      10%         halfway between entry and initial SL (risk halved)
      20%         locks +10%
      30%         locks +20%
      40%         trade closes (target = 40%)
```

  So with `BASE_AMOUNT=100` and `PERCENTAGE_PROFIT=40`, +$40 closes the
  trade; at +$10 the SL halves the remaining risk, at +$20 it locks
  +$10, at +$30 it locks +$20, and so on. The SL only ever moves toward
  more profit — it is never lowered. `PERCENTAGE_PROFIT=0` disables the
  feature entirely.

### 1.6 Invalidations

A setup dies (and can never fire later) on:

- **Bearish ordering** (`EMA32` back above the fast EMAs) before entry —
  the intersection resolved downward
- **Setup expiry** — `SETUP_MAX_AGE_CANDLES` without a BUY confirmation
- **Unreliable data** — stream gaps flag `DATA_UNRELIABLE` and suspend new
  entries until candles resynchronise (open trades keep being managed)

`NOTIFY_INVALIDATIONS=true` sends these to Telegram; otherwise they're
logged only.

---

## 2. Telegram messages

Every message carries the setup ID (`R25-5M-EMA-20261021-173000`) so the
full lifecycle can be traced in SQLite and the logs.

| Event | Message |
|---|---|
| EMAs converge within `MAX_EMA_SPREAD_PCT` | `🔵 EMA CONVERGENCE` — EMA values, spread %, `NOTIFY_CONVERGENCE` toggle |
| Intersection resolves `EMA7>14>32` | `🟢 BUY EXECUTION SIGNAL` — cross timestamps, EMA ordering, entry, swing-low SL |
| Basket filled (trading on) | `✅ BUY OPENED` — position count, actual average fill, SL, tickets |
| 32 EMA back above 14 and 7 | `🔴 BUY EXIT — EMA REVERSAL` — exit-cross times, exit price, price move |
| Price hits the swing-low SL (trading on) | `🛑 BUY STOP-LOSS HIT` |
| Floating profit hits `PERCENTAGE_PROFIT` of `BASE_AMOUNT` | Basket closed at market — `TRADE RESULT` shows `Closed by: Profit target reached` |
| Whole basket flat (trading on) | `✅/❌/⚪ TRADE RESULT` — combined real P/L, tickets, `Closed by:` |
| Setup dies before entry | `⚪ SETUP INVALIDATED/EXPIRED` — `NOTIFY_INVALIDATIONS` toggle |

Signal-only messages end with `⚠️ Strategy signal only. No trade is
executed by this bot.` so they can never be confused with real fills.

---

## 3. MT5 execution (TAKE_TRADE)

Execution is **off by default** (`TAKE_TRADE=false` → signals only). With
`TAKE_TRADE=true` the bot attaches to a **locally running MetaTrader 5
terminal** (Windows only) logged into a Deriv MT5 account and turns each
BUY signal into a **basket** of `POSITIONS_PER_TRADE` market orders
(default 5).

### Basket risk model

All N positions share symbol/direction/volume and are tagged with the
setup ID in the order comment plus a strategy magic number, so the bot can
tell its own positions apart from manual trades. Every position gets the
same broker-side **stop-loss at the most recent swing low** and no
take-profit — the exit is the strategy itself.

### Restart reconciliation

On startup the bot scans open MT5 positions by symbol/magic/comment and
the tickets stored in SQLite. A basket that is still open restores the
setup's `OPEN` state, so exit-cross monitoring continues where it left
off — both exit crosses must still occur *after* the entry, exactly as if
the bot had never stopped. Positions closed manually are detected and
reported with their real P/L from deal history; a close order that fails
is retried until the basket is flat.

### Requirements

- MT5 desktop terminal installed, running, logged into the Deriv MT5
  account, with **Algo Trading** enabled (green toolbar button)
- `pip install MetaTrader5` (Windows-only package)
- Symbols must exist on the account — Deriv MT5 uses the same display
  names (`Volatility 75 Index`). The bot auto-subscribes symbols to
  Market Watch.
- `TAKE_TRADE=true` fails validation unless `MT5_LOGIN`, `MT5_PASSWORD`
  and `MT5_SERVER` are set. These are Deriv **MT5** credentials — the
  Deriv API key cannot log in to MT5.

### Notes

- MT5 trades are **CFDs on synthetic indices**, not binary options — P/L
  scales with distance moved, not a fixed payout.
- `MT5_LOT=0` trades each symbol's own minimum volume; a positive value
  is a fixed lot clamped to each symbol's limits.
- You can monitor/close positions in MT5 on desktop or the mobile app
  logged into the same account.

---

## 4. Configuration (`.env`)

Copy `.env.example` to `.env`. Every strategy threshold is a **proposed
default — backtest before trusting it.**

```env
# Telegram
TELEGRAM_BOT_TOKEN=            # from @BotFather
TELEGRAM_CHAT_ID=              # channel/group/user id
DISPLAY_TIMEZONE=Africa/Lagos

# App
LOG_LEVEL=INFO
APP_MODE=PAPER                 # BACKTEST | PAPER | LIVE_SIGNAL
DATABASE_PATH=data/signals.db

# Market data — Deriv public feed needs no key
MARKET_DATA_PROVIDER=deriv
DERIV_ASSETS=R_25              # empty = all mapped volatility indices

# Triple EMA strategy
SIGNAL_TIMEFRAME=5m            # 1m,2m,3m,5m,10m,15m,30m (comma-separated ok)
EMA_FAST=7
EMA_MID=14
EMA_SLOW=32
EMA_PRICE_SOURCE=standard_close   # or heikin_ashi_close
MAX_EMA_SPREAD_PCT=0.10        # 3-EMA intersection threshold, % of price
CONVERGENCE_LOOKBACK=3         # candles the spread must appear within
SETUP_MAX_AGE_CANDLES=8        # setup expiry
SWING_LOOKBACK=2               # fractal candles each side for a swing low
SL_SEARCH_CANDLES=50           # how far back to search for the swing low

# Notification toggles
NOTIFY_CONVERGENCE=true
EXECUTION_SIGNAL=true
NOTIFY_INVALIDATIONS=false

# MT5 execution (Windows only)
TAKE_TRADE=false
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=                    # e.g. Deriv-Demo
MT5_LOT=0                      # 0 = each symbol's minimum
MT5_DEVIATION=30
# MT5_PATH=                    # optional terminal64.exe path
POSITIONS_PER_TRADE=5          # basket size per BUY signal
BASE_AMOUNT=100                # money the profit % is measured on
PERCENTAGE_PROFIT=40           # close at +40% of base; SL trails 10% behind
```

Validation rejects `0 >= EMA_FAST`, wrong EMA ordering
(`EMA_FAST < EMA_MID < EMA_SLOW` required), non-positive spread/lookback
values, unsupported `SIGNAL_TIMEFRAME`s, and `TAKE_TRADE=true` without
MT5 credentials.

## 5. Running

```powershell
pip install -r requirements.txt

# live signals (+ MT5 execution when TAKE_TRADE=true)
python scripts/paper_trade.py --assets R_25 R_50 R_75

# live signals with Telegram delivery
python -m src.main --mode LIVE_SIGNAL

# historical backtest
python scripts/backtest.py

# end-to-end signal-pipeline demo (scripted prices, never trades)
python scripts/test_signal_pipeline.py

# telegram connectivity check
python scripts/test_telegram.py

# tests
python -m pytest
```

`--assets` accepts Deriv symbols (`R_75`) or display names
(`"Volatility 75 Index"`); omit it to watch every mapped index.
`APP_MODE` only controls Telegram/notification routing — `TAKE_TRADE`
alone gates real orders, so `paper_trade.py` *does* trade when it's
`true`.

## 6. Project structure

```text
config/
  settings.py      env loading + validation
  strategy.py      EMA/convergence/cross/swing-low parameters
  assets.py        display name ↔ Deriv symbol map
src/
  main.py          SignalBot: provider loop, dispatch, heartbeat
  data/            Deriv websocket provider + tick→candle builder
  strategy/        ema.py, stop_loss.py (swing low), state_machine.py,
                   heikin_ashi.py (display/optional EMA source)
  signals/         Setup/SignalEvent models, engine, manager
  execution/       mt5_executor.py — BUY baskets, swing-low SL,
                   basket close, restart reconciliation
  telegram/        bot, formatter, notifier (dedup + signal toggles)
  storage/         SQLite (candles, setups, signals, open-trade restore)
  utils/           logging, time helpers
scripts/           paper_trade.py, backtest.py, test_signal_pipeline.py,
                   test_telegram.py
tests/             acceptance tests covering the strategy rules end-to-end
```

## 7. Reliability notes

- Signals pause (`DATA_UNRELIABLE`) when the stream has gaps — reconnects
  resync candles before signalling resumes. Open trades keep being
  managed; the broker-side SL protects them either way.
- Duplicate candles/messages are suppressed; one BUY signal per setup.
- `manager`/`notifier` run synchronously in the tick loop; MT5 and
  Telegram calls are blocking and local/fast.
- On shutdown, open MT5 positions keep their broker-side SL at the swing
  low, but the EMA-reversal exit stops until the bot is restarted; the
  bot warns and detaches.

## 8. Caveats

- All thresholds (`MAX_EMA_SPREAD_PCT`, `CONVERGENCE_LOOKBACK`,
  `SETUP_MAX_AGE_CANDLES`, `SWING_LOOKBACK`, …) are **proposed defaults** —
  run `scripts/backtest.py` on real history before enabling trading.
- The EMA reversal exit lags by construction — it needs both crosses on
  closed candles — so losing exits are normal. The swing-low SL is the
  emergency protection, not the profit mechanism.
- Backtests use closed candles only, so they cannot model intrabar fills
  or slippage; `MT5_DEVIATION` and real spreads affect live results.
