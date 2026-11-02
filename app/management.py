"""Versioned, local paper-position exits. These rules do not establish an edge."""
from __future__ import annotations

from datetime import datetime
import math

from .ai import Decision

PROFILE = 'expanded_v2'
POLICY = 'closed_h1_invalidation_v2'
EXIT_REASON = 'H1_INVALIDATION_V2'
HOUR_MS = 3_600_000


def uses_v2_policy(position: dict | None) -> bool:
    """The policy belongs to the opening cohort, never to today's settings."""
    try:
        return position['detail']['context']['rules']['strategy_profile'] == PROFILE
    except (KeyError, TypeError):
        return False


def _number(value, *, positive=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if positive and value <= 0:
        return None
    return value


def _reference(value):
    if not isinstance(value, dict) or _number(value.get('price')) is None:
        return None
    result = {'price': value['price']}
    for name in ('timeframe', 'source'):
        if isinstance(value.get(name), str):
            result[name] = value[name][:180]
    for name in ('pivot_at', 'confirmed_at', 'zone_start_at', 'contact_at'):
        timestamp = value.get(name)
        if isinstance(timestamp, int) and not isinstance(timestamp, bool) and timestamp >= 0:
            result[name] = timestamp
    return result


def opening_plan(position: dict) -> dict:
    """An explicit whitelist; never copy account data, raw prompts or full detail."""
    detail = position.get('detail') if isinstance(position.get('detail'), dict) else {}
    proposal = detail.get('proposal') if isinstance(detail.get('proposal'), dict) else {}
    plan = detail.get('plan') if isinstance(detail.get('plan'), dict) else {}
    structure = plan.get('structure_audit') if isinstance(plan.get('structure_audit'), dict) else {}
    direction = position.get('direction', 'LONG')
    setup = proposal.get('setup')
    result = {'strategy_profile': PROFILE if uses_v2_policy(position) else None,
              'direction': direction if isinstance(direction,str) and direction in {'LONG','SHORT'} else None,
              'setup': setup if isinstance(setup,str) and setup in
                  {'breakout', 'retest', 'consolidation', 'reversal', 'pullback'} else None,
              'opened_at': position['opened_at'][:80] if isinstance(position.get('opened_at'),str) else None,
              'entry': _number(position.get('entry')),
              'stop_loss': _number(position.get('stop_loss')),
              'take_profit': _number(position.get('take_profit')),
              'invalidation': _number(proposal.get('invalidation')),
              'stop_reference': _reference(structure.get('stop_reference')),
              'target_obstacle': _reference(structure.get('target_obstacle'))}
    return result


def _opened_ms(position):
    opened = datetime.fromisoformat(position['opened_at'].replace('Z', '+00:00'))
    if opened.tzinfo is None or opened.utcoffset() is None:
        raise ValueError('Timezone required')
    value = opened.timestamp() * 1000
    if not math.isfinite(value) or value < 0:
        raise ValueError('Invalid opening timestamp')
    return value


def _closed_h1(market, now_ms):
    """Validate the trusted indicator snapshot and every bar it refers to."""
    if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms <= 0:
        raise ValueError('Invalid current time')
    h1 = market['h1']
    rows = h1['candles']
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError('Missing closed H1 bars')
    previous_close = None
    for row in rows:
        start, end = row['open_time'], row['close_time']
        if any(not isinstance(v, int) or isinstance(v, bool) for v in (start, end)):
            raise ValueError('Invalid bar timestamps')
        if (start < 0 or start % HOUR_MS or end != start + HOUR_MS - 1 or
                end >= now_ms or previous_close is not None and start <= previous_close):
            raise ValueError('Unclosed or incoherent H1 history')
        values = [row[name] for name in ('open', 'high', 'low', 'close')]
        if any(_number(value) is None for value in values):
            raise ValueError('Non-finite H1 data')
        op, high, low, close = values
        if not low <= min(op, close) <= max(op, close) <= high:
            raise ValueError('Incoherent OHLC')
        previous_close = end
    if rows[-1] != h1['last'] or rows[-2] != h1['previous']:
        raise ValueError('Contradictory H1 references')
    if rows[-1]['close_time'] != now_ms // HOUR_MS * HOUR_MS - 1:
        raise ValueError('Stale H1 data')
    for key in ('ema21', 'ema50', 'ema21_previous'):
        if _number(h1[key]) is None:
            raise ValueError('Invalid EMA')
    return h1


def management_decision(position: dict, market: dict, *, now_ms: int):
    """HOLD unless a fully closed, post-opening H1 has opposite full alignment.

    Existing quote-driven stop/target/margin protection remains independent and
    takes priority. Missing entry candidates or an unrealized loss is no exit.
    """
    audit = {'policy': POLICY, 'exit_reason': None, 'condition_met': False,
             'opening_plan': opening_plan(position)}
    reason = 'Plan conservé : aucune invalidation H1 clôturée de la stratégie v2.'
    try:
        if not uses_v2_policy(position):
            raise ValueError('Different opening cohort')
        direction = position.get('direction', 'LONG')
        if direction not in {'LONG', 'SHORT'} or direction == 'SHORT' and position.get('kind') != 'cfd':
            raise ValueError('Invalid position direction')
        recorded = audit['opening_plan']
        entry, stop, target, invalidation = (recorded[key] for key in ('entry','stop_loss','take_profit','invalidation'))
        if any(value is None for value in (entry, stop, target, invalidation)):
            raise ValueError('Missing opening levels')
        if not (stop <= invalidation < entry < target if direction == 'LONG' else target < entry < invalidation <= stop):
            raise ValueError('Incoherent opening plan')
        h1 = _closed_h1(market, now_ms)
        opened = _opened_ms(position)
        if opened > now_ms:
            raise ValueError('Future opening timestamp')
        last = h1['last']
        audit.update(h1_closed_at=last['close_time'], h1_close=last['close'],
                     ema21=h1['ema21'], ema50=h1['ema50'], ema21_previous=h1['ema21_previous'])
        after_opening = last['close_time'] > opened
        opposite = (last['close'] < h1['ema21'] < h1['ema50'] and h1['ema21'] < h1['ema21_previous']) if direction == 'LONG' else (
            last['close'] > h1['ema21'] > h1['ema50'] and h1['ema21'] > h1['ema21_previous'])
        if after_opening and opposite:
            audit.update(condition_met=True, exit_reason=EXIT_REASON)
            reason = 'Invalidation H1 v2 : clôture après ouverture, alignement complet opposé à la position et pente EMA21 opposée.'
        elif not after_opening:
            reason = 'Plan conservé : aucune clôture H1 nouvelle depuis l’ouverture.'
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        audit['data_valid'] = False
        reason = 'Plan conservé : données H1 ou plan d’ouverture incomplets ; protection SL/TP indépendante maintenue.'
    else:
        audit['data_valid'] = True
    decision = Decision(symbol=position['symbol'], action='SELL' if audit['condition_met'] else 'HOLD',
        reason=reason, entry=None, stop_loss=None, take_profit=None, invalidation=None,
        confirmation='Règle locale sur H1 clôturée ; aucun nouveau niveau ni appel IA.',
        risks=[], setup='none', confirmation_met=False, risk_pct=None)
    return decision, audit
