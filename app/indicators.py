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
    Reversal also needs the established H1 alignment; an early neutral-H1
    bounce alone does not qualify as an entry.
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
        if uptrend and newer_low > older_low and close > pivot and close > m5["ema21"] and m5["macd_hist"] > m5["macd_hist_previous"] and volume_ok(1.2):
            result.append("reversal")
    return result


def short_candidates(m5: dict, h1: dict, require_volume: bool = True) -> list[str]:
    """Mirror the exact LONG structural filters, solely for authorised CFDs."""
    return candidates(_mirror_snapshot(m5),_mirror_snapshot(h1),require_volume=require_volume)


def _mirror_snapshot(data):
    # Reflection preserves EMA distances, ATR and volume for SHORT diagnostics too.
    anchor=2*max(data['recent_high'],data['resistance'],max(c['high'] for c in data['candles']),data['ema50'],data['ema21'])
    def bar(c):return {**c,'open':anchor-c['open'],'close':anchor-c['close'],'high':anchor-c['low'],'low':anchor-c['high']}
    result=dict(data)
    for key in ('ema9','ema21','ema50','ema21_previous','ema50_previous'):result[key]=anchor-data[key]
    for key in ('macd','macd_signal','macd_hist','macd_hist_previous'):result[key]=-data[key]
    result.update(rsi14=100-data['rsi14'],support=anchor-data['resistance'],resistance=anchor-data['support'],
        recent_low=anchor-data['recent_high'],recent_high=anchor-data['recent_low'],
        last=bar(data['last']),previous=bar(data['previous']),candles=[bar(c) for c in data['candles']])
    return result


def short_refusals(m5,h1):
    replacements={'RSI hors plage 40–76':'RSI hors plage 24–60','Bougie M5 non haussière':'Bougie M5 non baissière',
        'H1 sous EMA21 : achat non autorisé':'H1 au-dessus EMA21 : vente non autorisée',
        'EMA21 H1 descendante':'EMA21 H1 montante',
        'Tendance H1 non confirmée : EMA21 sous EMA50':'Tendance H1 non confirmée : EMA21 au-dessus EMA50'}
    return [replacements.get(reason,reason) for reason in long_refusals(_mirror_snapshot(m5),_mirror_snapshot(h1))]


def long_refusals(m5: dict, h1: dict) -> list[str]:
    """Explain existing gates without relaxing or replacing the strategy."""
    last=m5['last'];atr=m5['atr14'];reasons=[]
    if atr<=0 or not .03<=m5['atr_pct']<=3:reasons.append('ATR hors plage')
    if last['high']-last['low']>2.5*atr or last['close']-m5['ema21']>2.5*atr:reasons.append('Bougie ou prix trop étendu')
    if not 40<=m5['rsi14']<=76:reasons.append('RSI hors plage 40–76')
    if last['close']<=last['open']:reasons.append('Bougie M5 non haussière')
    if h1['last']['close']<h1['ema21']:reasons.append('H1 sous EMA21 : achat non autorisé')
    if h1['ema21']<h1['ema21_previous']:reasons.append('EMA21 H1 descendante')
    if h1['ema21']<=h1['ema50']:reasons.append('Tendance H1 non confirmée : EMA21 sous EMA50')
    if not reasons:reasons.append('Cassure, retest, reprise ou retournement confirmé absent ; volume éventuellement insuffisant')
    return reasons


def pullback_reference(m5: dict, h1: dict, *, short: bool = False,
                       require_volume: bool = True, now_ms: int | None = None) -> dict | None:
    """Observe a closed trend-pullback reference; never propose a target or order.

    The contact zone uses EMA21_previous, known before the signal candle.
    Contacts and a real retracement must occur in its three preceding candles;
    the stop reference covers six candles including the signal's own wick.
    SHORT is the price-reflected LONG rule, with the original ATR percentage.
    """
    import time
    try:
        now_ms = int(time.time()*1000) if now_ms is None else now_ms
        if type(now_ms) is not int or now_ms < 0 or type(short) is not bool or type(require_volume) is not bool:
            return None

        def finite(value):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

        def closed_rows(data, minimum):
            if not isinstance(data, dict) or not isinstance(data.get('candles'), list):
                return None
            rows = data['candles']
            if len(rows) < minimum:
                return None
            end = -1
            for bar in rows:
                start, stop = bar['open_time'], bar['close_time']
                op, high, low, close, volume = (bar[key] for key in ('open', 'high', 'low', 'close', 'volume'))
                if (type(start) is not int or type(stop) is not int or
                    not end < start <= stop <= now_ms or
                    not all(finite(v) for v in (op, high, low, close, volume)) or
                    not 0 < low <= min(op, close) <= max(op, close) <= high or volume < 0):
                    return None
                end = stop
            if data.get('last') != rows[-1] or minimum >= 6 and data.get('previous') != rows[-2]:
                return None
            return rows

        rows, hourly = closed_rows(m5, 6), closed_rows(h1, 2)
        if rows is None or hourly is None or hourly[-1]['close_time'] > rows[-1]['close_time']:
            return None
        m5_keys = ('ema9', 'ema21', 'ema21_previous', 'atr14', 'atr_pct',
                   'macd_hist', 'macd_hist_previous', 'rsi14', 'volume_ratio')
        h1_keys = ('ema21', 'ema50', 'ema21_previous')
        if not all(finite(m5[key]) for key in m5_keys) or not all(finite(h1[key]) for key in h1_keys):
            return None
        if min(m5['ema9'], m5['ema21'], m5['ema21_previous'],
               h1['ema21'], h1['ema50'], h1['ema21_previous']) <= 0:
            return None
        original_rows = rows
        reflection = None
        if short:
            def reflect(bar, anchor):
                return dict(bar, open=anchor-bar['open'], close=anchor-bar['close'],
                            high=anchor-bar['low'], low=anchor-bar['high'])
            reflection = 2*max(max(bar['high'] for bar in rows), *(m5[key] for key in m5_keys[:3]))
            hourly_reflection = 2*max(max(bar['high'] for bar in hourly), *(h1[key] for key in h1_keys))
            if not finite(reflection) or not finite(hourly_reflection):
                return None
            rows = [reflect(bar, reflection) for bar in rows]
            hourly = [reflect(bar, hourly_reflection) for bar in hourly]
            m5 = dict(m5, **{key: reflection-m5[key] for key in m5_keys[:3]},
                      macd_hist=-m5['macd_hist'], macd_hist_previous=-m5['macd_hist_previous'],
                      rsi14=100-m5['rsi14'])
            h1 = dict(h1, **{key: hourly_reflection-h1[key] for key in h1_keys})
        last, previous = rows[-1], rows[-2]
        atr = m5['atr14']
        if atr <= 0 or not .03 <= m5['atr_pct'] <= 3 or not 40 <= m5['rsi14'] <= 76:
            return None
        if (last['high']-last['low'] > 2.5*atr or last['close']-m5['ema21'] > 2.5*atr or
            last['close'] <= last['open'] or
            last['close'] <= max(previous['high'], m5['ema9'], m5['ema21']) or
            m5['macd_hist'] <= m5['macd_hist_previous']):
            return None
        if not (hourly[-1]['close'] > h1['ema21'] > h1['ema50'] and h1['ema21'] > h1['ema21_previous']):
            return None
        if not ((not require_volume and not m5.get('volume_available', True)) or m5['volume_ratio'] >= .8):
            return None
        prior = rows[-4:-1]
        contacts = [bar for bar in prior if m5['ema21_previous']-.5*atr <= bar['low'] <= m5['ema21_previous']+.3*atr]
        retraced = any(rows[i]['close'] < rows[i]['open'] or rows[i]['close'] < rows[i-1]['close']
                       for i in range(len(rows)-4, len(rows)-1))
        if not contacts or not retraced:
            return None
        pivot = min(rows[-6:], key=lambda bar: (bar['low'], -bar['close_time']))
        price = (next(bar['high'] for bar in original_rows if bar['close_time'] == pivot['close_time'])
                 if short else pivot['low'])
        if not finite(price) or price <= 0:
            return None
        return dict(price=price, source='reprise clôturée après pullback', timeframe='M5',
                    pivot_at=pivot['close_time'], confirmed_at=last['close_time'],
                    zone_start_at=original_rows[-6]['open_time'], contact_at=contacts[-1]['close_time'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None

