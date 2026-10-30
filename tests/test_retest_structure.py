"""Closed-retest references on synthetic candles, never profitability evidence."""
from copy import deepcopy
import math
import time
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.indicators import candidates, short_candidates, snapshot
from app.market import Candle, Quote, Rules
from app.opportunity import market_feasibility
from app.risk import RiskEngine
from app.structure import StructureRejected, structural_levels, validate_structural_plan


def context(now, *, low=99.45, short=False):
    result = {}
    for name, span in [('m5', 300000), ('h1', 3600000)]:
        end = now // span * span - 1
        bars = [dict(open_time=end-(39-i)*span-span+1, close_time=end-(39-i)*span,
                     open=98.8, high=99.2, low=98.4, close=99., volume=100.)
                for i in range(40)]
        if name == 'm5':
            bars[-10]['high'] = 99.5
            bars[-2].update(open=99.35, high=100., low=99., close=99.8)
            bars[-1].update(open=99.8, high=100.1, low=low, close=99.9)
        else:
            for bar in bars:
                bar.update(high=105., low=96.)
        if short:
            bars = [dict(bar, open=200-bar['open'], close=200-bar['close'],
                         high=200-bar['low'], low=200-bar['high']) for bar in bars]
        result[name] = {'candles': bars, 'last': bars[-1], 'atr14': 1.}
    return result


def retests(levels):
    return [row for key in ('supports', 'resistances') for row in levels[key]
            if row['source'] == 'retest clôturé après cassure']


@pytest.mark.parametrize('short', [False, True])
@pytest.mark.parametrize('low,anchor', [(99., 99.), (99.45, 99.45), (99.6, 99.5), (99.8, 99.5)])
def test_closed_retest_uses_observed_extreme_and_broken_level_conservatively(short, low, anchor):
    now = int(time.time()*1000)
    data = context(now, low=low, short=short)
    levels = structural_levels(data, 100.08 if short else 99.92,
                               'SHORT' if short else 'LONG', now)
    reference = levels['stop_reference']
    assert reference in retests(levels)
    assert reference['price'] == pytest.approx(200-anchor if short else anchor)
    assert reference['broken_level'] == pytest.approx(100.5 if short else 99.5)
    assert reference['observed_extreme'] == pytest.approx(200-low if short else low)
    assert reference['pivot_at'] == reference['confirmed_at'] == data['m5']['last']['close_time']
    assert reference['breakout_at'] == data['m5']['candles'][-2]['close_time']
    assert reference['timeframe'] == 'M5'
    # The previous breakout wick remains the nearest target barrier.
    assert levels['target_obstacle']['price'] == pytest.approx(100.)
    assert levels['stop_buffer'] == .15 and levels['target_buffer'] == .1
    assert retests(structural_levels(data, 99.92 if short else 100.08,
                                    'LONG' if short else 'SHORT', now)) == []


@pytest.mark.parametrize('change', ['wick_only', 'no_breakout', 'failed_retest',
                                  'below_zone', 'above_zone', 'too_few_bars', 'before_retest'])
@pytest.mark.parametrize('short', [False, True])
def test_no_role_reference_without_a_completed_geometric_retest(change, short):
    now = int(time.time()*1000)
    data = context(now)
    bars = data['m5']['candles']
    if change == 'wick_only':
        bars[-2]['close'] = 99.4
    elif change == 'no_breakout':
        bars[-2]['close'] = 99.5
    elif change == 'failed_retest':
        bars[-1].update(open=99.4, close=99.5, low=99.3)
    elif change == 'below_zone':
        bars[-1]['low'] = 98.99
    elif change == 'above_zone':
        bars[-1]['low'] = 99.81
        bars[-1]['open'] = 99.85
    elif change == 'too_few_bars':
        data['m5']['candles'] = bars[-21:]
    else:
        data['m5']['candles'] = bars[:-1]
    if short:
        for frame in data.values():
            frame['candles'] = [dict(bar, open=200-bar['open'], close=200-bar['close'],
                                    high=200-bar['low'], low=200-bar['high'])
                                for bar in frame['candles']]
    assert retests(structural_levels(data, 100.08 if short else 99.92,
                                    'SHORT' if short else 'LONG', now)) == []


def test_future_retest_and_nonchronological_data_are_rejected():
    now = int(time.time()*1000)
    data = context(now)
    with pytest.raises(StructureRejected, match='futures'):
        structural_levels(data, 99.92, now_ms=data['m5']['last']['close_time']-1)
    data['m5']['candles'][-1]['open_time'] = data['m5']['candles'][-2]['close_time']
    with pytest.raises(StructureRejected, match='chronologiques'):
        structural_levels(data, 99.92, now_ms=now)


def test_retest_reference_preserves_stop_buffer_and_first_target_obstacle():
    now = int(time.time()*1000)
    data = context(now)
    valid = validate_structural_plan(data, 99.7, 99.3, 99.9, now_ms=now)
    assert valid['validated'] and valid['stop_reference'] in retests(valid)
    with pytest.raises(StructureRejected, match='bruit du pivot'):
        validate_structural_plan(data, 99.7, 99.31, 99.9, now_ms=now)
    with pytest.raises(StructureRejected, match='premier obstacle'):
        validate_structural_plan(data, 99.7, 99.3, 100., now_ms=now)


def indicator_context(now, *, short=False, clears_prior_wick=True):
    """A real indicator-admitted retest with a separately observed H1 target."""
    result = {}
    for name, span in [('m5', 300000), ('h1', 3600000)]:
        end = now // span * span - 1
        bars = []
        for i in range(150):
            close = 97.5+i*.01+.2*math.sin(i*1.2) if name == 'm5' else 88+i*.076
            bars.append(Candle(end-(149-i)*span-span+1, end-(149-i)*span,
                               close-.12, close+.35, close-.47, close, 100.))
        if name == 'm5':
            resistance = max(c.high for c in bars[-22:-2])
            previous, last = bars[-2:]
            bars[-2] = Candle(previous.open_time, previous.close_time,
                              resistance-.1, resistance+.45, resistance-.15, resistance+.35, 200.)
            bars[-1] = Candle(last.open_time, last.close_time,
                              resistance+.2 if clears_prior_wick else resistance+.1,
                              resistance+.75 if clears_prior_wick else resistance+.4,
                              resistance+.01, resistance+.6 if clears_prior_wick else resistance+.25, 200.)
        else:
            bar = bars[120]
            bars[120] = Candle(bar.open_time, bar.close_time, bar.open,
                               resistance+3.5, bar.low, bar.close, bar.volume)
        if short:
            bars = [Candle(c.open_time, c.close_time, 200-c.open, 200-c.low,
                           200-c.high, 200-c.close, c.volume) for c in bars]
        result[name] = snapshot(bars)
    return result


@pytest.mark.parametrize('short', [False, True])
def test_observed_retest_flows_through_actual_feasibility_and_risk(short):
    now = int(time.time()*1000)
    data = indicator_context(now, short=short)
    admitted = short_candidates(data['m5'], data['h1']) if short else candidates(data['m5'], data['h1'])
    assert 'retest' in admitted
    symbol = 'CAP:TEST' if short else 'BTCUSDT'
    close = data['m5']['last']['close']
    quote = Quote(symbol, close-.02 if short else close, close if short else close+.02, now,
                  currency='USD' if short else 'USDT')
    rule = Rules(symbol, .001, 1e6, .001, 5., 1e9, .01,
                 currency=quote.currency, min_margin_pct=20., allow_short=short,
                 financing_long_daily=0., financing_short_daily=0.)
    cfg = Settings(cfd_enabled=short, cfd_fee_rate=.001).model_copy(update={'symbols': [symbol]})
    before = deepcopy(data)
    bound = market_feasibility(data, quote, rule, cfg, short=short)
    assert bound['status'] == 'feasible'
    decision = SimpleNamespace(symbol=symbol, action='SHORT' if short else 'BUY',
        entry=quote.bid if short else quote.ask, stop_loss=bound['reference_stop'],
        take_profit=bound['reference_target'], invalidation=bound['reference_stop'],
        setup='retest', confirmation_met=True, risk_pct=None)
    plan = RiskEngine.plan(decision, quote, rule, cfg, cash=900., equity=900.,
                           atr=data['m5']['atr14'], structure_context=data, now_ms=now)
    assert plan.rr == pytest.approx(bound['max_net_rr']) and plan.rr >= 2
    assert plan.risk_usdt <= 9. and plan.structure_audit['stop_reference'] in retests(plan.structure_audit)
    assert data == before
    if not short:
        assert plan.stop_loss == 99.34 and plan.take_profit == 102.88
        assert plan.rr == pytest.approx(2.3836679315)


def test_retest_under_prior_breakout_wick_still_fails_two_r_without_moving_target():
    now = int(time.time()*1000)
    data = indicator_context(now, clears_prior_wick=False)
    assert 'retest' in candidates(data['m5'], data['h1'])
    close = data['m5']['last']['close']
    quote = Quote('BTCUSDT', close, close+.02, now)
    rule = Rules('BTCUSDT', .001, 1e6, .001, 5., 1e9, .01)
    bound = market_feasibility(data, quote, rule, Settings())
    assert bound['status'] == 'insufficient_rr' and bound['max_net_rr'] < 0
    assert bound['reference_target'] == 99.83
