"""Read-only geometry/cost diagnostics on synthetic, closed candles."""
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
import json
import math
import time
import pytest
from app.config import Settings
from app.market import Quote, Rules
from app.opportunity import market_feasibility
from app.risk import RiskEngine, RiskRejected
from test_structural_risk import structural_fixture


def fixture(*, cfd=False, short=False, tick=.01, target=None, financing=None):
    symbol = 'CAP:NVDA' if cfd else 'BTCUSDT'
    decision = SimpleNamespace(symbol=symbol, action='SHORT' if short else 'BUY',
        entry=100. if short else 100.02, stop_loss=102. if short else 98.,
        take_profit=target if target is not None else 93. if short else 108.,
        invalidation=102. if short else 98., setup='breakout', confirmation_met=True, risk_pct=None)
    quote = Quote(symbol, 100., 100.02, int(time.time()*1000), currency='USD' if cfd else 'USDT',
                  conversion_bid=.998 if cfd else None, conversion_ask=1.002 if cfd else None)
    rule = Rules(symbol, .001, 1e6, .001, 0., 1e9, tick, currency='USD' if cfd else 'USDT',
                 min_margin_pct=20., allow_short=cfd, financing_long_daily=financing,
                 financing_short_daily=financing, financing_source='provider_fixture' if financing is not None else None)
    cfg = Settings(cfd_enabled=cfd).model_copy(update={'symbols':[symbol]})
    data = structural_fixture(decision, 1., quote.time_ms)
    return data, quote, rule, cfg


def plan_at_bound(data, quote, rule, cfg, diagnostic, short=False):
    decision = SimpleNamespace(symbol=quote.symbol, action='SHORT' if short else 'BUY',
        entry=quote.bid if short else quote.ask, stop_loss=diagnostic['reference_stop'],
        take_profit=diagnostic['reference_target'], invalidation=diagnostic['reference_stop'],
        setup='breakout', confirmation_met=True, risk_pct=None)
    return RiskEngine.plan(decision, quote, rule, cfg, 900., 900.,
                           atr=data['m5']['atr14'], structure_context=data)


@pytest.mark.parametrize('tick',[.001,.01,.05])
def test_spot_bound_matches_risk_engine_exact_levels_and_costs(tick):
    data, quote, rule, cfg = fixture(tick=tick)
    result = market_feasibility(data, quote, rule, cfg)
    assert result['status'] == 'feasible'
    plan = plan_at_bound(data, quote, rule, cfg, result)
    assert result['max_net_rr'] == pytest.approx(plan.rr, rel=1e-12)
    assert plan.stop_loss == result['reference_stop'] and plan.take_profit == result['reference_target']
    assert result['costs']['fee_rate'] == .001
    assert result['costs']['financing_reserve_days'] == 0
    assert result['quote_time_ms'] == quote.time_ms


@pytest.mark.parametrize('short',[False,True])
@pytest.mark.parametrize('financing',[None,0.,.0002])
@pytest.mark.parametrize('fee',[0.,.0004])
def test_cfd_bound_matches_directional_financing_fx_and_risk_engine(short,financing,fee):
    data, quote, rule, cfg = fixture(cfd=True,short=short,financing=financing)
    cfg = cfg.model_copy(update={'cfd_fee_rate':fee,'fee_rate':.009,'cfd_financing_daily':.001})
    result = market_feasibility(data,quote,rule,cfg,short=short)
    assert result['status'] == 'feasible'
    plan = plan_at_bound(data,quote,rule,cfg,result,short)
    assert result['max_net_rr'] == pytest.approx(plan.rr,rel=1e-12)
    assert result['costs']['fee_rate'] == fee == plan.fee_rate
    assert result['costs']['financing_daily'] == plan.financing_daily
    assert result['costs']['financing_source'] == plan.financing_source
    assert result['costs']['financing_reserve_days'] == 1
    assert result['costs']['financing_reserve_per_unit'] == pytest.approx(plan.entry*plan.financing_daily*1.002)


def test_directional_financing_is_not_replaced_by_opposite_side_rate_or_spot_fee():
    data, quote, rule, cfg = fixture(cfd=True)
    rule = replace(rule,financing_long_daily=None,financing_short_daily=.004,financing_daily=.002)
    result = market_feasibility(data,quote,rule,cfg)
    assert result['costs']['financing_daily'] == .002
    assert result['costs']['financing_source'] == 'configured_daily_estimate'
    assert result['costs']['fee_rate'] == 0.


def test_insufficient_rr_is_reported_without_moving_target_past_obstacle():
    data, quote, rule, cfg = fixture(target=101.)
    before = deepcopy(data)
    result = market_feasibility(data,quote,rule,cfg)
    assert result['status'] == 'insufficient_rr' and result['max_net_rr'] < 2
    assert result['reference_target'] <= 101.2-.1+1e-10
    assert data == before
    with pytest.raises(RiskRejected,match='Ratio net'):
        plan_at_bound(data,quote,rule,cfg,result)


def test_missing_obstacle_does_not_manufacture_a_two_r_target():
    data, quote, rule, cfg = fixture()
    for tf in data.values():
        for bar in tf['candles']:
            bar['high'] = max(bar['open'],bar['close'])
    result = market_feasibility(data,quote,rule,cfg)
    assert result['status'] == 'missing_target'
    assert result['reference_target'] is None and result['max_net_rr'] is None
    assert 'obstacle' in result['reason']


def test_missing_support_is_distinct_from_missing_target():
    data, quote, rule, cfg = fixture()
    for tf in data.values():
        for bar in tf['candles']:
            bar.update(open=101.,close=101.,low=101.)
    result = market_feasibility(data,quote,rule,cfg)
    assert result['status'] == 'missing_stop'
    assert result['reference_stop'] is None and result['max_net_rr'] is None


@pytest.mark.parametrize('tick,target',[(5.,108.),(.01,99.92)])
def test_precision_or_obstacle_room_can_prevent_valid_levels(tick,target):
    data, quote, rule, cfg = fixture(tick=tick,target=target)
    result = market_feasibility(data,quote,rule,cfg)
    assert result['status'] == 'invalid_levels' and result['max_net_rr'] is None


@pytest.mark.parametrize('change',[
    {'bid':float('nan')}, {'ask':99.}, {'time_ms':0},
    {'time_ms':int(time.time()*1000)+60000}, {'symbol':'ETHUSDT'},
])
def test_unverifiable_quotes_return_unknown_json_safe_diagnostics(change):
    data, quote, rule, cfg = fixture()
    result = market_feasibility(data,replace(quote,**change),rule,cfg)
    assert result['status'] == 'unknown' and result['max_net_rr'] is None
    json.dumps(result,allow_nan=False)


def test_invalid_or_future_structure_returns_unknown_without_raising():
    data, quote, rule, cfg = fixture()
    data['h1']['candles'][-1]['close_time'] = int(time.time()*1000)+60000
    assert market_feasibility(data,quote,rule,cfg)['status'] == 'unknown'
    assert market_feasibility({},quote,rule,cfg)['status'] == 'unknown'


@pytest.mark.parametrize('rate',[math.nan,math.inf,-.001,.051])
def test_invalid_provider_financing_is_unknown_instead_of_assumed_free(rate):
    data, quote, rule, cfg = fixture(cfd=True,financing=rate)
    result = market_feasibility(data,quote,rule,cfg)
    assert result['status'] == 'unknown' and result['max_net_rr'] is None
    json.dumps(result,allow_nan=False)


def test_diagnostic_is_read_only_and_is_not_order_admission():
    data, quote, rule, cfg = fixture()
    before = deepcopy((data,asdict(quote),asdict(rule),cfg.model_dump()))
    result = market_feasibility(data,quote,rule,cfg)
    assert before == (data,asdict(quote),asdict(rule),cfg.model_dump())
    assert result['status'] == 'feasible'
    assert 'aucun ordre' in result['reason'] and 'aucun gain garanti' in result['reason']
