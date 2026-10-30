"""Synthetic closed-pullback facts, with no order or profitability claims."""
from copy import deepcopy
import time

import pytest

from app.indicators import pullback_reference


def fixture(now, *, short=False):
    context = {}
    for name, span, count in [('m5', 300000, 6), ('h1', 3600000, 2)]:
        end = now//span*span-1
        rows = [dict(open_time=end-(count-1-i)*span-span+1, close_time=end-(count-1-i)*span,
                     open=104.8, high=105.3, low=104.5, close=105.1, volume=100.)
                for i in range(count)]
        if name == 'm5':
            prices = [(100.1,100.6,99.2,100.2), (100.2,100.5,99.4,100.),
                      (100.,100.45,99.7,99.9), (99.9,100.4,99.8,100.),
                      (100.,100.6,99.9,100.2), (100.25,101.,100.1,100.9)]
            for bar, (op, high, low, close) in zip(rows, prices):
                bar.update(open=op, high=high, low=low, close=close)
            state = dict(ema9=100.5, ema21=100.1, ema21_previous=100., atr14=1.,
                         atr_pct=1/100.9*100, macd_hist=-.02, macd_hist_previous=-.03,
                         rsi14=55., volume_ratio=.9, volume_available=True)
        else:
            state = dict(ema21=104., ema50=102., ema21_previous=103.9)
        state.update(candles=rows, last=rows[-1], previous=rows[-2])
        context[name] = state
    if short:
        for name, state in context.items():
            for bar in state['candles']:
                op, high, low, close = (bar[k] for k in ('open','high','low','close'))
                bar.update(open=210-op, high=210-low, low=210-high, close=210-close)
            for key in ('ema9','ema21','ema21_previous','ema50'):
                if key in state:
                    state[key] = 210-state[key]
            if name == 'm5':
                state.update(macd_hist=-state['macd_hist'], macd_hist_previous=-state['macd_hist_previous'],
                             rsi14=100-state['rsi14'])
    return context['m5'], context['h1']


@pytest.mark.parametrize('short', [False, True])
def test_closed_pullback_returns_only_causal_observed_reference_without_mutation(short):
    now = int(time.time()*1000)
    m5, h1 = fixture(now, short=short)
    before = deepcopy((m5, h1))
    result = pullback_reference(m5, h1, short=short, now_ms=now)
    assert result == dict(price=110.8 if short else 99.2, source='reprise clôturée après pullback',
                         timeframe='M5', pivot_at=m5['candles'][0]['close_time'],
                         confirmed_at=m5['last']['close_time'], zone_start_at=m5['candles'][0]['open_time'],
                         contact_at=m5['previous']['close_time'])
    assert result['zone_start_at'] <= result['pivot_at'] <= result['confirmed_at'] <= now
    assert result['contact_at'] < result['confirmed_at']
    assert (m5, h1) == before
    # Improving momentum may still be negative on a LONG recovery.
    assert m5['macd_hist'] > m5['macd_hist_previous'] if not short else m5['macd_hist'] < m5['macd_hist_previous']


@pytest.mark.parametrize('short', [False, True])
def test_signal_wick_is_part_of_stop_zone(short):
    now = int(time.time()*1000)
    m5, h1 = fixture(now, short=short)
    m5['last']['high' if short else 'low'] = 111.1 if short else 98.9
    result = pullback_reference(m5, h1, short=short, now_ms=now)
    assert result['price'] == (111.1 if short else 98.9)
    assert result['pivot_at'] == result['confirmed_at']


def test_old_extreme_outside_six_candles_does_not_move_zone_stop():
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    oldest = dict(m5['candles'][0], low=90.)
    oldest.update(open_time=oldest['open_time']-300000, close_time=oldest['close_time']-300000)
    m5['candles'].insert(0, oldest)
    result = pullback_reference(m5, h1, now_ms=now)
    assert result['price'] == 99.2 and result['zone_start_at'] == m5['candles'][1]['open_time']


@pytest.mark.parametrize('short', [False, True])
def test_recent_contact_without_a_real_retracement_is_not_a_pullback(short):
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    for i, bar in enumerate(m5['candles'][-4:-1]):
        bar.update(open=100.+i*.1, close=100.1+i*.1, high=100.6)
    if short:
        # A reflected monotonic rise is a monotonic fall, also without a pullback.
        for state in (m5, h1):
            for bar in state['candles']:
                op, high, low, close = (bar[k] for k in ('open','high','low','close'))
                bar.update(open=210-op, high=210-low, low=210-high, close=210-close)
            for key in ('ema9','ema21','ema21_previous','ema50'):
                if key in state:state[key]=210-state[key]
        m5.update(macd_hist=.02,macd_hist_previous=.03,rsi14=55.)
    assert pullback_reference(m5, h1, short=short, now_ms=now) is None


def test_contact_older_than_three_preceding_candles_is_rejected():
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    m5['candles'][1]['low'] = 99.8
    for bar in m5['candles'][-4:-1]:
        bar.update(open=100.55, high=100.7, low=100.35, close=100.45)
    assert pullback_reference(m5, h1, now_ms=now) is None


@pytest.mark.parametrize('contact', [99.5, 100.3])
def test_exact_contact_zone_boundaries_are_accepted(contact):
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    for bar in m5['candles'][-4:-1]:
        bar.update(open=100.5, high=100.6, low=contact, close=100.4)
    assert pullback_reference(m5, h1, now_ms=now) is not None


@pytest.mark.parametrize('contact', [99.49, 100.31])
def test_contact_extreme_outside_zone_is_rejected(contact):
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    for bar in m5['candles'][-4:-1]:
        bar.update(open=100.5, high=100.6, low=contact, close=100.4)
    assert pullback_reference(m5, h1, now_ms=now) is None


@pytest.mark.parametrize('failure', ['h1_alignment', 'h1_slope', 'h1_close', 'signal_body',
                                   'previous_high', 'ema_reclaim', 'momentum', 'rsi', 'atr_floor',
                                   'atr_ceiling', 'range', 'extension', 'volume'])
def test_each_existing_quality_guard_remains_required(failure):
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    if failure == 'h1_alignment':h1['ema50'] = 104.1
    elif failure == 'h1_slope':h1['ema21_previous'] = 104.
    elif failure == 'h1_close':h1['last']['close'] = 103.9
    elif failure == 'signal_body':m5['last']['open'] = 100.95
    elif failure == 'previous_high':m5['previous']['high'] = 100.9
    elif failure == 'ema_reclaim':m5['ema21'] = 100.9
    elif failure == 'momentum':m5['macd_hist'] = -.03
    elif failure == 'rsi':m5['rsi14'] = 76.01
    elif failure == 'atr_floor':m5['atr_pct'] = .029
    elif failure == 'atr_ceiling':m5['atr_pct'] = 3.01
    elif failure == 'range':m5['last']['low'] = 98.
    elif failure == 'extension':m5['ema21'] = 98.3
    else:m5['volume_ratio'] = .79
    assert pullback_reference(m5, h1, now_ms=now) is None


def test_missing_cfd_volume_uses_the_same_explicit_convention_as_classic_setups():
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    m5.update(volume_available=False, volume_ratio=0.)
    assert pullback_reference(m5, h1, require_volume=False, now_ms=now) is not None
    assert pullback_reference(m5, h1, require_volume=True, now_ms=now) is None
    m5['volume_available'] = True
    assert pullback_reference(m5, h1, require_volume=False, now_ms=now) is None


@pytest.mark.parametrize('failure', ['future_m5', 'future_h1', 'h1_after_signal', 'chronology',
                                   'bad_ohlc', 'few_m5', 'few_h1', 'last_mismatch', 'previous_mismatch'])
def test_future_inconsistent_or_incomplete_candles_fail_closed(failure):
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    if failure == 'future_m5':m5['last']['close_time'] = now+1
    elif failure == 'future_h1':h1['last']['close_time'] = now+1
    elif failure == 'h1_after_signal':h1['last']['close_time'] = m5['last']['close_time']+1
    elif failure == 'chronology':m5['last']['open_time'] = m5['previous']['close_time']
    elif failure == 'bad_ohlc':m5['last']['low'] = 102.
    elif failure == 'few_m5':m5['candles'] = m5['candles'][1:]
    elif failure == 'few_h1':h1['candles'] = h1['candles'][1:]
    elif failure == 'last_mismatch':m5['last'] = dict(m5['last'],close=100.8)
    else:m5['previous'] = dict(m5['previous'],close=100.1)
    assert pullback_reference(m5, h1, now_ms=now) is None


@pytest.mark.parametrize('key', ['ema9', 'ema21', 'ema21_previous', 'atr14', 'atr_pct',
                               'macd_hist', 'macd_hist_previous', 'rsi14', 'volume_ratio'])
@pytest.mark.parametrize('value', [float('nan'), float('inf')])
def test_nonfinite_indicators_fail_closed(key, value):
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    m5[key] = value
    assert pullback_reference(m5, h1, now_ms=now) is None


@pytest.mark.parametrize('value', [float('nan'), float('inf'), None, '100', True])
def test_bad_candle_prices_and_bad_hourly_indicators_fail_closed(value):
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    m5['last']['low'] = value
    assert pullback_reference(m5, h1, now_ms=now) is None
    m5, h1 = fixture(now)
    h1['ema21'] = value
    assert pullback_reference(m5, h1, now_ms=now) is None


def test_missing_data_and_invalid_clock_fail_closed():
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    assert pullback_reference({}, h1, now_ms=now) is None
    assert pullback_reference(m5, None, now_ms=now) is None
    assert pullback_reference(m5, h1, now_ms=True) is None
    assert pullback_reference(m5, h1, now_ms=-1) is None
    assert pullback_reference(m5, h1, now_ms=float('nan')) is None
