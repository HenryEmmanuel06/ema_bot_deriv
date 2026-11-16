"""SQLite schema. Portable to PostgreSQL later.

The Triple EMA strategy uses its own `ema_setups` / `ema_signals` tables so
databases created by the old W/M bot keep working untouched.
"""

SETUPS_TABLE = """
CREATE TABLE IF NOT EXISTS ema_setups (
    setup_id TEXT PRIMARY KEY,
    asset TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction TEXT NOT NULL,
    state TEXT,
    status TEXT NOT NULL,
    window_start TEXT,
    convergence_time TEXT,
    convergence_spread_pct REAL,
    convergence_emas TEXT,
    cross_fast_time TEXT,
    cross_mid_time TEXT,
    entry_confirmation_time TEXT,
    entry_price REAL,
    entry_emas TEXT,
    stop_loss REAL,
    swing_low_time TEXT,
    exit_cross_mid_time TEXT,
    exit_cross_fast_time TEXT,
    exit_signal_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    exit_time TEXT,
    profit REAL,
    result TEXT DEFAULT 'UNKNOWN',
    invalidation_reason TEXT,
    tickets TEXT,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

SIGNALS_TABLE = """
CREATE TABLE IF NOT EXISTS ema_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    asset TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction TEXT,
    entry_time TEXT,
    entry_price REAL,
    stop_loss REAL,
    exit_price REAL,
    result TEXT DEFAULT 'UNKNOWN',
    detail TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (setup_id, kind),
    FOREIGN KEY (setup_id) REFERENCES ema_setups (setup_id)
);
"""

CANDLES_TABLE = """
CREATE TABLE IF NOT EXISTS candles (
    asset TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    source TEXT,
    PRIMARY KEY (asset, timeframe, timestamp)
);
"""

#: Columns added after the first release: applied to existing databases.
MIGRATIONS: dict[str, dict[str, str]] = {}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ema_setups_asset_tf ON ema_setups (asset, timeframe);",
    "CREATE INDEX IF NOT EXISTS idx_ema_setups_status ON ema_setups (status);",
    "CREATE INDEX IF NOT EXISTS idx_ema_signals_setup ON ema_signals (setup_id);",
]

ALL_TABLES = [SETUPS_TABLE, SIGNALS_TABLE, CANDLES_TABLE]
