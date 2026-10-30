"""Synthetic pullback selection, exact zone risk and SQLite paper execution."""
from copy import deepcopy
from dataclasses import asdict, replace
import time

import pytest

from app.ai import Decision, wait
from app.config import Settings
from app.entry_plans import build_entry_plans
from app.execution import PaperExecutionEngine
from app.indicators import pullback_reference
from app.market import Quote, Rules
from app.opportunity import market_feasibility
from app.risk import RiskEngine, RiskRejected
from app.storage import Store
from app.structure import structural_levels, StructureRejected
from test_pullback_strategy import fixture


def market_case(*, short=False, close_obstacle=False):
    now = int(time.time()*1000)
    m5, h1 = fixture(now)
    first = m5['candles'][0]
    old = []
    for i, low in enumerate([99.9, 99.8, 99.6, 99.8, 99.9, 99.8]):
        end = first['open_time']-1-(5-i)*300000
        old.append(dict(open_time=end-300000+1, close_time=end,
                        open=100., high=100.8, low=low, close=100., volume=100.))
    if close_obstacle:
        old[2]['high'] = 101.2
    m5['candles'] = old+m5['candles']
    end = h1['last']['close_time']
    h1['candles'] = [dict(open_time=end-(6-i)*3600000-3600000+1,
                          close_time=end-(6-i)*3600000,
                          open=104.8, high=108., low=104.5, close=105.1, volume=100.)
                     for i in range(7)]
    h1.update(last=h1['candles'][-1], previous=h1['candles'][-2])
    if short:
        for state in (m5, h1):
            for bar in state['candles']:
                op, high, low, close = (bar[k] for k in ('open','high','low','close'))
                bar.update(open=210-op, high=210-low, low=210-high, close=210-close)
            for key in ('ema9','ema21','ema21_previous','ema50'):
                if key in state:state[key]=210-state[key]
        m5.update(macd_hist=-m5['macd_hist'],macd_hist_previous=-m5['macd_hist_previous'],
                  rsi14=100-m5['rsi14'])
    symbol, side = 'CAP:TEST', 'SHORT' if short else 'BUY'
    close = m5['last']['close']
    quote = Quote(symbol, close-.02 if short else close, close if short else close+.02,
                  now, currency='USD', conversion_bid=1., conversion_ask=1.)
    rules = Rules(symbol, .001, 1e6, .001, 0., 1e9, .01, asset_class='stock_cfd',
                  provider='capital', currency='USD', min_margin_pct=20., allow_short=True,
                  financing_long_daily=0., financing_short_daily=0., financing_source='provider_fixture')
    cfg = Settings(strategy_profile='expanded_v1', cfd_enabled=True, cfd_fee_rate=.001).model_copy(
        update={'symbols': [symbol]})
    data = dict(m5=m5, h1=h1, quote=asdict(quote), directions=[side],
                direction_candidates={side: ['pullback']}, instrument={'asset_class': 'stock_cfd'})
    bound = market_feasibility(data, quote, rules, cfg, short=short, setup='pullback')
    classic = market_feasibility(data, quote, rules, cfg, short=short)
    data.update(feasibility={side: classic}, setup_feasibility={side: {'pullback': bound}},
                structure={side: structural_levels(data, quote.bid if short else quote.ask,
                                                   'SHORT' if short else 'LONG', now_ms=now)})
    return data, quote, rules, cfg, bound, now


def h1_history_with_obstacle(data, long_price, *, short=False):
    """Add an older, confirmed H1 pivot without changing the signal snapshot."""
    first = data['h1']['candles'][0]
    history = []
    for i in range(5):
        end = first['open_time']-1-(4-i)*3600000
        bar = dict(open_time=end-3600000+1, close_time=end,
                   open=100.3, high=long_price if i == 2 else 100.8,
                   low=100., close=100.5, volume=100.)
        if short:
            op, high, low, close = (bar[k] for k in ('open','high','low','close'))
            bar.update(open=210-op, high=210-low, low=210-high, close=210-close)
        history.append(bar)
    data['h1_structure_candles'] = history+deepcopy(data['h1']['candles'])


def decision_from_catalog(data, quote, cfg):
    catalog = build_entry_plans([(quote.symbol, data, ['pullback'])], cfg)
    assert len(catalog) == 1
    return Decision(**{key: value for key, value in catalog[0].items() if key != 'id'},
                    reason='Reprise clôturée de fixture.', confirmation='Zone observée reprise.',
                    risks=['Fausse reprise'], confirmation_met=True, risk_pct=None)


@pytest.mark.parametrize('short', [False, True])
def test_pullback_zone_survives_catalog_risk_and_paper_sqlite_execution(tmp_path, short):
    data, quote, rules, cfg, bound, now = market_case(short=short)
    side, direction = ('SHORT', 'SHORT') if short else ('BUY', 'LONG')
    reference = pullback_reference(data['m5'], data['h1'], short=short, require_volume=False, now_ms=now)
    levels = structural_levels(data, quote.bid if short else quote.ask, direction,
                               now_ms=now, setup='pullback')
    classic = structural_levels(data, quote.bid if short else quote.ask, direction, now_ms=now)
    assert levels['stop_reference'] == reference
    assert classic['stop_reference']['price'] == pytest.approx(110.4 if short else 99.6)
    assert reference['price'] == pytest.approx(110.8 if short else 99.2)
    assert levels['target_obstacle'] == classic['target_obstacle']
    assert bound['status'] == 'feasible' and bound['stop_reference']['price'] == reference['price']
    catalog = build_entry_plans([(quote.symbol, data, ['pullback'])], cfg)
    assert len(catalog) == 1 and catalog[0]['id'] == f'{quote.symbol}:{side}:pullback'
    chosen = {key: value for key, value in catalog[0].items() if key != 'id'}
    decision = Decision(**chosen, reason='Reprise clôturée de fixture.', confirmation='Zone observée reprise.',
                        risks=['Fausse reprise'], confirmation_met=True, risk_pct=None)
    assert decision.invalidation == reference['price']
    plan = RiskEngine.plan(decision, quote, rules, cfg, cash=900., equity=900.,
                           atr=data['m5']['atr14'], now_ms=now, structure_context=data)
    assert plan.structure_audit['stop_reference'] == reference
    assert plan.rr == pytest.approx(bound['max_net_rr']) and plan.rr >= 2
    assert plan.risk_usdt <= 9. and plan.risk_pct_used <= 1.
    assert plan.stop_loss == bound['reference_stop'] and plan.take_profit == bound['reference_target']
    assert plan.stop_loss >= reference['price']+.15 if short else plan.stop_loss <= reference['price']-.15
    store = Store(tmp_path/'pullback.sqlite3')
    try:
        opened = PaperExecutionEngine(store).buy(plan, quote, f'pullback-fixture:{side}', decision, cfg,
                                               {'plan': asdict(plan), 'proposal': decision.model_dump()})
        assert opened['kind'] == 'cfd' and opened['direction'] == direction
        persisted = store.position(quote.symbol)
        assert persisted['detail']['plan']['structure_audit']['stop_reference'] == reference
        assert persisted['detail']['proposal']['setup'] == 'pullback'
        assert persisted['stop_loss'] == plan.stop_loss and persisted['take_profit'] == plan.take_profit
        assert persisted['quantity'] > 0 and persisted['risk_usdt'] <= 9.
        assert 0 < store.get('cash') < 900. and len(store.positions()) == 1
    finally:
        store.close()


@pytest.mark.parametrize('short', [False, True])
def test_pullback_keeps_nearby_h1_obstacle_and_produces_no_entry_or_cash_change(tmp_path, short):
    data, quote, rules, cfg, _, _ = market_case(short=short)
    h1_history_with_obstacle(data, 101.2, short=short)
    bound = market_feasibility(data, quote, rules, cfg, short=short, setup='pullback')
    data['setup_feasibility']['SHORT' if short else 'BUY']['pullback'] = bound
    assert pullback_reference(data['m5'], data['h1'], short=short, require_volume=False) is not None
    assert bound['status'] == 'insufficient_rr' and bound['max_net_rr'] < 2
    assert bound['reference_target'] == (108.9 if short else 101.1)
    assert bound['target_obstacle']['timeframe'] == 'H1'
    catalog = build_entry_plans([(quote.symbol, data, ['pullback'])], cfg)
    assert catalog == []
    assert wait('Obstacle trop proche pour le ratio net requis.').action == 'WAIT'
    store = Store(tmp_path/'blocked.sqlite3')
    try:
        assert store.positions() == [] and store.get('cash') == 900.
    finally:
        store.close()


@pytest.mark.parametrize('short', [False, True])
def test_m5_minor_obstacle_stays_visible_and_target_uses_first_observed_h1(short):
    data, quote, rules, cfg, bound, now = market_case(short=short, close_obstacle=True)
    side, direction = ('SHORT', 'SHORT') if short else ('BUY', 'LONG')
    reference_price = quote.ask if short else quote.bid
    levels = structural_levels(data, bound['costs']['entry_fill'], direction, now_ms=now,
                               setup='pullback', reference_price=reference_price)
    assert levels['target_obstacle']['timeframe'] == 'H1'
    assert levels['target_obstacle']['price'] == (102. if short else 108.)
    assert levels['minor_obstacles'][0]['price'] == pytest.approx(108.8 if short else 101.2)
    assert all(level['timeframe'] == 'M5' for level in levels['minor_obstacles'])
    assert bound['target_basis'] == 'structure H1 observée'
    assert bound['minor_obstacles'][0]['price'] == pytest.approx(108.8 if short else 101.2)
    assert bound['reference_target'] == (102.1 if short else 107.9)
    assert bound['status'] == 'feasible'
    # Changing the required multiple does not manufacture a different target.
    demanding = market_feasibility(data, quote, rules, cfg.model_copy(update={'min_rr': 20.}),
                                   short=short, setup='pullback')
    assert demanding['reference_target'] == bound['reference_target']
    assert demanding['status'] == 'insufficient_rr'
    decision = decision_from_catalog(data, quote, cfg)
    plan = RiskEngine.plan(decision, quote, rules, cfg, cash=900., equity=900.,
                           atr=data['m5']['atr14'], now_ms=now, structure_context=data)
    assert plan.structure_audit['minor_obstacles'][0]['price'] == pytest.approx(108.8 if short else 101.2)
    assert plan.take_profit == bound['reference_target']


@pytest.mark.parametrize('short', [False, True])
@pytest.mark.parametrize('failure', ['future', 'chronology'])
def test_additional_h1_history_is_validated_before_target_selection(short, failure):
    data, quote, rules, cfg, _, now = market_case(short=short)
    data['h1_structure_candles'] = deepcopy(data['h1']['candles'])
    history = data['h1_structure_candles']
    if failure == 'future':history[-1]['close_time'] = now+60000
    else:history[-1]['open_time'] = history[-2]['close_time']
    with pytest.raises(StructureRejected, match='futures ou non chronologiques'):
        structural_levels(data, quote.bid if short else quote.ask, 'SHORT' if short else 'LONG',
                          now_ms=now, setup='pullback', reference_price=quote.ask if short else quote.bid)
    bound = market_feasibility(data, quote, rules, cfg, short=short, setup='pullback')
    assert bound['status'] == 'unknown' and bound['reference_target'] is None


@pytest.mark.parametrize('short', [False, True])
def test_quote_already_at_pullback_zone_does_not_authorize_new_entry(short):
    data, quote, rules, cfg, _, now = market_case(short=short)
    zone = pullback_reference(data['m5'], data['h1'], short=short, require_volume=False, now_ms=now)['price']
    quote = replace(quote, ask=zone, bid=zone-.02) if short else replace(quote, bid=zone, ask=zone+.02)
    data['quote'] = asdict(quote)
    levels = structural_levels(data, quote.bid if short else quote.ask, 'SHORT' if short else 'LONG',
                               now_ms=now, setup='pullback', reference_price=quote.ask if short else quote.bid)
    assert levels['stop_reference'] is None
    bound = market_feasibility(data, quote, rules, cfg, short=short, setup='pullback')
    assert bound['status'] == 'missing_stop'
    data['setup_feasibility']['SHORT' if short else 'BUY']['pullback'] = bound
    assert build_entry_plans([(quote.symbol, data, ['pullback'])], cfg) == []


@pytest.mark.parametrize('short', [False, True])
def test_cost_adjusted_fill_cannot_skip_first_h1_level_still_ahead_of_exit_quote(short):
    data, quote, rules, cfg, _, now = market_case(short=short)
    original = decision_from_catalog(data, quote, cfg)
    level = round(quote.ask-.04 if short else quote.bid+.04, 2)
    h1_history_with_obstacle(data, 210-level if short else level, short=short)
    bound = market_feasibility(data, quote, rules, cfg, short=short, setup='pullback')
    assert bound['target_obstacle']['price'] == pytest.approx(level)
    fill = bound['costs']['entry_fill']
    assert fill < level < quote.ask if short else quote.bid < level < fill
    assert bound['status'] == 'invalid_levels'
    assert bound['reference_target'] > fill if short else bound['reference_target'] < fill
    data['setup_feasibility']['SHORT' if short else 'BUY']['pullback'] = bound
    assert build_entry_plans([(quote.symbol, data, ['pullback'])], cfg) == []
    # An older selection aimed at the further H1 level must fail revalidation.
    with pytest.raises(RiskRejected, match='premier obstacle'):
        RiskEngine.plan(original, quote, rules, cfg, cash=900., equity=900.,
                        atr=data['m5']['atr14'], now_ms=now, structure_context=data)


@pytest.mark.parametrize('short', [False, True])
def test_default_and_explicit_classic_dispatch_keep_the_existing_reference(short):
    data, quote, rules, cfg, _, now = market_case(short=short)
    entry, direction = (quote.bid, 'SHORT') if short else (quote.ask, 'LONG')
    default = structural_levels(data, entry, direction, now_ms=now)
    assert default == structural_levels(data, entry, direction, now_ms=now, setup='breakout')
    assert default['stop_reference']['source'] == 'pivot confirmé (2 bougies de chaque côté)'
    assert market_feasibility(data, quote, rules, cfg, short=short) == market_feasibility(
        data, quote, rules, cfg, short=short, setup='breakout')


def test_pullback_catalog_never_borrows_a_feasible_classic_pivot_bound():
    data, quote, _, cfg, _, _ = market_case()
    assert data['feasibility']['BUY']['status'] == 'feasible'
    data.pop('setup_feasibility')
    assert build_entry_plans([(quote.symbol, data, ['pullback'])], cfg) == []
