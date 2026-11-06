"""A protection fill during a scan must not turn into a stale management trade."""
from copy import deepcopy
from dataclasses import replace

import pytest

from app.ai import Decision
from app.market import Quote
from app.risk import OrderPlan
from test_engine import rig
from test_management_policy import NOW, POLICY, install_v2_position, market


@pytest.mark.asyncio
@pytest.mark.parametrize('replace_position', [False, True])
async def test_closed_position_during_scan_does_not_pause_engine_or_manage_replacement(
        rig, monkeypatch, replace_position):
    engine, store, feed, ai, symbol = install_v2_position(
        rig, monkeypatch, short=True, opposite=False)
    original = store.position(symbol)
    plan = OrderPlan(**original['detail']['plan'])
    proposal = Decision.model_validate(original['detail']['proposal'])
    q = Quote('CAP:OTHER', 100., 100.02, NOW, currency='USD', conversion_bid=1., conversion_ask=1.)
    other = engine.paper.buy(replace(plan, symbol=q.symbol), q, 'other-held-fixture',
        proposal.model_copy(update={'symbol':q.symbol}), engine.settings(), deepcopy(original['detail']))
    other_before = store.rows('SELECT * FROM trades WHERE id=?', (other['id'],))[0]
    prepare = engine._prepare_entry_candidates
    cash_after_protection = None
    replacement_id = None

    async def protect_while_scanning(*args):
        nonlocal cash_after_protection, replacement_id
        # Fast quote protection is independent from the asynchronous M5 scan.
        stop_quote = Quote(symbol, 102.2, 102.22, NOW, currency='USD',
                           conversion_bid=1., conversion_ask=1.)
        engine.paper.protect(stop_quote)
        assert store.position(symbol) is None
        if replace_position:
            fresh = Quote(symbol, 100., 100.02, NOW, currency='USD',
                          conversion_bid=1., conversion_ask=1.)
            replacement = engine.paper.buy(plan, fresh, 'replacement-fixture', proposal,
                                            engine.settings(), deepcopy(original['detail']))
            replacement_id = replacement['id']
        cash_after_protection = store.get('cash')
        return await prepare(*args)

    monkeypatch.setattr(engine, '_prepare_entry_candidates', protect_while_scanning)
    await engine.analyze()

    assert engine.critical_error is None
    assert store.get('mode') == 'running'
    assert not ai.contexts and not store.rows('SELECT * FROM usage')
    assert store.get('cash') == cash_after_protection
    assert store.rows('SELECT * FROM trades WHERE id=?', (other['id'],))[0] == other_before
    closed = store.trades()
    assert len(closed) == 1 and closed[0]['id'] == original['id']
    assert closed[0]['exit_reason'] == 'STOP_LOSS'
    assert (store.position(symbol) or {}).get('id') == replacement_id
    # A completed scan is a normal cancellation of this stale management job,
    # not an invented HOLD/SELL for the already closed opening identity.
    assert not store.rows("SELECT * FROM decisions WHERE action IN ('HOLD','SELL')")


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['closed', 'empty_capture', 'replacement'])
async def test_retired_management_context_never_raises_or_spends_api_budget(
        rig, monkeypatch, state):
    engine, store, feed, ai, symbol = install_v2_position(rig, monkeypatch, short=True)
    original = store.position(symbol)
    context = {'mode':'management', 'management_policy':POLICY, 'managed_symbol':symbol,
               'positions':[] if state=='empty_capture' else [original], 'markets':{symbol:market(True)}}
    engine.paper.protect(Quote(symbol, 102.2, 102.22, NOW, currency='USD',
                               conversion_bid=1., conversion_ask=1.))
    if state=='replacement':
        engine.paper.buy(OrderPlan(**original['detail']['plan']),
            Quote(symbol, 100., 100.02, NOW, currency='USD', conversion_bid=1., conversion_ask=1.),
            'replacement-after-context', Decision.model_validate(original['detail']['proposal']),
            engine.settings(), deepcopy(original['detail']))
    positions = store.positions()
    cash = store.get('cash')
    closed = store.trades()
    result = await engine._decide_and_execute(context, {symbol:market(True)}, engine.settings(),
        engine._settings_revision, NOW-5001, 'retired-management-fixture', '2026-11-02')
    assert result['error'] is None and not result['api_called']
    assert not ai.contexts and not store.rows('SELECT * FROM usage')
    assert store.get('cash') == cash and store.trades() == closed
    assert store.positions() == positions and store.get('mode') == 'running'
    assert not store.rows("SELECT * FROM decisions WHERE action IN ('HOLD','SELL')")
