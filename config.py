"""Load paper-trading settings from the environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

ROOT = Path(__file__).resolve().parent


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip().strip('"').strip("'").replace("\n", "").replace("\r", "")
        if value:
            return value
    return default


def env_decimal(name: str, default: str) -> Decimal:
    raw = env_first(name, default=default)
    try:
        return Decimal(raw)
    except Exception:
        return Decimal(default)


@dataclass(frozen=True)
class InstrumentSpec:
    oanda: str
    display: str
    yahoo: str
    pip: Decimal
    display_precision: int
    units_per_lot: Decimal
    typical_spread: Decimal
    min_sl_distance: Decimal


INSTRUMENTS: tuple[InstrumentSpec, ...] = (
    InstrumentSpec(
        oanda="EUR_USD",
        display="EUR/USD",
        yahoo="EURUSD=X",
        pip=Decimal("0.0001"),
        display_precision=5,
        units_per_lot=Decimal("100000"),
        typical_spread=Decimal("0.00008"),
        min_sl_distance=Decimal("0.00020"),
    ),
    InstrumentSpec(
        oanda="GBP_USD",
        display="GBP/USD",
        yahoo="GBPUSD=X",
        pip=Decimal("0.0001"),
        display_precision=5,
        units_per_lot=Decimal("100000"),
        typical_spread=Decimal("0.00012"),
        min_sl_distance=Decimal("0.00020"),
    ),
    InstrumentSpec(
        oanda="SPX500_USD",
        display="S&P 500",
        yahoo="^GSPC",
        pip=Decimal("1"),
        display_precision=2,
        units_per_lot=Decimal("1"),
        typical_spread=Decimal("0.50"),
        min_sl_distance=Decimal("2"),
    ),
)

SPECS: dict[str, InstrumentSpec] = {spec.oanda: spec for spec in INSTRUMENTS}

OANDA_API_KEY = env_first("OANDA_API_KEY", "OANDA_TOKEN")
OANDA_ACCOUNT_ID = env_first("OANDA_ACCOUNT_ID")
OANDA_ENVIRONMENT = env_first("OANDA_ENVIRONMENT", default="practice").lower()
OANDA_BASE_URL = (
    "https://api-fxtrade.oanda.com"
    if OANDA_ENVIRONMENT == "live"
    else "https://api-fxpractice.oanda.com"
)

STARTING_CAPITAL_EUR = env_decimal("STARTING_CAPITAL_EUR", "50")
MAX_LOT = env_decimal("MAX_LOT", "0.01")
MAX_RISK_PCT = env_decimal("MAX_RISK_PCT", "0.01")
MAX_RISK_EUR = env_decimal("MAX_RISK_EUR", "0.50")
TP_RATIO = env_decimal("TP_RATIO", "2.0")
ATR_SL_MULT = env_decimal("ATR_SL_MULT", "1.2")

_TIMEFRAME_RAW = env_first("TIMEFRAME", default="5m").upper().replace("M", "")
if _TIMEFRAME_RAW in {"15", "15M"}:
    TIMEFRAME_LABEL = "15m"
    OANDA_GRANULARITY = "M15"
    YAHOO_INTERVAL = "15m"
    STALE_SECONDS = 20 * 60
else:
    TIMEFRAME_LABEL = "5m"
    OANDA_GRANULARITY = "M5"
    YAHOO_INTERVAL = "5m"
    STALE_SECONDS = 8 * 60

POLL_SECONDS = int(env_first("POLL_SECONDS", default="15") or "15")
MAX_OPEN_POSITIONS = int(env_first("MAX_OPEN_POSITIONS", default="3") or "3")
COOLDOWN_AFTER_EXIT_SECONDS = int(env_first("COOLDOWN_AFTER_EXIT_SECONDS", default="60") or "60")
OHLCV_LIMIT = 80
RSI_PERIOD = 14
RSI_OVERSOLD = Decimal(env_first("RSI_OVERSOLD", default="40"))
RSI_OVERBOUGHT = Decimal(env_first("RSI_OVERBOUGHT", default="60"))
EMA_TREND_PERIOD = 50
ATR_PERIOD = 14
ATR_SPIKE_MULT = Decimal("2.5")
VOL_LOOKBACK = 20
VOL_ZSCORE_MAX = Decimal("2.5")

STATE_FILE = ROOT / "bot_state_paper.json"
LOG_FILE = ROOT / "bot.log"
PORT = int(os.getenv("PORT", "10000"))
