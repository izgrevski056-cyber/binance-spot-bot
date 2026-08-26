"""Live market data (OANDA practice or Yahoo) plus a local EUR paper ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

import requests

from config import (
    INSTRUMENTS,
    LOG_FILE,
    MAX_LOT,
    OANDA_ACCOUNT_ID,
    OANDA_API_KEY,
    OANDA_BASE_URL,
    OANDA_GRANULARITY,
    OHLCV_LIMIT,
    SPECS,
    STARTING_CAPITAL_EUR,
    STATE_FILE,
    STALE_SECONDS,
    YAHOO_INTERVAL,
    InstrumentSpec,
)
from strategy import to_decimal


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ts() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def log(kind: str, message: str) -> None:
    line = f"[{ts()}] {kind:<9} {message}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def fmt_price(spec: InstrumentSpec, price: Decimal) -> str:
    return f"{price:.{spec.display_precision}f}"


@dataclass
class Quote:
    instrument: str
    bid: Decimal
    ask: Decimal
    time: datetime
    tradeable: bool

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")


@dataclass
class Position:
    instrument: str
    side: str
    units: Decimal
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    opened_at: str
    lots: Decimal

    def mark(self, quote: Quote) -> Decimal:
        return quote.bid if self.side == "BUY" else quote.ask

    def unrealized_usd(self, quote: Quote) -> Decimal:
        price = self.mark(quote)
        if self.side == "BUY":
            return self.units * (price - self.entry)
        return self.units * (self.entry - price)

    def hit_stop(self, quote: Quote) -> bool:
        if self.side == "BUY":
            return quote.bid <= self.stop_loss
        return quote.ask >= self.stop_loss

    def hit_take(self, quote: Quote) -> bool:
        if self.side == "BUY":
            return quote.bid >= self.take_profit
        return quote.ask <= self.take_profit


@dataclass
class PaperAccount:
    cash_eur: Decimal = STARTING_CAPITAL_EUR
    positions: dict[str, Position] = field(default_factory=dict)
    cooldowns: dict[str, float] = field(default_factory=dict)
    realized_pnl_eur: Decimal = Decimal("0")
    data_source: str = "yahoo"

    def equity_eur(self, quotes: dict[str, Quote], eurusd: Decimal) -> Decimal:
        floating = Decimal("0")
        for pos in self.positions.values():
            quote = quotes.get(pos.instrument)
            if quote is None or eurusd <= 0:
                continue
            floating += pos.unrealized_usd(quote) / eurusd
        return self.cash_eur + floating


class DataError(RuntimeError):
    pass


class OandaFeed:
    def __init__(self, token: str, account_id: str) -> None:
        self.token = token
        self.account_id = account_id
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept-Datetime-Format": "RFC3339",
                "Content-Type": "application/json",
            }
        )
        if not self.account_id:
            self.account_id = self._first_account()

    def _url(self, path: str) -> str:
        return f"{OANDA_BASE_URL}{path}"

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = self.session.get(self._url(path), params=params, timeout=20)
        if response.status_code >= 400:
            raise DataError(f"OANDA {response.status_code}: {response.text[:300]}")
        return response.json()

    def _first_account(self) -> str:
        data = self._get("/v3/accounts")
        accounts = data.get("accounts") or []
        if not accounts:
            raise DataError("OANDA token has no practice accounts")
        account_id = str(accounts[0]["id"])
        log("CONNECT", f"OANDA account auto-selected {account_id}")
        return account_id

    def quotes(self) -> dict[str, Quote]:
        names = ",".join(spec.oanda for spec in INSTRUMENTS)
        data = self._get(
            f"/v3/accounts/{self.account_id}/pricing",
            {"instruments": names},
        )
        out: dict[str, Quote] = {}
        for row in data.get("prices") or []:
            instrument = row.get("instrument")
            if instrument not in SPECS:
                continue
            bids = row.get("bids") or []
            asks = row.get("asks") or []
            if not bids or not asks:
                continue
            stamp = row.get("time") or utc_now().isoformat()
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                when = utc_now()
            out[instrument] = Quote(
                instrument=instrument,
                bid=to_decimal(bids[0]["price"]),
                ask=to_decimal(asks[0]["price"]),
                time=when,
                tradeable=bool(row.get("tradeable", True)),
            )
        return out

    def candles(self, instrument: str) -> list:
        data = self._get(
            f"/v3/instruments/{instrument}/candles",
            {
                "granularity": OANDA_GRANULARITY,
                "count": str(OHLCV_LIMIT),
                "price": "M",
            },
        )
        rows = []
        for candle in data.get("candles") or []:
            mid = candle.get("mid") or {}
            if not mid:
                continue
            stamp = candle.get("time") or ""
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            rows.append(
                [
                    int(when.timestamp() * 1000),
                    to_decimal(mid.get("o")),
                    to_decimal(mid.get("h")),
                    to_decimal(mid.get("l")),
                    to_decimal(mid.get("c")),
                    to_decimal(candle.get("volume") or 0),
                ]
            )
        return rows


class YahooFeed:
    """Key-free fallback: delayed Yahoo Finance charts for FX and the S&P 500."""

    CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; forex-paper-bot/1.0)",
                "Accept": "application/json",
            }
        )

    def _chart(self, symbol: str, interval: str, range_: str) -> dict:
        response = self.session.get(
            self.CHART.format(symbol=symbol),
            params={"interval": interval, "range": range_},
            timeout=20,
        )
        if response.status_code >= 400:
            raise DataError(f"Yahoo {symbol} {response.status_code}: {response.text[:200]}")
        payload = response.json()
        results = ((payload.get("chart") or {}).get("result") or [None])[0]
        if not results:
            raise DataError(f"Yahoo returned no chart for {symbol}")
        return results

    def quotes(self) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for spec in INSTRUMENTS:
            chart = self._chart(spec.yahoo, YAHOO_INTERVAL, "1d")
            meta = chart.get("meta") or {}
            price = to_decimal(meta.get("regularMarketPrice") or meta.get("previousClose") or 0)
            if price <= 0:
                timestamps = chart.get("timestamp") or []
                closes = ((chart.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
                for _ts_unix, close in zip(reversed(timestamps), reversed(closes)):
                    if close is not None:
                        price = to_decimal(close)
                        break
            if price <= 0:
                continue
            half = spec.typical_spread / Decimal("2")
            stamp = meta.get("regularMarketTime")
            when = datetime.fromtimestamp(int(stamp), tz=timezone.utc) if stamp else utc_now()
            age = (utc_now() - when).total_seconds()
            out[spec.oanda] = Quote(
                instrument=spec.oanda,
                bid=price - half,
                ask=price + half,
                time=when,
                tradeable=age <= STALE_SECONDS,
            )
        return out

    def candles(self, instrument: str) -> list:
        spec = SPECS[instrument]
        range_ = "5d" if YAHOO_INTERVAL == "5m" else "1mo"
        chart = self._chart(spec.yahoo, YAHOO_INTERVAL, range_)
        timestamps = chart.get("timestamp") or []
        quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
        rows = []
        for i, ts_unix in enumerate(timestamps):
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            volumes = quote.get("volume") or []
            if i >= len(opens) or any(item is None for item in (opens[i], highs[i], lows[i], closes[i])):
                continue
            vol = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
            rows.append(
                [
                    int(ts_unix) * 1000,
                    to_decimal(opens[i]),
                    to_decimal(highs[i]),
                    to_decimal(lows[i]),
                    to_decimal(closes[i]),
                    to_decimal(vol),
                ]
            )
        return rows[-OHLCV_LIMIT:]


def connect_feed() -> tuple[Any, str]:
    if OANDA_API_KEY:
        try:
            feed = OandaFeed(OANDA_API_KEY, OANDA_ACCOUNT_ID)
            quotes = feed.quotes()
            if quotes:
                log("CONNECT", f"OANDA practice feed online ({OANDA_BASE_URL})")
                return feed, "oanda"
            log("WARN", "OANDA returned no prices - falling back to Yahoo")
        except Exception as exc:  # noqa: BLE001
            log("WARN", f"OANDA unavailable ({exc}) - falling back to Yahoo")
    else:
        log(
            "CONNECT",
            "No OANDA_API_KEY in .env - using Yahoo Finance for live quotes (paper only)",
        )
    return YahooFeed(), "yahoo"


class PaperBroker:
    def __init__(self) -> None:
        self.feed, source = connect_feed()
        self.account = PaperAccount(data_source=source)
        self._restore()

    def quotes(self) -> dict[str, Quote]:
        return self.feed.quotes()

    def candles(self, instrument: str) -> list:
        return self.feed.candles(instrument)

    def eurusd_rate(self, quotes: dict[str, Quote]) -> Decimal:
        quote = quotes.get("EUR_USD")
        if quote and quote.mid > 0:
            return quote.mid
        return Decimal("1.10")

    def lot_units(self, spec: InstrumentSpec) -> Decimal:
        units = MAX_LOT * spec.units_per_lot
        if spec.units_per_lot >= 1000:
            units = units.quantize(Decimal("1"), rounding=ROUND_DOWN)
        else:
            units = units.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        return units if units > 0 else Decimal("0")

    def open_position(
        self,
        spec: InstrumentSpec,
        side: str,
        quote: Quote,
        stop: Decimal,
        take: Decimal,
    ) -> Position | None:
        units = self.lot_units(spec)
        if units <= 0:
            log("SKIP", f"{spec.display} lot size rounded to zero")
            return None
        entry = quote.ask if side == "BUY" else quote.bid
        pos = Position(
            instrument=spec.oanda,
            side=side,
            units=units,
            entry=entry,
            stop_loss=stop,
            take_profit=take,
            opened_at=ts(),
            lots=MAX_LOT,
        )
        self.account.positions[spec.oanda] = pos
        self._save()
        return pos

    def close_position(
        self,
        pos: Position,
        quote: Quote,
        eurusd: Decimal,
        reason: str,
    ) -> Decimal:
        exit_price = pos.mark(quote)
        pnl_usd = pos.unrealized_usd(quote)
        pnl_eur = (pnl_usd / eurusd).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if eurusd else Decimal("0")
        self.account.cash_eur += pnl_eur
        self.account.realized_pnl_eur += pnl_eur
        self.account.positions.pop(pos.instrument, None)
        self._save()
        spec = SPECS[pos.instrument]
        log(
            "EXIT",
            f"{spec.display} {pos.side} closed {reason} @ {fmt_price(spec, exit_price)} "
            f"PnL {pnl_eur:+.2f} EUR",
        )
        return pnl_eur

    def _restore(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if data.get("cash_eur") is not None:
            self.account.cash_eur = to_decimal(data["cash_eur"])
        self.account.realized_pnl_eur = to_decimal(data.get("realized_pnl_eur") or 0)
        for key, row in (data.get("positions") or {}).items():
            if key not in SPECS or not isinstance(row, dict) or "side" not in row:
                continue
            self.account.positions[key] = Position(
                instrument=key,
                side=row["side"],
                units=to_decimal(row["units"]),
                entry=to_decimal(row["entry"]),
                stop_loss=to_decimal(row["stop_loss"]),
                take_profit=to_decimal(row["take_profit"]),
                opened_at=str(row.get("opened_at") or ts()),
                lots=to_decimal(row.get("lots") or MAX_LOT),
            )
        log(
            "STATE",
            f"Restored paper account {self.account.cash_eur:.2f} EUR cash, "
            f"{len(self.account.positions)} open",
        )

    def _save(self) -> None:
        payload = {
            "cash_eur": str(self.account.cash_eur),
            "realized_pnl_eur": str(self.account.realized_pnl_eur),
            "data_source": self.account.data_source,
            "positions": {
                key: {
                    "side": pos.side,
                    "units": str(pos.units),
                    "entry": str(pos.entry),
                    "stop_loss": str(pos.stop_loss),
                    "take_profit": str(pos.take_profit),
                    "opened_at": pos.opened_at,
                    "lots": str(pos.lots),
                }
                for key, pos in self.account.positions.items()
            },
        }
        try:
            STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log("WARN", f"Could not persist state file: {exc}")
