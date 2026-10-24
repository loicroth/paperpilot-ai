"""Pure-Python indicators on CLOSED candles, with no forward data or synthetic prices."""
from __future__ import annotations

from dataclasses import asdict
import math
from typing import Sequence

from .market import Candle, MarketError


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(result[-1] + alpha * (value - result[-1]))
    return result


def _wilder(values: list[float], period: int) -> float:
    avg = sum(values[:period]) / period
    for value in values[period:]:
        avg = (avg * (period - 1) + value) / period
    return avg


def snapshot(candles: Sequence[Candle]) -> dict:
    if len(candles) < 60:
        raise MarketError("Au moins 60 bougies clôturées sont nécessaires aux indicateurs.")
    for i, c in enumerate(candles):
        if not all(math.isfinite(v) for v in (c.open, c.high, c.low, c.close, c.volume)):
            raise MarketError("Valeurs non finies dans les indicateurs.")
        if min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0 or not c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high:
            raise MarketError("Bougie invalide dans les indicateurs.")
        if i and c.open_time <= candles[i - 1].close_time:
            raise MarketError("Bougies non chronologiques.")
    closes = [c.close for c in candles]
    ema9, ema21, ema50 = (_ema(closes, n) for n in (9, 21, 50))
    fast, slow = _ema(closes, 12), _ema(closes, 26)
    macd = [a - b for a, b in zip(fast, slow)]
    signal = _ema(macd, 9)
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = _wilder([max(0., d) for d in changes], 14)
    loss = _wilder([max(0., -d) for d in changes], 14)
    rsi = 50. if gain == loss == 0 else 100. if loss == 0 else 100 - 100 / (1 + gain / loss)
    trs = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
           for p, c in zip(candles, candles[1:])]
    atr = _wilder(trs, 14)
    prior = list(candles[-21:-1])
    avg_volume = sum(c.volume for c in prior) / len(prior)
    support, resistance = min(c.low for c in prior), max(c.high for c in prior)
    result = {
        "ema9": ema9[-1], "ema21": ema21[-1], "ema50": ema50[-1],
        "ema21_previous": ema21[-2], "ema50_previous": ema50[-2],
        "rsi14": rsi, "macd": macd[-1], "macd_signal": signal[-1],
        "macd_hist": macd[-1] - signal[-1], "macd_hist_previous": macd[-2] - signal[-2],
        "atr14": atr, "atr_pct": atr / closes[-1] * 100,
        "support": support, "resistance": resistance,
        "recent_high": max(c.high for c in candles[-5:]),
        "recent_low": min(c.low for c in candles[-5:]),
        "volume_available": all(getattr(c,"volume_available",True) for c in candles) and any(c.volume>0 for c in candles),
        "volume_ratio": candles[-1].volume / avg_volume if avg_volume > 0 else 0.,
        "change_pct": (closes[-1] / closes[-2] - 1) * 100,
        "last": asdict(candles[-1]), "previous": asdict(candles[-2]),
        "candles": [asdict(c) for c in candles[-40:]],
        "trend": "haussiere" if closes[-1] > ema21[-1] > ema50[-1] else
                 "baissiere" if closes[-1] < ema21[-1] < ema50[-1] else "neutre",
    }
    if not all(math.isfinite(v) for v in result.values() if isinstance(v, (int, float))):
        raise MarketError("Indicateur non fini ; entrée interdite.")
    return result


def candidates(m5: dict, h1: dict, require_volume: bool = True) -> list[str]:
    """Deterministic LONG setup admission; IA chooses at most one admissible market.

    Breakout, retest, consolidation resumption need an upward H1 context.
    Reversal additionally needs an improving H1 structure above EMA21.
    These are constraints, not a claim of a profitable edge.
    """
    volume_ok = lambda threshold: (not require_volume and not m5.get("volume_available",True)) or m5["volume_ratio"] >= threshold
    last, previous = m5["last"], m5["previous"]
    close, atr = last["close"], m5["atr14"]
    if atr <= 0 or not .03 <= m5["atr_pct"] <= 3:
        return []
    if last["high"] - last["low"] > 2.5 * atr or close - m5["ema21"] > 2.5 * atr:
        return []
    if not 40 <= m5["rsi14"] <= 76 or last["close"] <= last["open"]:
        return []
    if h1["last"]["close"] < h1["ema21"] or h1["ema21"] < h1["ema21_previous"]:
        return []
    uptrend = h1["ema21"] > h1["ema50"]
    momentum = m5["ema9"] > m5["ema21"] and m5["macd_hist"] > 0
    result = []
    rows = m5["candles"]
    if uptrend and momentum and close > m5["resistance"] and volume_ok(1.2):
        result.append("breakout")
    # Previous close must have broken the earlier range, then latest retests it from above.
    if len(rows) >= 22:
        old_resistance = max(c["high"] for c in rows[-22:-2])
        if uptrend and momentum and previous["close"] > old_resistance and last["low"] <= old_resistance + .3 * atr and last["low"] >= old_resistance - .5 * atr and close > old_resistance and volume_ok(.8):
            result.append("retest")
    if len(rows) >= 8:
        box = rows[-7:-1]
        box_high, box_low = max(c["high"] for c in box), min(c["low"] for c in box)
        if uptrend and momentum and box_high - box_low <= 2 * atr and close > box_high and volume_ok(1.1):
            result.append("consolidation")
    if len(rows) >= 12:
        older_low = min(c["low"] for c in rows[-12:-6])
        newer_low = min(c["low"] for c in rows[-6:-1])
        pivot = max(c["high"] for c in rows[-4:-1])
        if newer_low > older_low and close > pivot and close > m5["ema21"] and m5["macd_hist"] > m5["macd_hist_previous"] and volume_ok(1.2):
            result.append("reversal")
    return result


def short_candidates(m5: dict, h1: dict, require_volume: bool = True) -> list[str]:
    """Mirror the exact LONG structural filters, solely for authorised CFDs."""
    def mirror(data):
        # Reflection about a positive anchor preserves EMA distances, ATR and volume.
        anchor = 2 * max(data['recent_high'], data['resistance'], max(c['high'] for c in data['candles']), data['ema50'], data['ema21'])
        def bar(c):
            return {**c, 'open':anchor-c['open'], 'close':anchor-c['close'], 'high':anchor-c['low'], 'low':anchor-c['high']}
        result = dict(data)
        for key in ('ema9','ema21','ema50','ema21_previous','ema50_previous'):
            result[key] = anchor-data[key]
        for key in ('macd','macd_signal','macd_hist','macd_hist_previous'): result[key] = -data[key]
        result.update(rsi14=100-data['rsi14'],support=anchor-data['resistance'],resistance=anchor-data['support'],
            recent_low=anchor-data['recent_high'],recent_high=anchor-data['recent_low'],
            last=bar(data['last']),previous=bar(data['previous']),candles=[bar(c) for c in data['candles']])
        return result
    return candidates(mirror(m5),mirror(h1),require_volume=require_volume)


def long_refusals(m5: dict, h1: dict) -> list[str]:
    """Explain existing gates without relaxing or replacing the strategy."""
    last=m5['last'];atr=m5['atr14'];reasons=[]
    if atr<=0 or not .03<=m5['atr_pct']<=3:reasons.append('ATR hors plage')
    if last['high']-last['low']>2.5*atr or last['close']-m5['ema21']>2.5*atr:reasons.append('Bougie ou prix trop étendu')
    if not 40<=m5['rsi14']<=76:reasons.append('RSI hors plage 40–76')
    if last['close']<=last['open']:reasons.append('Bougie M5 non haussière')
    if h1['last']['close']<h1['ema21']:reasons.append('H1 sous EMA21 : achat non autorisé')
    if h1['ema21']<h1['ema21_previous']:reasons.append('EMA21 H1 descendante')
    if not reasons:reasons.append('Cassure, retest, reprise ou retournement confirmé absent ; volume éventuellement insuffisant')
    return reasons
