#!/usr/bin/env python3
"""
Binance Spot scalping bot for Render.
Trades BTC/USDT, ETH/USDT, SOL/USDT. Sizes each position at 33% of live free USDT.
Set DRY_RUN = True to paper-trade without placing live orders.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ccxt
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
DRY_RUN = True  # True = paper trading (no live Binance orders). Set False to go live.
CCXT_SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
QUOTE_ASSET = "USDT"
BINANCE_TAKER_FEE = Decimal("0.001")  # 0.1% per side (~0.2% round-trip)
TAKE_PROFIT_PCT = Decimal("0.0075")  # +0.75% from entry (gross)
TAKE_PROFIT_NET_PCT = TAKE_PROFIT_PCT - (BINANCE_TAKER_FEE * 2)  # ~+0.55% after fees
DAILY_STOP_LOSS_PCT = Decimal("0.02")
MAX_ALLOCATION_PCT = Decimal("0.33")  # 33% of available capital per position
MAX_OPEN_POSITIONS = 3
POLL_SECONDS = 5
COOLDOWN_AFTER_TP_SECONDS = 15
ENTRY_TIMEFRAME = "5m"
RSI_PERIOD = 14
RSI_OVERSOLD = Decimal("42")
ATR_PERIOD = 14
ATR_SPIKE_MULT = Decimal("2.5")
VOL_LOOKBACK = 20
VOL_ZSCORE_MAX = Decimal("2")
OHLCV_LIMIT = 80
CLIENT_ID_PREFIX = "sbot"
STATE_FILE = Path(__file__).with_name("bot_state_paper.json" if DRY_RUN else "bot_state.json")
LOG_FILE = Path(__file__).with_name("bot.log")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ts() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


def log(kind: str, message: str) -> None:
    line = f"[{ts()}] {kind:<9} {message}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def to_decimal(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def fmt_qty(qty: Decimal) -> str:
    return f"{qty.normalize():f}"


def env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip().strip('"').strip("'").replace("\n", "").replace("\r", "")
        if value:
            return value
    return ""


def compute_rsi(closes: list[Decimal], period: int = RSI_PERIOD) -> Decimal | None:
    if len(closes) < period + 1:
        return None
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for prev, curr in zip(closes, closes[1:]):
        delta = curr - prev
        gains.append(delta if delta > 0 else Decimal("0"))
        losses.append(-delta if delta < 0 else Decimal("0"))
    avg_gain = sum(gains[:period], Decimal("0")) / Decimal(period)
    avg_loss = sum(losses[:period], Decimal("0")) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)
    if avg_loss == 0:
        return Decimal("100") if avg_gain > 0 else Decimal("50")
    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def compute_stdev(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal("0")
    mean = sum(values, Decimal("0")) / Decimal(len(values))
    variance = sum((item - mean) ** 2 for item in values) / Decimal(len(values) - 1)
    if variance <= 0:
        return Decimal("0")
    return variance.sqrt()


def compute_atr(candles: list, period: int = ATR_PERIOD) -> Decimal | None:
    if len(candles) < period + 1:
        return None
    trs: list[Decimal] = []
    for prev, curr in zip(candles, candles[1:]):
        high = to_decimal(curr[2])
        low = to_decimal(curr[3])
        prev_close = to_decimal(prev[4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    atr = sum(trs[:period], Decimal("0")) / Decimal(period)
    for tr in trs[period:]:
        atr = (atr * Decimal(period - 1) + tr) / Decimal(period)
    return atr


def within_normal_volatility(candles: list, price: Decimal) -> tuple[bool, str]:
    need = max(VOL_LOOKBACK, ATR_PERIOD) + 1
    if len(candles) < need:
        return False, f"need {need} candles, got {len(candles)}"
    closes = [to_decimal(row[4]) for row in candles[-VOL_LOOKBACK:]]
    mean = sum(closes, Decimal("0")) / Decimal(len(closes))
    stdev = compute_stdev(closes)
    if stdev > 0:
        zscore = abs(price - mean) / stdev
        if zscore > VOL_ZSCORE_MAX:
            return False, f"z-score {zscore:.2f} > {VOL_ZSCORE_MAX}"
    atr = compute_atr(candles, ATR_PERIOD)
    last_range = to_decimal(candles[-1][2]) - to_decimal(candles[-1][3])
    if atr is not None and atr > 0 and last_range > ATR_SPIKE_MULT * atr:
        return False, f"bar range {last_range:.6f} > {ATR_SPIKE_MULT}x ATR {atr:.6f}"
    atr_txt = f"{atr:.6f}" if atr is not None else "n/a"
    return True, f"z<= {VOL_ZSCORE_MAX} ATR {atr_txt}"


def take_profit_price(entry: Decimal) -> Decimal:
    return entry * (Decimal("1") + TAKE_PROFIT_PCT)


@dataclass
class SymbolSpec:
    ccxt_symbol: str
    base: str
    min_amount: Decimal = Decimal("0")
    min_cost: Decimal = Decimal("5")
    amount_step: Decimal = Decimal("0.00000001")


@dataclass
class Position:
    entry_price: Decimal
    qty: Decimal


@dataclass
class BotState:
    day_key: str | None = None
    day_start_equity: Decimal | None = None
    halted_until_date: str | None = None
    positions: dict[str, Position] = field(default_factory=dict)
    cooldowns: dict[str, float] = field(default_factory=dict)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"binance-spot-bot ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log("HEALTH", f"HTTP health endpoint on 0.0.0.0:{port}")


class SpotLiveBot:
    def __init__(self) -> None:
        api_key = env_first("BINANCE_API_KEY")
        api_secret = env_first("BINANCE_API_SECRET", "BINANCE_SECRET_KEY")
        if not DRY_RUN and (not api_key or not api_secret):
            raise SystemExit(
                "Missing BINANCE_API_KEY / BINANCE_API_SECRET. "
                "Set them as environment variables (Render Dashboard > Environment)."
            )

        config = {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {
                "defaultType": "spot",
                "adjustForTimeDifference": True,
                "recvWindow": 10000,
                "fetchCurrencies": False,
                "fetchMarkets": {"types": ["spot"]},
            },
        }
        proxy_url = env_first("BOT_HTTPS_PROXY", "BOT_HTTP_PROXY")
        if proxy_url:
            config["proxies"] = {"http": proxy_url, "https": proxy_url}
        self.exchange = ccxt.binance(config)
        self.exchange.set_sandbox_mode(False)
        self.exchange.has["fetchCurrencies"] = False
        self.exchange.options["fetchMarkets"] = {"types": ["spot"]}
        self.exchange.options["defaultType"] = "spot"
        self.specs: dict[str, SymbolSpec] = {}
        self.state = BotState()
        self.capital_usdt = Decimal("0")
        self.private_ok = False
        self._last_auth_try = 0.0
        self.paper_balances: dict[str, Decimal] = {}

    def connect(self) -> None:
        try:
            self._load_spot_markets_public()
            self._load_specs()
            self._restore_state()
        except ccxt.BaseError as exc:
            self._abort_if_geo_blocked(exc)
            raise
        if DRY_RUN:
            self._init_paper_wallet()
            self.private_ok = True
            log("CONNECT", "DRY_RUN paper trading - public prices only, no live Binance orders")
        else:
            self.private_ok = self._probe_private_api()
            if self.private_ok:
                try:
                    self._recover_positions()
                except ccxt.AuthenticationError as exc:
                    self.private_ok = False
                    log("ERROR", f"Position recover failed auth: {exc}")
                live_usdt = self.get_balances().get(QUOTE_ASSET, Decimal("0"))
                self.capital_usdt = live_usdt
                log("CONNECT", "Binance Spot LIVE API connected")
                log(
                    "CAPITAL",
                    f"Live free USDT {live_usdt:.2f} - each position uses "
                    f"{MAX_ALLOCATION_PCT * 100:.0f}% (~{(live_usdt * MAX_ALLOCATION_PCT):.2f} USDT)",
                )
            else:
                log(
                    "AUTH",
                    "Public market data works. Trading is paused until Binance accepts "
                    "the API key (Enable Reading + Spot trading, IP unrestricted).",
                )
        if DRY_RUN:
            log(
                "CAPITAL",
                f"Paper USDT {self.paper_balances.get(QUOTE_ASSET, Decimal('0')):.2f} - "
                f"{MAX_ALLOCATION_PCT * 100:.0f}% per position",
            )

    def _probe_private_api(self) -> bool:
        try:
            self.exchange.fetch_balance({"type": "spot"})
            log("AUTH", "Private Spot API accepted the key")
            return True
        except ccxt.AuthenticationError as exc:
            log(
                "ERROR",
                "Binance rejected the API key (-2015) on fetch_balance. "
                "This is not a missing Render env var. In Binance API Management "
                "enable Reading + Spot trading and set IP access to Unrestricted.",
            )
            log("ERROR", str(exc))
            return False

    def _load_spot_markets_public(self) -> None:
        """Load Spot markets from public exchangeInfo only. Never call signed margin/sapi."""
        client = self.exchange
        saved_key, saved_secret = client.apiKey, client.secret
        client.apiKey = None
        client.secret = None
        try:
            client.options.setdefault("crossMarginPairsData", [])
            client.options.setdefault("isolatedMarginPairsData", [])
            raw = client.publicGetExchangeInfo()
            wanted = {symbol.replace("/", "") for symbol in CCXT_SYMBOLS}
            rows = [
                row
                for row in (raw.get("symbols") or [])
                if row.get("symbol") in wanted and row.get("status") == "TRADING"
            ]
            if len(rows) != len(wanted):
                found = {row.get("symbol") for row in rows}
                missing = ", ".join(sorted(wanted - found)) or "none"
                raise RuntimeError(f"Missing Spot symbols in exchangeInfo: {missing}")
            markets = client.parse_markets(rows)
            client.set_markets(markets)
            log("CONNECT", f"Loaded Spot markets: {', '.join(CCXT_SYMBOLS)}")
        finally:
            client.apiKey = saved_key
            client.secret = saved_secret

    @staticmethod
    def _abort_if_geo_blocked(exc: BaseException) -> None:
        text = str(exc)
        if "451" not in text and "restricted location" not in text.lower():
            return
        log(
            "ERROR",
            "Binance.com returned HTTP 451 (restricted location). "
            "Render Oregon/US West is in the United States, and Binance.com "
            "does not allow API access from the US. Move this service to "
            "Frankfurt or Singapore in Render Dashboard > Settings > Region, "
            "then redeploy. A US region cannot trade on Binance.com.",
        )
        log("ERROR", text)
        while True:
            time.sleep(300)
            log("ERROR", "Still geo-blocked. Change Render region to Frankfurt or Singapore.")

    @staticmethod
    def _abort_if_auth_error(exc: BaseException) -> None:
        text = str(exc)
        log(
            "ERROR",
            "Binance rejected the API key (-2015). For Render you need a LIVE "
            "Spot key with Enable Reading + Enable Spot Trading, and IP access "
            "set to unrestricted (Render IPs change). Do not enable withdrawals. "
            "Margin permission is not required; the bot is Spot-only.",
        )
        log("ERROR", text)
        while True:
            time.sleep(300)
            log("ERROR", "Still waiting on a valid unrestricted Binance Spot API key.")

    def _load_specs(self) -> None:
        for symbol in CCXT_SYMBOLS:
            market = self.exchange.market(symbol)
            limits = market.get("limits") or {}
            min_amount = to_decimal((limits.get("amount") or {}).get("min") or 0)
            min_cost = to_decimal((limits.get("cost") or {}).get("min") or 5)
            precision = market.get("precision") or {}
            amount_digits = precision.get("amount")
            if isinstance(amount_digits, int):
                step = Decimal("1").scaleb(-amount_digits)
            else:
                step = to_decimal(amount_digits or "0.00000001")
            self.specs[symbol] = SymbolSpec(
                ccxt_symbol=symbol,
                base=market["base"],
                min_amount=min_amount,
                min_cost=min_cost if min_cost > 0 else Decimal("5"),
                amount_step=step if step > 0 else Decimal("0.00000001"),
            )

    def _public_last_price(self, binance_symbol: str) -> Decimal:
        raw = self.exchange.publicGetTickerPrice({"symbol": binance_symbol})
        return to_decimal(raw.get("price"))

    def _resolve_capital(self) -> Decimal:
        usdt_raw = env_first("TRADE_CAPITAL_USDT")
        eur_raw = env_first("TRADE_CAPITAL_EUR")
        if usdt_raw and not eur_raw:
            return to_decimal(usdt_raw).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        amount_eur = to_decimal(eur_raw or "250")
        try:
            rate = self._public_last_price("EURUSDT")
            capital = (amount_eur * rate).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            log("CAPITAL", f"{amount_eur} EUR x {rate:.4f} EURUSDT = {capital:.2f} USDT")
            return capital
        except Exception as exc:  # noqa: BLE001
            fallback = Decimal("250.00")
            log("WARN", f"EURUSDT ticker unavailable ({exc}); using {fallback:.2f} USDT")
            return fallback

    def _restore_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.state.day_key = data.get("day_key")
        if data.get("day_start_equity") is not None:
            self.state.day_start_equity = to_decimal(data["day_start_equity"])
        self.state.halted_until_date = data.get("halted_until_date")
        for symbol, row in (data.get("positions") or {}).items():
            if symbol not in self.specs:
                continue
            self.state.positions[symbol] = Position(
                entry_price=to_decimal(row["entry_price"]),
                qty=to_decimal(row["qty"]),
            )
        if DRY_RUN:
            self.paper_balances = {
                str(asset): to_decimal(qty)
                for asset, qty in (data.get("paper_balances") or {}).items()
            }

    def _save_state(self) -> None:
        payload = {
            "day_key": self.state.day_key,
            "day_start_equity": (
                str(self.state.day_start_equity)
                if self.state.day_start_equity is not None
                else None
            ),
            "halted_until_date": self.state.halted_until_date,
            "positions": {
                symbol: {"entry_price": str(pos.entry_price), "qty": str(pos.qty)}
                for symbol, pos in self.state.positions.items()
            },
        }
        if DRY_RUN:
            payload["paper_balances"] = {
                asset: str(qty) for asset, qty in self.paper_balances.items()
            }
        try:
            STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log("WARN", f"Could not persist state file: {exc}")

    def _init_paper_wallet(self) -> None:
        wanted = [QUOTE_ASSET, *(spec.base for spec in self.specs.values())]
        if QUOTE_ASSET not in self.paper_balances:
            live_usdt = self._try_live_free_usdt()
            if live_usdt is not None and live_usdt > 0:
                self.paper_balances[QUOTE_ASSET] = live_usdt
                log("PAPER", f"Seeded virtual USDT from live Binance free balance: {live_usdt:.2f}")
            else:
                self.paper_balances[QUOTE_ASSET] = self._resolve_capital()
        for asset in wanted:
            self.paper_balances.setdefault(asset, Decimal("0"))
        self.capital_usdt = self.paper_balances.get(QUOTE_ASSET, Decimal("0"))
        held = "  ".join(
            f"{asset} {fmt_qty(self.paper_balances[asset])}"
            if asset != QUOTE_ASSET
            else f"USDT {self.paper_balances[asset]:.2f}"
            for asset in wanted
            if asset == QUOTE_ASSET or self.paper_balances[asset] > 0
        )
        log("PAPER", f"Virtual wallet: {held}")

    def _try_live_free_usdt(self) -> Decimal | None:
        if not env_first("BINANCE_API_KEY"):
            return None
        try:
            raw = self.exchange.fetch_balance({"type": "spot"})
            return to_decimal((raw.get("free") or {}).get(QUOTE_ASSET) or 0)
        except ccxt.BaseError as exc:
            log("WARN", f"Could not read live free USDT ({exc})")
            return None

    def _recover_positions(self) -> None:
        """Rebuild bot positions from tagged orders if the disk state was lost (Render)."""
        balances = self.get_balances()
        for symbol, spec in self.specs.items():
            free_qty = balances.get(spec.base, Decimal("0"))
            if free_qty < spec.min_amount:
                wallet = Decimal("0")
            else:
                wallet = self.sellable_qty(symbol, free_qty)
            if symbol in self.state.positions:
                if wallet <= 0:
                    log("RECOVER", f"{symbol}: state had a position but wallet is empty - clearing")
                    self.state.positions.pop(symbol, None)
                continue
            if wallet <= 0:
                continue
            recovered = self._infer_bot_position(symbol, wallet)
            if recovered:
                self.state.positions[symbol] = recovered
                log(
                    "RECOVER",
                    f"{symbol}: restored bot position {fmt_qty(recovered.qty)} @ {recovered.entry_price:.4f}",
                )
            else:
                log(
                    "IGNORE",
                    f"{spec.base} wallet {fmt_qty(wallet)} was not bought by this bot - left untouched",
                )
        self._save_state()

    def _infer_bot_position(self, symbol: str, wallet_qty: Decimal) -> Position | None:
        try:
            orders = self.exchange.fetch_closed_orders(symbol, limit=100)
        except ccxt.BaseError as exc:
            log("WARN", f"{symbol}: cannot fetch closed orders ({exc})")
            return None
        remaining = wallet_qty
        cost = Decimal("0")
        qty = Decimal("0")
        for order in reversed(orders):
            client_id = str(order.get("clientOrderId") or "")
            if not client_id.startswith(CLIENT_ID_PREFIX):
                continue
            if order.get("side") != "buy":
                continue
            filled = to_decimal(order.get("filled") or order.get("amount") or 0)
            if filled <= 0:
                continue
            avg = to_decimal(order.get("average") or order.get("price") or 0)
            take = min(filled, remaining)
            if take <= 0:
                continue
            cost += take * avg
            qty += take
            remaining -= take
            if remaining <= 0:
                break
        if qty <= 0 or cost <= 0:
            return None
        return Position(entry_price=cost / qty, qty=qty)

    def get_prices(self) -> dict[str, Decimal]:
        tickers = self.exchange.fetch_tickers(list(CCXT_SYMBOLS))
        prices: dict[str, Decimal] = {}
        for symbol in CCXT_SYMBOLS:
            row = tickers.get(symbol) or {}
            last = row.get("last") or row.get("close")
            if last is None:
                raise RuntimeError(f"Missing ticker for {symbol}")
            prices[symbol] = to_decimal(last)
        return prices

    def get_balances(self) -> dict[str, Decimal]:
        if DRY_RUN:
            return dict(self.paper_balances)
        raw = self.exchange.fetch_balance({"type": "spot"})
        free = raw.get("free") or {}
        wanted = {QUOTE_ASSET, *(spec.base for spec in self.specs.values())}
        return {asset: to_decimal(free.get(asset) or 0) for asset in wanted}

    def amount_to_lot(self, symbol: str, qty: Decimal) -> Decimal:
        if qty <= 0:
            return Decimal("0")
        try:
            precise = self.exchange.amount_to_precision(symbol, float(qty))
        except ccxt.InvalidOrder:
            return Decimal("0")
        return to_decimal(precise)

    def sellable_qty(self, symbol: str, free_qty: Decimal) -> Decimal:
        spec = self.specs[symbol]
        if free_qty <= 0 or free_qty < spec.min_amount:
            return Decimal("0")
        qty = self.amount_to_lot(symbol, free_qty)
        if qty < spec.min_amount:
            return Decimal("0")
        return qty

    def position_value(self, prices: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for symbol, pos in self.state.positions.items():
            total += pos.qty * prices[symbol]
        return total

    def spendable_usdt(self, free_usdt: Decimal, prices: dict[str, Decimal]) -> Decimal:
        return max(free_usdt, Decimal("0"))

    def bot_equity(self, free_usdt: Decimal, prices: dict[str, Decimal]) -> Decimal:
        return self.position_value(prices) + max(free_usdt, Decimal("0"))

    def roll_daily_window(self, equity: Decimal) -> None:
        today = utc_now().strftime("%Y-%m-%d")
        if self.state.day_key != today:
            self.state.day_key = today
            self.state.day_start_equity = equity
            if self.state.halted_until_date and self.state.halted_until_date <= today:
                self.state.halted_until_date = None
            log("RISK", f"New UTC day - starting equity snapshot {equity:.2f} USDT")
            self._save_state()
        elif self.state.day_start_equity is None:
            self.state.day_start_equity = equity
            self._save_state()

    def daily_pnl_pct(self, equity: Decimal) -> Decimal:
        if not self.state.day_start_equity:
            return Decimal("0")
        return (equity - self.state.day_start_equity) / self.state.day_start_equity

    def halted_today(self) -> bool:
        return self.state.halted_until_date == utc_now().strftime("%Y-%m-%d")

    def print_dashboard(
        self,
        prices: dict[str, Decimal],
        balances: dict[str, Decimal],
        equity: Decimal,
    ) -> None:
        pnl_pct = self.daily_pnl_pct(equity) * Decimal("100")
        halt = " HALTED" if self.halted_today() else ""
        print("", flush=True)
        print("=" * 86, flush=True)
        mode = "PAPER" if DRY_RUN else "USDT"
        log(
            "ACCOUNT",
            f"{mode} {balances.get(QUOTE_ASSET, Decimal('0')):.2f}  |  "
            f"Equity {equity:.2f} USDT  |  "
            f"Size {MAX_ALLOCATION_PCT * 100:.0f}%/pos  |  "
            f"Day PnL {pnl_pct:+.3f}%  |  Open {len(self.state.positions)}/{MAX_OPEN_POSITIONS}{halt}",
        )
        log(
            "PRICES",
            "  ".join(f"{self.specs[symbol].base} {prices[symbol]:.2f}" for symbol in CCXT_SYMBOLS),
        )
        for symbol in CCXT_SYMBOLS:
            spec = self.specs[symbol]
            pos = self.state.positions.get(symbol)
            price = prices[symbol]
            if not pos:
                log("POSITION", f"{spec.base:<4}  FLAT")
                continue
            move = (price - pos.entry_price) / pos.entry_price * Decimal("100")
            tp = take_profit_price(pos.entry_price)
            log(
                "POSITION",
                f"{spec.base:<4}  LONG {fmt_qty(pos.qty)}  entry {pos.entry_price:.4f}  "
                f"now {price:.2f}  unrealized {move:+.3f}%  TP {tp:.4f} "
                f"(+{TAKE_PROFIT_PCT * 100:.2f}% / net ~+{TAKE_PROFIT_NET_PCT * 100:.2f}%)",
            )
        print("=" * 86, flush=True)

    def _client_order_id(self, symbol: str, side: str) -> str:
        code = self.specs[symbol].base[:3]
        stamp = str(int(time.time() * 1000))[-10:]
        return f"{CLIENT_ID_PREFIX}{side[0]}{code}{stamp}"[:36]

    def _last_price(self, symbol: str) -> Decimal:
        ticker = self.exchange.fetch_ticker(symbol) or {}
        return to_decimal(ticker.get("last") or ticker.get("close") or 0)

    def market_buy(self, symbol: str, quote_amount: Decimal) -> dict | None:
        spec = self.specs[symbol]
        quote_amount = quote_amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if quote_amount < spec.min_cost:
            log("SKIP", f"{symbol} buy {quote_amount:.2f} USDT below min notional {spec.min_cost}")
            return None
        try:
            cost = to_decimal(self.exchange.cost_to_precision(symbol, float(quote_amount)))
        except ccxt.InvalidOrder:
            log("SKIP", f"{symbol} buy {quote_amount:.2f} USDT failed cost precision")
            return None
        if cost <= 0:
            log("SKIP", f"{symbol} buy rounded to zero by precision")
            return None
        if DRY_RUN:
            return self._paper_buy(symbol, spec, cost)
        client_id = self._client_order_id(symbol, "buy")
        log("TRADE", f"ENTRY  {symbol}  LIVE MARKET BUY  spending {cost:.2f} USDT")
        order = self.exchange.create_market_buy_order_with_cost(
            symbol,
            float(cost),
            {"newClientOrderId": client_id},
        )
        fill_qty = to_decimal(order.get("filled") or order.get("amount") or 0)
        fill_cost = to_decimal(order.get("cost") or cost)
        avg = to_decimal(order.get("average") or 0)
        if fill_qty <= 0 and fill_cost > 0:
            avg_price = self._last_price(symbol)
            fill_qty = fill_cost / avg_price if avg_price else Decimal("0")
            avg = avg_price
        if avg <= 0 and fill_qty > 0:
            avg = fill_cost / fill_qty
        self.state.positions[symbol] = Position(entry_price=avg, qty=fill_qty)
        self._save_state()
        log(
            "TRADE",
            f"ENTRY FILLED  {symbol}  bought {fmt_qty(fill_qty)} {spec.base}  "
            f"avg {avg:.4f}  cost {fill_cost:.2f} USDT  id={order.get('id')}",
        )
        return order

    def _paper_buy(self, symbol: str, spec: SymbolSpec, cost: Decimal) -> dict | None:
        free_usdt = self.paper_balances.get(QUOTE_ASSET, Decimal("0"))
        if free_usdt < cost:
            log("SKIP", f"{symbol} paper buy {cost:.2f} USDT > virtual USDT {free_usdt:.2f}")
            return None
        avg = self._last_price(symbol)
        if avg <= 0:
            log("ERROR", f"{symbol} paper buy: no last price")
            return None
        fill_qty = self.amount_to_lot(symbol, (cost / avg) * (Decimal("1") - BINANCE_TAKER_FEE))
        if fill_qty < spec.min_amount:
            log("SKIP", f"{symbol} paper buy qty below minimum after {BINANCE_TAKER_FEE * 100:.1f}% fee")
            return None
        self.paper_balances[QUOTE_ASSET] = free_usdt - cost
        self.paper_balances[spec.base] = self.paper_balances.get(spec.base, Decimal("0")) + fill_qty
        self.state.positions[symbol] = Position(entry_price=avg, qty=fill_qty)
        self._save_state()
        log(
            "PAPER",
            f"ENTRY  {symbol}  simulated MARKET BUY  spending {cost:.2f} USDT",
        )
        log(
            "PAPER",
            f"ENTRY FILLED  {symbol}  bought {fmt_qty(fill_qty)} {spec.base}  "
            f"avg {avg:.4f}  fee {BINANCE_TAKER_FEE * 100:.1f}%  "
            f"virtual USDT {self.paper_balances[QUOTE_ASSET]:.2f}",
        )
        return {"id": "paper", "filled": float(fill_qty), "average": float(avg), "cost": float(cost)}

    def market_sell(self, symbol: str, qty: Decimal, reason: str) -> dict | None:
        spec = self.specs[symbol]
        qty = self.amount_to_lot(symbol, qty)
        if qty < spec.min_amount:
            log("SKIP", f"{symbol} sell qty below minimum")
            return None
        if DRY_RUN:
            return self._paper_sell(symbol, spec, qty, reason)
        client_id = self._client_order_id(symbol, "sell")
        log("TRADE", f"EXIT  {symbol}  LIVE MARKET SELL  {fmt_qty(qty)} {spec.base}  reason={reason}")
        order = self.exchange.create_order(
            symbol,
            "market",
            "sell",
            float(qty),
            None,
            {"newClientOrderId": client_id},
        )
        fill_qty = to_decimal(order.get("filled") or qty)
        fill_cost = to_decimal(order.get("cost") or 0)
        avg = to_decimal(order.get("average") or 0)
        pos = self.state.positions.get(symbol)
        pnl_txt = ""
        if pos and fill_qty > 0 and avg > 0:
            pnl = (avg - pos.entry_price) / pos.entry_price * Decimal("100")
            pnl_txt = f"  vs entry {pnl:+.3f}%"
        self.state.positions.pop(symbol, None)
        self._save_state()
        log(
            "TRADE",
            f"EXIT FILLED  {symbol}  sold {fmt_qty(fill_qty)} {spec.base}  "
            f"avg {avg:.4f}  proceeds {fill_cost:.2f} USDT{pnl_txt}  id={order.get('id')}",
        )
        return order

    def _paper_sell(self, symbol: str, spec: SymbolSpec, qty: Decimal, reason: str) -> dict | None:
        held = self.paper_balances.get(spec.base, Decimal("0"))
        fill_qty = min(qty, held)
        fill_qty = self.amount_to_lot(symbol, fill_qty)
        if fill_qty < spec.min_amount:
            log("SKIP", f"{symbol} paper sell qty below minimum")
            return None
        avg = self._last_price(symbol)
        if avg <= 0:
            log("ERROR", f"{symbol} paper sell: no last price")
            return None
        fill_cost = (fill_qty * avg * (Decimal("1") - BINANCE_TAKER_FEE)).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        pos = self.state.positions.get(symbol)
        pnl_txt = ""
        if pos and fill_qty > 0 and avg > 0:
            gross = (avg - pos.entry_price) / pos.entry_price * Decimal("100")
            net = (
                (avg * (Decimal("1") - BINANCE_TAKER_FEE))
                / (pos.entry_price / (Decimal("1") - BINANCE_TAKER_FEE))
                - Decimal("1")
            ) * Decimal("100")
            pnl_txt = f"  vs entry {gross:+.3f}%  net after fees {net:+.3f}%"
        self.paper_balances[spec.base] = held - fill_qty
        self.paper_balances[QUOTE_ASSET] = self.paper_balances.get(QUOTE_ASSET, Decimal("0")) + fill_cost
        self.state.positions.pop(symbol, None)
        self._save_state()
        log(
            "PAPER",
            f"EXIT  {symbol}  simulated MARKET SELL  {fmt_qty(fill_qty)} {spec.base}  reason={reason}",
        )
        log(
            "PAPER",
            f"EXIT FILLED  {symbol}  sold {fmt_qty(fill_qty)} {spec.base}  "
            f"avg {avg:.4f}  proceeds {fill_cost:.2f} USDT{pnl_txt}  "
            f"virtual USDT {self.paper_balances[QUOTE_ASSET]:.2f}",
        )
        return {"id": "paper", "filled": float(fill_qty), "average": float(avg), "cost": float(fill_cost)}

    def allocation_quote(self, free_usdt: Decimal, prices: dict[str, Decimal], symbol: str) -> Decimal | None:
        spec = self.specs[symbol]
        available = max(free_usdt, Decimal("0"))
        equity = available + self.position_value(prices)
        amount = (equity * MAX_ALLOCATION_PCT).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        amount = min(amount, available)
        if amount < spec.min_cost:
            log(
                "SKIP",
                f"{symbol} size {amount:.2f} USDT below min {spec.min_cost} "
                f"(live free {available:.2f}, {MAX_ALLOCATION_PCT * 100:.0f}% of equity {equity:.2f})",
            )
            return None
        log(
            "SIZE",
            f"{symbol} allocating {amount:.2f} USDT "
            f"({MAX_ALLOCATION_PCT * 100:.0f}% of equity {equity:.2f}, free {available:.2f})",
        )
        return amount

    def fetch_ohlcv_candles(self, symbol: str) -> list:
        candles = self.exchange.fetch_ohlcv(symbol, ENTRY_TIMEFRAME, limit=OHLCV_LIMIT)
        return [row for row in candles if row and len(row) > 4]

    def entry_signal(self, symbol: str, price: Decimal) -> bool:
        try:
            candles = self.fetch_ohlcv_candles(symbol)
        except ccxt.BaseError as exc:
            log("WARN", f"{symbol} entry signal skipped - OHLCV error: {exc}")
            return False
        closes = [to_decimal(row[4]) for row in candles]
        rsi = compute_rsi(closes)
        rsi_ok = rsi is not None and rsi < RSI_OVERSOLD
        vol_ok, vol_txt = within_normal_volatility(candles, price)
        rsi_txt = f"{rsi:.1f}" if rsi is not None else "n/a"
        if rsi_ok and vol_ok:
            log(
                "SIGNAL",
                f"{symbol} BUY  RSI({RSI_PERIOD}) {rsi_txt} < {RSI_OVERSOLD} AND "
                f"volatility normal ({vol_txt})  ({ENTRY_TIMEFRAME})",
            )
            return True
        log(
            "SKIP",
            f"{symbol} no entry  RSI({RSI_PERIOD}) {rsi_txt}  vol {vol_txt}  "
            f"price {price:.4f}  (need RSI < {RSI_OVERSOLD} AND normal volatility)",
        )
        return False

    def maybe_enter(self, symbol: str, free_usdt: Decimal, prices: dict[str, Decimal]) -> Decimal:
        if symbol in self.state.positions:
            return free_usdt
        if len(self.state.positions) >= MAX_OPEN_POSITIONS:
            return free_usdt
        cooldown_until = self.state.cooldowns.get(symbol, 0.0)
        if time.time() < cooldown_until:
            remaining = int(cooldown_until - time.time())
            log("WAIT", f"{symbol} cooldown after take-profit - {remaining}s left")
            return free_usdt
        if not self.entry_signal(symbol, prices[symbol]):
            return free_usdt
        quote_amount = self.allocation_quote(free_usdt, prices, symbol)
        if quote_amount is None:
            return free_usdt
        order = self.market_buy(symbol, quote_amount)
        if order is None:
            return free_usdt
        return free_usdt - quote_amount

    def close_all(self, balances: dict[str, Decimal], reason: str) -> None:
        for symbol, spec in self.specs.items():
            pos = self.state.positions.get(symbol)
            if not pos:
                continue
            qty = self.sellable_qty(symbol, min(balances.get(spec.base, Decimal("0")), pos.qty))
            if qty <= 0:
                qty = self.sellable_qty(symbol, pos.qty)
            if qty > 0:
                self.market_sell(symbol, qty, reason=reason)
                time.sleep(0.4)
        self.state.positions.clear()
        self._save_state()

    def maybe_take_profits(self, prices: dict[str, Decimal], balances: dict[str, Decimal]) -> None:
        for symbol, pos in list(self.state.positions.items()):
            price = prices[symbol]
            target = take_profit_price(pos.entry_price)
            if price < target:
                continue
            log(
                "TRADE",
                f"TAKE-PROFIT HIT  {symbol}  price {price:.4f} >= target {target:.4f}  "
                f"+{TAKE_PROFIT_PCT * 100:.2f}% from entry  "
                f"(net ~+{TAKE_PROFIT_NET_PCT * 100:.2f}% after {BINANCE_TAKER_FEE * 200:.1f}% fees)  "
                f"from {pos.entry_price:.4f}",
            )
            qty = self.sellable_qty(symbol, min(balances.get(self.specs[symbol].base, Decimal("0")), pos.qty))
            if qty <= 0:
                qty = self.amount_to_lot(symbol, pos.qty)
            self.market_sell(symbol, qty, reason="take_profit_0.75pct")
            self.state.cooldowns[symbol] = time.time() + COOLDOWN_AFTER_TP_SECONDS
            time.sleep(0.4)

    def check_daily_stop(
        self,
        equity: Decimal,
        balances: dict[str, Decimal],
    ) -> bool:
        if not self.state.day_start_equity:
            return False
        limit = self.state.day_start_equity * (Decimal("1") - DAILY_STOP_LOSS_PCT)
        if equity > limit:
            return False
        log(
            "RISK",
            f"DAILY STOP-LOSS HIT  equity {equity:.2f} <= {limit:.2f} "
            f"(-{DAILY_STOP_LOSS_PCT * 100:.1f}% from day start {self.state.day_start_equity:.2f})",
        )
        self.close_all(balances, reason="daily_stop_loss_2pct")
        self.state.halted_until_date = utc_now().strftime("%Y-%m-%d")
        log("RISK", f"Trading halted for the rest of {self.state.halted_until_date} UTC")
        self._save_state()
        return True

    def run_loop(self) -> None:
        mode = "PAPER / DRY_RUN (no live orders)" if DRY_RUN else "LIVE Binance Spot (real funds)"
        print("=" * 86)
        print(" Binance Spot scalper")
        print(f" Pairs           : {', '.join(CCXT_SYMBOLS)}")
        print(f" Position size   : {MAX_ALLOCATION_PCT * 100:.0f}% of live free/equity USDT per trade")
        print(f" Max positions   : {MAX_OPEN_POSITIONS} (one per coin)")
        print(
            f" Entry           : RSI({RSI_PERIOD})<{RSI_OVERSOLD} AND normal volatility on {ENTRY_TIMEFRAME}"
        )
        print(
            f" Take-profit     : +{TAKE_PROFIT_PCT * 100:.2f}% from entry "
            f"(net ~+{TAKE_PROFIT_NET_PCT * 100:.2f}% after {BINANCE_TAKER_FEE * 200:.1f}% fees)"
        )
        print(f" Daily stop-loss : -{DAILY_STOP_LOSS_PCT * 100:.1f}% vs UTC-day starting equity")
        print(f" Mode            : {mode}")
        print(" Stop            : Ctrl+C  |  Render: stop the service")
        print("=" * 86)

        while True:
            try:
                if not self.private_ok:
                    prices = self.get_prices()
                    log(
                        "PRICES",
                        "  ".join(
                            f"{self.specs[symbol].base} {prices[symbol]:.2f}"
                            for symbol in CCXT_SYMBOLS
                        ),
                    )
                    now = time.time()
                    if now - self._last_auth_try >= 60:
                        self._last_auth_try = now
                        self.private_ok = self._probe_private_api()
                        if self.private_ok:
                            self._recover_positions()
                            log("CONNECT", "Private API is working - trading enabled")
                    else:
                        log("AUTH", "Trading paused - waiting for a valid unrestricted Binance Spot key")
                    time.sleep(POLL_SECONDS)
                    continue

                prices = self.get_prices()
                balances = self.get_balances()
                free_usdt = balances.get(QUOTE_ASSET, Decimal("0"))
                equity = self.bot_equity(free_usdt, prices)
                self.roll_daily_window(equity)
                self.print_dashboard(prices, balances, equity)

                if self.check_daily_stop(equity, balances):
                    time.sleep(POLL_SECONDS)
                    continue

                if self.halted_today():
                    log("RISK", "Daily stop is active - no new entries until next UTC day")
                    time.sleep(POLL_SECONDS)
                    continue

                self.maybe_take_profits(prices, balances)
                balances = self.get_balances()
                free_usdt = balances.get(QUOTE_ASSET, Decimal("0"))
                prices = self.get_prices()

                for symbol in CCXT_SYMBOLS:
                    free_usdt = self.maybe_enter(symbol, free_usdt, prices)
                    if free_usdt <= 0:
                        break

            except ccxt.InsufficientFunds as exc:
                log("ERROR", f"Insufficient funds: {exc}")
            except ccxt.NetworkError as exc:
                if "451" in str(exc) or "restricted location" in str(exc).lower():
                    self._abort_if_geo_blocked(exc)
                log("ERROR", f"Network: {exc}")
            except ccxt.BaseError as exc:
                if "451" in str(exc) or "restricted location" in str(exc).lower():
                    self._abort_if_geo_blocked(exc)
                log("ERROR", f"Binance/ccxt: {exc}")
            except Exception as exc:  # noqa: BLE001
                log("ERROR", f"{type(exc).__name__}: {exc}")

            time.sleep(POLL_SECONDS)


def main() -> None:
    start_health_server()
    bot = SpotLiveBot()
    bot.connect()
    try:
        bot.run_loop()
    except KeyboardInterrupt:
        print()
        log("STOP", "Shut down by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
