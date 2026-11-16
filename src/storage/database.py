"""SQLite persistence layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.data.models import Candle
from src.signals.models import Setup, SetupStatus, SignalKind
from src.storage.models import ALL_TABLES, INDEXES, MIGRATIONS
from src.utils.logging import get_logger
from src.utils.time import now_utc

logger = get_logger(__name__)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        for ddl in ALL_TABLES:
            self._conn.execute(ddl)
        for ddl in INDEXES:
            self._conn.execute(ddl)
        self._migrate()
        self._conn.commit()
        logger.info("Database ready at %s", self.path)

    def _migrate(self) -> None:
        """Adds columns introduced after a database was first created."""
        for table, columns in MIGRATIONS.items():
            existing = {
                row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            for column, sql_type in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
                    logger.info("Database migrated: %s.%s added", table, column)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    # ---------------------------------------------------------------- write
    def save_setup(self, setup: Setup) -> None:
        row = setup.as_row()
        columns = ", ".join(row.keys())
        placeholders = ", ".join(f":{key}" for key in row)
        updates = ", ".join(f"{key}=excluded.{key}" for key in row if key != "setup_id")
        try:
            self.conn.execute(
                f"INSERT INTO ema_setups ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(setup_id) DO UPDATE SET {updates}",
                row,
            )
            self.conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to save setup %s: %s", setup.setup_id, exc)

    def save_signal(self, setup: Setup, kind: SignalKind, detail: str = "") -> None:
        try:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO ema_signals (
                    setup_id, kind, asset, timeframe, direction, entry_time,
                    entry_price, stop_loss, exit_price, result, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    setup.setup_id,
                    kind.value,
                    setup.asset,
                    setup.timeframe,
                    setup.direction.value,
                    setup.entry_confirmation_time.isoformat()
                    if setup.entry_confirmation_time
                    else None,
                    setup.entry_price,
                    setup.stop_loss,
                    setup.exit_price,
                    setup.result.value,
                    detail,
                    now_utc().isoformat(),
                ),
            )
            self.conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to save signal %s/%s: %s", setup.setup_id, kind.value, exc)

    def save_candle(self, candle: Candle) -> None:
        row = candle.as_row()
        try:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO candles
                (asset, timeframe, timestamp, open, high, low, close, volume, source)
                VALUES (:asset, :timeframe, :timestamp, :open, :high, :low, :close,
                        :volume, :source)
                """,
                row,
            )
            self.conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to save candle: %s", exc)

    # ----------------------------------------------------------------- read
    def open_setups(self) -> list[Setup]:
        """BUYs saved as open - restored after a restart."""
        rows = self.conn.execute(
            "SELECT * FROM ema_setups WHERE status=? ORDER BY created_at",
            (SetupStatus.OPEN.value,),
        ).fetchall()
        return [Setup.from_row(dict(row)) for row in rows]

    def stats(self) -> dict:
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open,
                SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed,
                SUM(CASE WHEN status='INVALIDATED' THEN 1 ELSE 0 END) AS invalidated,
                SUM(CASE WHEN status='EXPIRED' THEN 1 ELSE 0 END) AS expired,
                SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) AS losses,
                SUM(COALESCE(profit, 0)) AS profit
            FROM ema_setups
            """
        ).fetchone()
        return dict(row) if row else {}
