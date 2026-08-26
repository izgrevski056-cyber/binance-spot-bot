#!/usr/bin/env python3
"""Forex / CFD paper-trading bot. Trades EUR/USD, GBP/USD and S&P 500 on a 50 EUR demo book."""

from __future__ import annotations

import os
import sys
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from broker import PaperBroker, Quote, fmt_price, log, ts
from config import (
    COOLDOWN_AFTER_EXIT_SECONDS,
    INSTRUMENTS,
    MAX_LOT,
    MAX_OPEN_POSITIONS,
    MAX_RISK_EUR,
    MAX_RISK_PCT,
    OANDA_API_KEY,
    POLL_SECONDS,
    PORT,
    SPECS,
    STARTING_CAPITAL_EUR,
    TIMEFRAME_LABEL,
    TP_RATIO,
)
from strategy import compute_atr, entry_side, sl_tp_prices


TRADE_HEADER = (
    "Време               | Двойка        | Вход      | Stop-Loss | Take-Profit | PnL"
)
TRADE_RULE = "-" * len(TRADE_HEADER)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"forex-paper-bot ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def start_health_server() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log("HEALTH", f"HTTP health endpoint on 0.0.0.0:{PORT}")


def max_risk_now(equity: Decimal) -> Decimal:
    risk = (equity * MAX_RISK_PCT).quantize(Decimal("0.01"))
    if risk <= 0:
        return Decimal("0")
    return min(risk, MAX_RISK_EUR)


def log_trade_row(
    when: str,
    pair: str,
    entry: str,
    stop: str,
    take: str,
    pnl: str,
) -> None:
    line = f"{when:<19} | {pair:<13} | {entry:<9} | {stop:<9} | {take:<11} | {pnl}"
    log("TRADE", line)


class ForexPaperBot:
    def __init__(self) -> None:
        self.broker = PaperBroker()

    def print_banner(self) -> None:
        source = self.broker.account.data_source.upper()
        feed = "OANDA v20 Practice" if source == "OANDA" else "Yahoo Finance (fallback)"
        print("=" * 88, flush=True)
        print(" Forex / CFD Paper Trading Bot", flush=True)
        print(" Markets         : EUR/USD, GBP/USD, S&P 500 (CFD)", flush=True)
        print(f" Timeframe       : {TIMEFRAME_LABEL}", flush=True)
        print(f" Capital         : {STARTING_CAPITAL_EUR:.2f} EUR (fictional paper balance)", flush=True)
        print(f" Max position    : {MAX_LOT} lot (micro)", flush=True)
        print(
            f" Protection      : SL max {MAX_RISK_PCT * 100:.0f}% / {MAX_RISK_EUR:.2f} EUR   "
            f"TP ratio 1:{TP_RATIO.normalize()}",
            flush=True,
        )
        print(f" Data            : {feed}", flush=True)
        print(" Mode            : PAPER / DEMO  (no live money)", flush=True)
        print("=" * 88, flush=True)
        log("TRADE", TRADE_HEADER)
        log("TRADE", TRADE_RULE)

    def print_dashboard(self, quotes: dict[str, Quote], equity: Decimal, eurusd: Decimal) -> None:
        cash = self.broker.account.cash_eur
        floating = equity - cash
        print("", flush=True)
        print("=" * 88, flush=True)
        log(
            "ACCOUNT",
            f"Equity {equity:.2f} EUR  |  Cash {cash:.2f}  |  "
            f"Floating {floating:+.2f}  |  Realized {self.broker.account.realized_pnl_eur:+.2f}  |  "
            f"Open {len(self.broker.account.positions)}/{MAX_OPEN_POSITIONS}",
        )
        bits = []
        for spec in INSTRUMENTS:
            quote = quotes.get(spec.oanda)
            if not quote:
                bits.append(f"{spec.display} n/a")
                continue
            status = "" if quote.tradeable else " (closed)"
            bits.append(f"{spec.display} {fmt_price(spec, quote.mid)}{status}")
        log("PRICES", "  ".join(bits))
        for spec in INSTRUMENTS:
            pos = self.broker.account.positions.get(spec.oanda)
            if not pos:
                log("POSITION", f"{spec.display:<8}  FLAT")
                continue
            quote = quotes.get(spec.oanda)
            u_pnl = ""
            if quote and eurusd:
                u_eur = (pos.unrealized_usd(quote) / eurusd).quantize(Decimal("0.01"))
                u_pnl = f"  unrealized {u_eur:+.2f} EUR"
            log(
                "POSITION",
                f"{spec.display:<8}  {pos.side} {pos.lots} lot  "
                f"entry {fmt_price(spec, pos.entry)}  "
                f"SL {fmt_price(spec, pos.stop_loss)}  "
                f"TP {fmt_price(spec, pos.take_profit)}{u_pnl}",
            )
        print("=" * 88, flush=True)

    def manage_exits(self, quotes: dict[str, Quote], eurusd: Decimal) -> None:
        for key, pos in list(self.broker.account.positions.items()):
            quote = quotes.get(key)
            if not quote:
                continue
            spec = SPECS[key]
            if pos.hit_stop(quote):
                reason = "STOP-LOSS"
            elif pos.hit_take(quote):
                reason = "TAKE-PROFIT"
            else:
                continue
            pnl = self.broker.close_position(pos, quote, eurusd, reason)
            log_trade_row(
                ts(),
                f"{spec.display} {pos.side}",
                fmt_price(spec, pos.entry),
                fmt_price(spec, pos.stop_loss),
                fmt_price(spec, pos.take_profit),
                f"{pnl:+.2f} EUR ({reason})",
            )
            self.broker.account.cooldowns[key] = time.time() + COOLDOWN_AFTER_EXIT_SECONDS

    def maybe_enter(self, quotes: dict[str, Quote], equity: Decimal, eurusd: Decimal) -> None:
        if equity <= 0:
            log("RISK", "Paper equity is depleted - no new trades")
            return
        risk_cap = max_risk_now(equity)
        if risk_cap <= 0:
            return
        for spec in INSTRUMENTS:
            if spec.oanda in self.broker.account.positions:
                continue
            if len(self.broker.account.positions) >= MAX_OPEN_POSITIONS:
                return
            cooldown_until = self.broker.account.cooldowns.get(spec.oanda, 0.0)
            if time.time() < cooldown_until:
                remaining = int(cooldown_until - time.time())
                log("WAIT", f"{spec.display} cooldown after exit - {remaining}s left")
                continue
            quote = quotes.get(spec.oanda)
            if not quote:
                log("SKIP", f"{spec.display} no quote")
                continue
            if not quote.tradeable:
                log("SKIP", f"{spec.display} market closed or stale")
                continue
            try:
                candles = self.broker.candles(spec.oanda)
            except Exception as exc:  # noqa: BLE001
                log("WARN", f"{spec.display} candles unavailable: {exc}")
                continue
            if len(candles) < 60:
                log("SKIP", f"{spec.display} not enough candles ({len(candles)})")
                continue
            side, reason = entry_side(candles, quote.mid)
            if side is None:
                log("SKIP", f"{spec.display} {reason}")
                continue
            atr = compute_atr(candles)
            units = self.broker.lot_units(spec)
            levels = sl_tp_prices(spec, side, quote.ask if side == "BUY" else quote.bid, atr, units, eurusd, risk_cap)
            if levels is None:
                log(
                    "SKIP",
                    f"{spec.display} cannot fit SL within {risk_cap:.2f} EUR risk at {MAX_LOT} lot",
                )
                continue
            stop, take, risk_eur = levels
            pos = self.broker.open_position(spec, side, quote, stop, take)
            if pos is None:
                continue
            log("SIGNAL", f"{spec.display} {reason}")
            log(
                "SIZE",
                f"{spec.display} {pos.lots} lot ({pos.units} units)  "
                f"risk {risk_eur:.2f} EUR  TP 1:{TP_RATIO.normalize()}",
            )
            log_trade_row(
                pos.opened_at,
                f"{spec.display} {pos.side}",
                fmt_price(spec, pos.entry),
                fmt_price(spec, pos.stop_loss),
                fmt_price(spec, pos.take_profit),
                "open 0.00 EUR",
            )

    def run_loop(self) -> None:
        self.print_banner()
        if not OANDA_API_KEY:
            log(
                "HINT",
                "Add OANDA_API_KEY and OANDA_ACCOUNT_ID to .env for real-time OANDA practice prices. "
                "Create a free fxTrade Practice token at https://www.oanda.com",
            )
        while True:
            try:
                quotes = self.broker.quotes()
                if not quotes:
                    log("WARN", "No market data this cycle")
                    time.sleep(POLL_SECONDS)
                    continue
                eurusd = self.broker.eurusd_rate(quotes)
                equity = self.broker.account.equity_eur(quotes, eurusd)
                self.print_dashboard(quotes, equity, eurusd)
                self.manage_exits(quotes, eurusd)
                quotes = self.broker.quotes()
                eurusd = self.broker.eurusd_rate(quotes)
                equity = self.broker.account.equity_eur(quotes, eurusd)
                self.maybe_enter(quotes, equity, eurusd)
            except Exception as exc:  # noqa: BLE001
                log("ERROR", f"{type(exc).__name__}: {exc}")
            time.sleep(POLL_SECONDS)


def main() -> None:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    start_health_server()
    bot = ForexPaperBot()
    try:
        bot.run_loop()
    except KeyboardInterrupt:
        print()
        log("STOP", "Shut down by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
