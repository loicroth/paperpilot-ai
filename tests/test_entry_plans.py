"""Catalog selection tests use synthetic levels, never a trading record."""
from copy import deepcopy
from types import SimpleNamespace
import math
import pytest
from app.entry_plans import build_entry_plans


def config(**changes):
    values = dict(min_rr=2., cfd_enabled=True, allow_short=True)
    values.update(changes)
    return SimpleNamespace(**values)


def candidate(short=False):
    action, symbol = ('SHORT', 'CAP:NVDA') if short else ('BUY', 'BTCUSDT')
    stop, target, pivot = (102., 94., 101.8) if short else (98., 108., 98.2)
    data = dict(quote=dict(symbol=symbol, bid=100., ask=100.02, time_ms=1),
        directions=[action], direction_candidates={action:['breakout']},
        structure={action:{'stop_reference':{'price':pivot}}},
        feasibility={action:dict(status='feasible', max_net_rr=3., reference_stop=stop,
            reference_target=target, costs={'entry_fill':99.95 if short else 100.07001})})
    return symbol, data, ['breakout']


@pytest.mark.parametrize('short',[False,True])
def test_catalog_preserves_market_levels_and_raw_quote_entry(short):
    row = candidate(short)
    before = deepcopy(row)
    plans = build_entry_plans([row],config())
    assert row == before and len(plans) == 1
    plan = plans[0]
    side = 'SHORT' if short else 'BUY'
    assert plan['id'] == f'{row[0]}:{side}:breakout'
    assert plan['entry'] == row[1]['quote']['bid' if short else 'ask']
    assert plan['stop_loss'] == row[1]['feasibility'][side]['reference_stop']
    assert plan['take_profit'] == row[1]['feasibility'][side]['reference_target']
    assert plan['invalidation'] == row[1]['structure'][side]['stop_reference']['price']


@pytest.mark.parametrize('pivot',[100.01,100.02,101.,97.,None,float('nan')])
def test_buy_uses_unchanged_stop_when_pivot_is_crossed_or_incoherent(pivot):
    row = candidate()
    row[1]['structure']['BUY']['stop_reference']['price'] = pivot
    plan = build_entry_plans([row],config())[0]
    assert plan['invalidation'] == plan['stop_loss'] == 98.


@pytest.mark.parametrize('pivot',[100.,100.01,99.,103.,None,float('nan')])
def test_short_uses_unchanged_stop_when_pivot_is_crossed_or_incoherent(pivot):
    row = candidate(True)
    row[1]['structure']['SHORT']['stop_reference']['price'] = pivot
    plan = build_entry_plans([row],config())[0]
    assert plan['invalidation'] == plan['stop_loss'] == 102.


@pytest.mark.parametrize('short,stop',[(False,100.),(False,100.01),(True,100.02),(True,100.01)])
def test_crossed_stop_cannot_be_repaired_by_an_invalidation_inside_spread(short,stop):
    row = candidate(short)
    side = 'SHORT' if short else 'BUY'
    row[1]['feasibility'][side]['reference_stop'] = stop
    row[1]['structure'][side]['stop_reference']['price'] = 100.01
    assert build_entry_plans([row],config()) == []


@pytest.mark.parametrize('status',['unknown','missing_target','invalid_levels','insufficient_rr',None])
def test_infeasible_or_unknown_market_has_no_catalog_plan(status):
    row = candidate()
    row[1]['feasibility']['BUY']['status'] = status
    assert build_entry_plans([row],config()) == []


@pytest.mark.parametrize('rr',[1.99,0.,-1.,math.nan,math.inf,'3',True])
def test_contradictory_sub_two_r_or_invalid_bound_is_not_used(rr):
    row = candidate()
    row[1]['feasibility']['BUY']['max_net_rr'] = rr
    assert build_entry_plans([row],config()) == []


@pytest.mark.parametrize('section,key,value',[
    ('quote','bid',math.nan), ('quote','ask',99.), ('quote','ask',None),
    ('bound','reference_stop',None), ('bound','reference_stop',-1.),
    ('bound','reference_target',math.inf), ('bound','reference_target',100.),
    ('costs','entry_fill',None), ('costs','entry_fill',math.nan),
])
def test_incomplete_or_invalid_prices_do_not_make_a_plan(section,key,value):
    row = candidate()
    target = row[1]['quote'] if section == 'quote' else row[1]['feasibility']['BUY']
    if section == 'costs':
        target = target['costs']
    target[key] = value
    assert build_entry_plans([row],config()) == []


def test_setup_must_be_admitted_for_that_direction_and_have_stable_id():
    row = candidate()
    row[1]['direction_candidates']['BUY'] = ['breakout','retest','none']
    rows = [(row[0],row[1],['retest','breakout','reversal'])]
    first = build_entry_plans(rows,config())
    assert [p['setup'] for p in first] == ['breakout','retest']
    assert len(build_entry_plans(rows+rows,config())) == 2
    changed = deepcopy(rows)
    changed[0][1]['quote']['ask'] += .001
    assert [p['id'] for p in build_entry_plans(changed,config())] == [p['id'] for p in first]


def test_short_spot_disabled_cfd_and_disabled_short_have_no_plan():
    row = candidate(True)
    assert build_entry_plans([row],config(allow_short=False)) == []
    assert build_entry_plans([row],config(cfd_enabled=False)) == []
    spot = ('BTCUSDT',deepcopy(row[1]),row[2])
    spot[1]['quote']['symbol'] = 'BTCUSDT'
    assert build_entry_plans([spot],config()) == []


def test_malformed_result_or_absent_direction_metadata_is_skipped():
    row = candidate()
    row[1]['direction_candidates'] = {}
    assert build_entry_plans([None,{},('BTCUSDT',),row],config()) == []
    assert build_entry_plans(None,config()) == []


@pytest.mark.parametrize('directions',[None,'BUY',42,{}])
def test_malformed_direction_list_is_skipped_without_raising(directions):
    row = candidate()
    row[1]['directions'] = directions
    assert build_entry_plans([row],config()) == []
