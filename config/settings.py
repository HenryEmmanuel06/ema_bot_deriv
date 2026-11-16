"""Environment driven application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from config.strategy import TIMEFRAME_CONFIG, validate_strategy_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

VALID_MODES = ("BACKTEST", "PAPER", "LIVE_SIGNAL")

#: Public Deriv WebSocket: market data without any API key or login.
DERIV_PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int = 0) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float = 0.0) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_pct(name: str, default: float = 0.0) -> float:
    """Percentage env value; a trailing % sign is allowed ("40%" -> 40.0)."""
    raw = _env(name).rstrip("%").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def normalize_chat_id(raw: str) -> str:
    """Telegram channel/supergroup ids are negative and start with -100.

    Users frequently copy the id without the leading minus sign, which makes
    the Bot API reject the request. Restore it when the value is clearly a
    channel id.
    """
    value = raw.strip()
    if not value:
        return value
    if value.startswith("@") or value.startswith("-"):
        return value
    if value.isdigit() and value.startswith("100") and len(value) >= 13:
        return "-" + value
    return value


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    log_level: str
    app_mode: str
    enabled_timeframes: tuple[str, ...]
    market_data_provider: str
    #: Optional - the public Deriv endpoint needs no key. Kept for future
    #: authenticated endpoints.
    deriv_api_key: str
    deriv_ws_url: str
    display_timezone: str
    notify_invalidations: bool
    #: Phase 1 "EMA convergence" Telegram message switch.
    notify_convergence: bool
    #: When true, BUY execution signals open POSITIONS_PER_TRADE market
    #: positions on the MT5 account below. Each carries a broker-side SL at
    #: the most recent swing low and the bot closes the basket once the 32
    #: EMA has crossed back above both the 14 EMA and the 7 EMA.
    take_trade: bool
    #: Deriv MT5 account credentials (a demo account works). Created in the
    #: Deriv dashboard - the Deriv API key cannot log in to MT5.
    mt5_login: int
    mt5_password: str
    mt5_server: str
    #: Optional path to terminal64.exe; empty = auto-detect.
    mt5_path: str
    mt5_lot: float
    mt5_deviation: int
    #: Market positions opened per execution signal.
    positions_per_trade: int
    #: Profit-target / trailing-SL feature (account currency). The basket's
    #: floating profit is measured as a % of `base_amount`; the trade
    #: closes at `percentage_profit` % and the SL trails every 10%
    #: milestone 10% behind. 0 disables the feature.
    base_amount: float
    percentage_profit: float
    #: Telegram execution-signal switch.
    notify_execution: bool
    database_path: Path
    historical_dir: Path
    log_dir: Path
    extra: dict = field(default_factory=dict)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def is_live_data_mode(self) -> bool:
        return self.app_mode in ("PAPER", "LIVE_SIGNAL")

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.app_mode not in VALID_MODES:
            problems.append(f"APP_MODE must be one of {VALID_MODES}, got {self.app_mode!r}")
        if not self.enabled_timeframes:
            problems.append("SIGNAL_TIMEFRAME is empty (e.g. SIGNAL_TIMEFRAME=5m)")
        unknown = [tf for tf in self.enabled_timeframes if tf not in TIMEFRAME_CONFIG]
        if unknown:
            problems.append(
                f"SIGNAL_TIMEFRAME has unsupported values {unknown}; "
                f"choose from {list(TIMEFRAME_CONFIG)}"
            )
        problems.extend(validate_strategy_config())
        if self.app_mode == "LIVE_SIGNAL" and not self.telegram_configured:
            problems.append("LIVE_SIGNAL mode requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        if self.is_live_data_mode and self.market_data_provider != "deriv":
            problems.append(
                "Live market data must come from Deriv. "
                "Set MARKET_DATA_PROVIDER=deriv"
            )
        if self.take_trade:
            if not (self.mt5_login and self.mt5_password and self.mt5_server):
                problems.append(
                    "TAKE_TRADE=true requires MT5_LOGIN, MT5_PASSWORD and "
                    "MT5_SERVER (a Deriv MT5 account - the Deriv API key "
                    "cannot log in to MT5)"
                )
            if self.mt5_lot < 0:
                problems.append("MT5_LOT cannot be negative (0 = each symbol's minimum)")
            if self.positions_per_trade < 1:
                problems.append("POSITIONS_PER_TRADE must be at least 1")
            if self.base_amount < 0:
                problems.append("BASE_AMOUNT cannot be negative")
            if self.percentage_profit < 0:
                problems.append("PERCENTAGE_PROFIT cannot be negative (0 disables the profit target)")
            if self.percentage_profit > 0 and self.base_amount <= 0:
                problems.append(
                    "PERCENTAGE_PROFIT requires BASE_AMOUNT > 0 - the floating "
                    "profit is measured as a percentage of it"
                )
        return problems


def load_settings() -> Settings:
    timeframes: list[str] = []
    for label in _env("SIGNAL_TIMEFRAME", default="5m").lower().split(","):
        if label.strip() and label.strip() not in timeframes:
            timeframes.append(label.strip())

    database_path = Path(_env("DATABASE_PATH", default="data/signals.db"))
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path

    return Settings(
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", "BOT_TOKEN"),
        telegram_chat_id=normalize_chat_id(_env("TELEGRAM_CHAT_ID", "CHAT_ID")),
        log_level=_env("LOG_LEVEL", default="INFO").upper(),
        app_mode=_env("APP_MODE", default="PAPER").upper(),
        enabled_timeframes=tuple(timeframes),
        market_data_provider=_env("MARKET_DATA_PROVIDER", default="deriv").lower(),
        deriv_api_key=_env("DERIV_API_KEY", "DERIV_API_TOKEN"),
        deriv_ws_url=_env("DERIV_WS_URL", default=DERIV_PUBLIC_WS_URL),
        display_timezone=_env("DISPLAY_TIMEZONE", default="UTC"),
        notify_invalidations=_env_bool("NOTIFY_INVALIDATIONS", False),
        notify_convergence=_env_bool("NOTIFY_CONVERGENCE", True),
        take_trade=_env_bool("TAKE_TRADE", False),
        mt5_login=_env_int("MT5_LOGIN"),
        mt5_password=_env("MT5_PASSWORD"),
        mt5_server=_env("MT5_SERVER"),
        mt5_path=_env("MT5_PATH"),
        mt5_lot=_env_float("MT5_LOT", 0.0),
        mt5_deviation=_env_int("MT5_DEVIATION", 30),
        positions_per_trade=max(1, _env_int("POSITIONS_PER_TRADE", 5)),
        base_amount=_env_float("BASE_AMOUNT", 0.0),
        percentage_profit=_env_pct("PERCENTAGE_PROFIT", 0.0),
        notify_execution=_env_bool("EXECUTION_SIGNAL", True),
        database_path=database_path,
        historical_dir=PROJECT_ROOT / "data" / "historical",
        log_dir=PROJECT_ROOT / "data" / "logs",
    )


settings = load_settings()
