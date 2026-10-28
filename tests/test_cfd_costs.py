"""Synthetic CFD cost contracts; no broker session, order, or performance claim."""
from dataclasses import asdict, replace
import math
import time

import pytest

from app.capital import CapitalClient
from app.config import Settings
from app.execution import PaperExecutionEngine, cfd_financing, cfd_valuation
from app.market import Quote, Rules
from app.risk import RiskRejected
from app.storage import Store
from test_capital import details
from test_derivatives import SYMBOL, cfg, proposal, quote
from test_structural_risk import structural_plan, proposal as spot_proposal


PROVIDER_SOURCE = 'capital_api_overnight_daily_estimate'
FALLBACK_SOURCE = 'configured_daily_estimate'


def cost_rule(**changes):
    """Use the production Rules dataclass, including independently optional rates."""
    fields = dict(symbol=SYMBOL, min_qty=.001, max_qty=100000., step_size=.001,
        min_notional=0., max_notional=1e9, tick_size=.01, currency='USD', multiplier=1.,
        allow_short=True, min_margin_pct=20., financing_daily=0., maintenance_margin_pct=50.,
        financing_long_daily=None, financing_short_daily=None, financing_source=None)
    fields.update(changes)
    return Rules(**fields)


def cost_plan(*, short=False, market_rule=None, config=None, target=None):
    decision = proposal(short)
    if target is not None:
        decision.take_profit = target
    observed = quote()
    settings = config or cfg(cfd_fee_rate=0.)
    plan = structural_plan(decision, observed, market_rule or cost_rule(), settings,
        900., 900., atr=1.)
    return plan, decision, observed, settings


def test_cfd_commission_setting_has_independent_safe_default_and_bounds():
    settings = Settings()
    assert settings.cfd_fee_rate == 0.
    assert settings.fee_rate == .001
    legacy = settings.model_dump()
    legacy.pop('cfd_fee_rate')
    assert Settings.model_validate(legacy).cfd_fee_rate == 0.
    assert Settings(cfd_fee_rate=.01).cfd_fee_rate == .01
    for value in (-.00001, .010001, math.nan, math.inf):
        with pytest.raises(ValueError):
            Settings(cfd_fee_rate=value)


@pytest.mark.asyncio
@pytest.mark.parametrize('long_rate,short_rate,expected_long,expected_short', [
    (-.0199715, .0117515, .000199715, 0.),
    (-.02, -.08, .0002, .0008),
    (0., 0., 0., 0.),
])
async def test_provider_overnight_percentages_convert_debits_and_ignore_credits(
        monkeypatch, long_rate, short_rate, expected_long, expected_short):
    document = details()
    document['instrument']['overnightFee'] = dict(longRate=long_rate, shortRate=short_rate,
        swapChargeTimestamp=int(time.time()*1000), swapChargeInterval=1440)
    client = CapitalClient()
    async def detail(_):
        return document
    monkeypatch.setattr(client, '_detail', detail)
    try:
        market_rule = (await client.rules(['CAP:SILVER']))['CAP:SILVER']
        assert market_rule.financing_long_daily == pytest.approx(expected_long)
        assert market_rule.financing_short_daily == pytest.approx(expected_short)
        assert market_rule.financing_source == PROVIDER_SOURCE
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    None,
    {'swapChargeInterval': 60},
    {'swapChargeInterval': None},
    {'swapChargeTimestamp': None},
    {'swapChargeTimestamp': -1},
    {'swapChargeTimestamp': 99999999999999},
])
async def test_unverified_overnight_schedule_keeps_contract_but_uses_no_provider_rates(monkeypatch, change):
    document = details()
    if change is not None:
        document['instrument']['overnightFee'] = dict(longRate=-.02, shortRate=-.08,
            swapChargeTimestamp=int(time.time()*1000), swapChargeInterval=1440)
        document['instrument']['overnightFee'].update(change)
    client = CapitalClient()
    async def detail(_):
        return document
    monkeypatch.setattr(client, '_detail', detail)
    try:
        market_rule = (await client.rules(['CAP:SILVER']))['CAP:SILVER']
        assert market_rule.currency == 'USD' and market_rule.multiplier == 1.
        assert market_rule.financing_long_daily is None
        assert market_rule.financing_short_daily is None
        assert market_rule.financing_source is None
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_rate', [None, 'unknown', math.nan, math.inf])
async def test_unverified_one_direction_does_not_discard_other_direction(monkeypatch, bad_rate):
    document = details()
    document['instrument']['overnightFee'] = dict(longRate=bad_rate, shortRate=-.08,
        swapChargeTimestamp=int(time.time()*1000), swapChargeInterval=1440)
    client = CapitalClient()
    async def detail(_):
        return document
    monkeypatch.setattr(client, '_detail', detail)
    try:
        market_rule = (await client.rules(['CAP:SILVER']))['CAP:SILVER']
        assert market_rule.financing_long_daily is None
        assert market_rule.financing_short_daily == pytest.approx(.0008)
        assert market_rule.financing_source == PROVIDER_SOURCE
    finally:
        await client.close()


@pytest.mark.parametrize('short,expected', [(False, .0002), (True, .0008)])
def test_plan_uses_verified_direction_rate_instead_of_configured_fallback(short, expected):
    market_rule = cost_rule(financing_long_daily=.0002, financing_short_daily=.0008,
        financing_source=PROVIDER_SOURCE)
    plan, _, _, _ = cost_plan(short=short, market_rule=market_rule,
        config=cfg(cfd_fee_rate=0., cfd_financing_daily=.001))
    assert plan.financing_daily == expected
    assert plan.financing_source == PROVIDER_SOURCE


@pytest.mark.parametrize('short', [False, True])
def test_verified_zero_financing_is_not_replaced_by_conservative_fallback(short):
    market_rule = cost_rule(financing_long_daily=0., financing_short_daily=0.,
        financing_source=PROVIDER_SOURCE)
    plan, _, _, _ = cost_plan(short=short, market_rule=market_rule)
    assert plan.financing_daily == 0.
    assert plan.financing_source == PROVIDER_SOURCE


@pytest.mark.parametrize('provider_fallback,expected', [(0., .001), (.002, .002)])
def test_missing_direction_uses_maximum_of_configured_and_legacy_fallback(provider_fallback, expected):
    market_rule = cost_rule(financing_long_daily=None, financing_short_daily=.0002,
        financing_daily=provider_fallback, financing_source=PROVIDER_SOURCE)
    plan, _, _, _ = cost_plan(market_rule=market_rule)
    assert plan.financing_daily == expected
    assert plan.financing_source == FALLBACK_SOURCE


@pytest.mark.parametrize('short', [False, True])
def test_plan_rr_and_quantity_reserve_one_day_directional_financing_and_cfd_fee(short):
    market_rule = cost_rule(financing_long_daily=.0002, financing_short_daily=.0008,
        financing_source=PROVIDER_SOURCE)
    plan, _, observed, settings = cost_plan(short=short, market_rule=market_rule,
        config=cfg(cfd_fee_rate=.0004, fee_rate=.009))
    sign = -1 if short else 1
    slip = settings.slippage_bps/10000
    stop_fill = plan.stop_loss*(1+slip if short else 1-slip)
    target_fill = plan.take_profit*(1+slip if short else 1-slip)
    entry_fee = plan.entry*.0004*observed.conversion_ask
    reserve = plan.entry*plan.financing_daily*observed.conversion_ask
    loss = sign*(plan.entry-stop_fill)*observed.conversion_ask + entry_fee + stop_fill*.0004*observed.conversion_ask + reserve
    reward = sign*(target_fill-plan.entry)*observed.conversion_bid - entry_fee - target_fill*.0004*observed.conversion_ask - reserve
    assert plan.fee_rate == .0004
    assert plan.entry_fee == pytest.approx(plan.quantity*entry_fee)
    assert plan.risk_usdt == pytest.approx(plan.quantity*loss)
    assert plan.rr == pytest.approx(reward/loss)
    assert plan.risk_usdt <= 9.+1e-8 and plan.rr >= 2.


def test_zero_cfd_fee_removes_spot_fee_gate_without_removing_financing_or_net_rr_gate():
    free, _, _, _ = cost_plan(target=105., config=cfg(cfd_fee_rate=0., fee_rate=.001))
    changed_spot, _, _, _ = cost_plan(target=105., config=cfg(cfd_fee_rate=0., fee_rate=.01))
    assert free.entry_fee == 0. and free.fee_rate == 0.
    assert free.financing_daily == .001 and free.rr >= 2.
    assert free.quantity == changed_spot.quantity and free.rr == changed_spot.rr
    with pytest.raises(RiskRejected, match='Ratio CFD net insuffisant'):
        cost_plan(target=105., config=cfg(cfd_fee_rate=.001))
    with pytest.raises(RiskRejected, match='Ratio CFD net insuffisant'):
        cost_plan(target=104., config=cfg(cfd_fee_rate=0.))


def test_spot_plan_keeps_its_own_fee_when_cfd_commission_changes():
    decision = spot_proposal()
    observed = Quote(decision.symbol, 100., 100.02, int(time.time()*1000))
    market_rule = Rules(decision.symbol, .001, 1e6, .001, 5., 1e9, .01)
    first = structural_plan(decision, observed, market_rule, Settings(cfd_fee_rate=0.), 900., 900., atr=1.)
    second = structural_plan(decision, observed, market_rule, Settings(cfd_fee_rate=.01), 900., 900., atr=1.)
    assert first.entry_fee == pytest.approx(first.entry*first.quantity*.001)
    assert first.entry_fee == second.entry_fee
    assert first.quantity == second.quantity and first.rr == second.rr


@pytest.mark.parametrize('short', [False, True])
def test_execution_freezes_explicit_plan_fee_and_directional_financing_across_restart(tmp_path, monkeypatch, short):
    clock = {'seconds': time.time()}
    monkeypatch.setattr('app.execution.time.time', lambda: clock['seconds'])
    market_rule = cost_rule(financing_long_daily=.0002, financing_short_daily=.0008,
        financing_source=PROVIDER_SOURCE)
    plan, decision, observed, settings = cost_plan(short=short, market_rule=market_rule,
        config=cfg(cfd_fee_rate=.0004))
    path = tmp_path/'costs.sqlite3'
    db = Store(path)
    engine = PaperExecutionEngine(db)
    changed_settings = cfg(cfd_fee_rate=.009, fee_rate=.007, cfd_financing_daily=.01)
    opened = engine.buy(plan, observed, 'cost-freeze', decision, changed_settings, {'plan': asdict(plan)})
    assert opened['fee_rate'] == .0004
    assert opened['financing_daily'] == plan.financing_daily
    assert opened['detail']['cfd_costs']['financing_source'] == PROVIDER_SOURCE
    assert opened['detail']['plan']['fee_rate'] == .0004
    db.close()

    clock['seconds'] += 43200
    db = Store(path)
    engine = PaperExecutionEngine(db)
    current = quote(99., 99.02) if short else quote(101., 101.02)
    engine.protect(current)
    first = db.position(SYMBOL)
    expected_half_day = plan.entry*plan.quantity*plan.fx_open*plan.financing_daily*.5
    assert first['financing_accrued'] == pytest.approx(expected_half_day)
    # A second observation at the same instant must not double-charge carry.
    engine.protect(current)
    assert db.position(SYMBOL)['financing_accrued'] == pytest.approx(expected_half_day)
    value = cfd_valuation(db.position(SYMBOL), current)
    assert value['financing_estimate'] is True
    assert value['financing_source'] == PROVIDER_SOURCE
    assert value['financing_method']

    clock['seconds'] += 21600
    current = quote(98.5, 98.52) if short else quote(102., 102.02)
    net = engine.sell(current, 'COST_TEST')
    closed = db.trades()[0]
    assert closed['financing_accrued'] == pytest.approx(expected_half_day*1.5)
    expected_fee = closed['exit']*plan.quantity*.0004*current.conversion_ask
    assert closed['exit_fee'] == pytest.approx(expected_fee)
    assert closed['fees'] == pytest.approx(plan.entry_fee+expected_fee)
    assert closed['detail']['cfd_costs']['financing_source'] == PROVIDER_SOURCE
    assert db.get('cash') == pytest.approx(900.+net)
    db.close()


def test_legacy_plan_without_frozen_fee_uses_old_setting_and_keeps_it_after_restart(tmp_path):
    plan, decision, observed, _ = cost_plan(config=cfg(cfd_fee_rate=.002, fee_rate=.002))
    # Old callers did not carry a fee override. Their cash/entry fee remains unchanged.
    legacy = replace(plan, fee_rate=None, financing_source=None)
    path = tmp_path/'legacy_costs.sqlite3'
    db = Store(path)
    opened = PaperExecutionEngine(db).buy(legacy, observed, 'legacy-fee', decision,
        cfg(cfd_fee_rate=0., fee_rate=.002), {})
    assert opened['fee_rate'] == .002
    assert opened['entry_fee'] == legacy.entry_fee
    db.close()
    db = Store(path)
    held = db.position(SYMBOL)
    assert held['fee_rate'] == .002 and held['financing_daily'] == legacy.financing_daily
    current = quote(104., 104.02)
    PaperExecutionEngine(db).sell(current, 'LEGACY_CLOSE')
    closed = db.trades()[0]
    assert closed['exit_fee'] == pytest.approx(closed['exit']*closed['quantity']*.002*current.conversion_ask)
    db.close()


def test_default_zero_cfd_commissions_survive_round_trip_and_24h_time_exit(tmp_path, monkeypatch):
    clock = {'seconds': time.time()}
    monkeypatch.setattr('app.execution.time.time', lambda: clock['seconds'])
    plan, decision, observed, settings = cost_plan(config=cfg(cfd_fee_rate=Settings().cfd_fee_rate))
    db = Store(tmp_path/'zero_commission.sqlite3')
    engine = PaperExecutionEngine(db)
    opened = engine.buy(plan, observed, 'default-cfd-cost', decision, settings, {})
    assert opened['fee_rate'] == 0. and opened['entry_fee'] == 0.
    assert opened['detail']['cfd_costs']['financing_source'] == FALLBACK_SOURCE
    clock['seconds'] += 86401
    engine.protect(quote())
    closed = db.trades()[0]
    assert closed['exit_reason'] == 'DUREE_MAX_24H'
    assert closed['exit_fee'] == 0. and closed['fees'] == 0.
    expected_carry = plan.entry*plan.quantity*plan.fx_open*plan.financing_daily*86401/86400
    assert closed['financing_accrued'] == pytest.approx(expected_carry)
    assert closed['detail']['exit']['financing_estimate'] is True
    assert closed['detail']['exit']['financing_source'] == FALLBACK_SOURCE
    assert closed['detail']['exit']['financing_method']
    assert db.get('cash') == pytest.approx(900.+closed['net_pnl'])
    db.close()


def test_financing_is_prorated_to_elapsed_time_and_never_credits_or_rewinds():
    position = dict(entry=100., quantity=2., multiplier=1., fx_open=1.01,
        financing_daily=.0002, financing_at=1000000, financing_accrued=.03)
    assert cfd_financing(position, 1000000+21600000, 1.) == pytest.approx(.03+200*1.01*.0002*.25)
    assert cfd_financing(position, 1000000+86400000, 1.02) == pytest.approx(.03+200*1.02*.0002)
    assert cfd_financing(position, 999999, 1.02) == .03
