"""RSI / ATR / EMA helpers and forex entry + risk levels."""

from __future__ import annotations

from decimal import Decimal

from config import (
    ATR_PERIOD,
    ATR_SL_MULT,
    ATR_SPIKE_MULT,
    EMA_TREND_PERIOD,
    InstrumentSpec,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_PERIOD,
    TP_RATIO,
    VOL_LOOKBACK,
    VOL_ZSCORE_MAX,
)


def to_decimal(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


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


def compute_ema(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    k = Decimal("2") / Decimal(period + 1)
    ema = sum(values[:period], Decimal("0")) / Decimal(period)
    one = Decimal("1")
    for value in values[period:]:
        ema = value * k + ema * (one - k)
    return ema


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
        return False, f"bar range {last_range} > {ATR_SPIKE_MULT}x ATR {atr}"
    atr_txt = f"{atr:.6f}" if atr is not None else "n/a"
    return True, f"z<= {VOL_ZSCORE_MAX} ATR {atr_txt}"


def entry_side(candles: list, price: Decimal) -> tuple[str | None, str]:
    """Return BUY, SELL, or None plus a human-readable reason."""
    closes = [to_decimal(row[4]) for row in candles]
    rsi = compute_rsi(closes)
    ema = compute_ema(closes, EMA_TREND_PERIOD)
    vol_ok, vol_txt = within_normal_volatility(candles, price)
    rsi_txt = f"{rsi:.1f}" if rsi is not None else "n/a"
    ema_txt = f"{ema:.5f}" if ema is not None else "n/a"
    if rsi is None or ema is None:
        return None, f"warming up RSI={rsi_txt} EMA{EMA_TREND_PERIOD}={ema_txt}"
    if not vol_ok:
        return None, f"no entry RSI {rsi_txt} vol {vol_txt}"
    if price > ema and rsi < RSI_OVERSOLD:
        return (
            "BUY",
            f"BUY RSI({RSI_PERIOD}) {rsi_txt} < {RSI_OVERSOLD} "
            f"and price {price} > EMA{EMA_TREND_PERIOD} {ema_txt} ({vol_txt})",
        )
    if price < ema and rsi > RSI_OVERBOUGHT:
        return (
            "SELL",
            f"SELL RSI({RSI_PERIOD}) {rsi_txt} > {RSI_OVERBOUGHT} "
            f"and price {price} < EMA{EMA_TREND_PERIOD} {ema_txt} ({vol_txt})",
        )
    return (
        None,
        f"no entry RSI {rsi_txt} EMA{EMA_TREND_PERIOD} {ema_txt} "
        f"price {price} ({vol_txt})",
    )


def sl_tp_prices(
    spec: InstrumentSpec,
    side: str,
    entry: Decimal,
    atr: Decimal | None,
    units: Decimal,
    eurusd: Decimal,
    max_risk_eur: Decimal,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Return (stop, take, risk_eur) capped so risk <= max_risk_eur. None if unusable."""
    if units <= 0 or eurusd <= 0:
        return None
    atr_distance = (atr * ATR_SL_MULT) if atr and atr > 0 else spec.min_sl_distance
    sl_distance = max(atr_distance, spec.min_sl_distance)

    def risk_of(distance: Decimal) -> Decimal:
        return (units * distance) / eurusd

    if risk_of(sl_distance) > max_risk_eur:
        sl_distance = (max_risk_eur * eurusd) / units
    if sl_distance < spec.min_sl_distance:
        return None
    risk_eur = risk_of(sl_distance)
    if risk_eur > max_risk_eur:
        return None
    tp_distance = sl_distance * TP_RATIO
    if side == "BUY":
        stop = entry - sl_distance
        take = entry + tp_distance
    else:
        stop = entry + sl_distance
        take = entry - tp_distance
    if stop <= 0 or take <= 0:
        return None
    quantum = Decimal("1").scaleb(-spec.display_precision)
    stop = stop.quantize(quantum)
    take = take.quantize(quantum)
    return stop, take, risk_eur
