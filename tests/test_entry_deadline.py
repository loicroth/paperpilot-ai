"""Entry deadlines use local execution time, independently of fresh feed events."""
from dataclasses import replace
from datetime import datetime, timezone
import pytest
from app.ai import Decision
from app.indicators import snapshot
from test_engine import rig
from test_multi_engine import configure


async def entry_fixture(rig,monkeypatch):
    engine,store,market,ai,_=configure(rig,monkeypatch)
    monkeypatch.setattr('app.engine.time.time',lambda:market.now/1000)
    symbol='CAP:NVDA'
    data={'m5':snapshot(await market.candles(symbol,'5m')),
          'h1':snapshot(await market.candles(symbol,'1h'))}
    cfg=engine.settings()
    context={'mode':'entry','symbols':[symbol],'markets':{symbol:data},
        'rules':cfg.model_copy(update={'symbols':[symbol]}).model_dump(),
        'positions':[],'candidates':{symbol:['breakout']},'directions':{symbol:['SHORT']},
        'direction_candidates':{symbol:{'SHORT':['breakout']}}}
    expected=market.now//300000*300000-1
    args=(context,{symbol:data},cfg,engine._settings_revision,expected,
          f'm5:{expected}',datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    return engine,store,market,ai,args


@pytest.mark.asyncio
@pytest.mark.parametrize('elapsed_ms,event_age_ms,opens',[
    (64999,10000,True),(65000,10000,True),(65001,10000,False),(70000,10000,False),
    (70000,0,False)])
async def test_local_entry_deadline_boundary_cannot_be_bypassed_by_older_fresh_event(rig,monkeypatch,elapsed_ms,event_age_ms,opens):
    engine,store,market,ai,args=await entry_fixture(rig,monkeypatch)
    expected=args[4];original_decide=ai.decide;original_quote=market.quote
    async def decide(context):
        result=await original_decide(context)
        market.now=expected+elapsed_ms
        return result
    async def quote(symbol):
        observed=await original_quote(symbol)
        return replace(observed,time_ms=observed.time_ms-event_age_ms)
    monkeypatch.setattr(ai,'decide',decide)
    monkeypatch.setattr(market,'quote',quote)
    await engine._decide_and_execute(*args)
    decision=store.rows('SELECT action,rejection FROM decisions ORDER BY id DESC LIMIT 1')[0]
    assert len(ai.contexts)==1 and decision['action']=='SHORT'
    assert bool(store.position('CAP:NVDA')) is opens
    if opens:assert decision['rejection'] is None
    else:assert '>65 s' in decision['rejection']


@pytest.mark.asyncio
@pytest.mark.parametrize('elapsed_ms',[65000,65001,90000])
async def test_expired_entry_window_does_not_call_ai_or_spend_budget(rig,monkeypatch,elapsed_ms):
    engine,store,market,ai,args=await entry_fixture(rig,monkeypatch)
    market.now=args[4]+elapsed_ms
    before=store.rows('SELECT * FROM usage')
    result=await engine._decide_and_execute(*args)
    assert result=={'error':None,'error_kind':None}
    assert not ai.contexts and store.rows('SELECT * FROM usage')==before
    assert store.position() is None
    record=store.rows('SELECT action,rejection FROM decisions ORDER BY id DESC LIMIT 1')[0]
    assert record['action']=='WAIT' and 'avant appel IA' in record['rejection']


@pytest.mark.asyncio
async def test_deadline_rechecked_immediately_after_risk_planning_before_order(rig,monkeypatch):
    engine,store,market,ai,args=await entry_fixture(rig,monkeypatch)
    original_decide=ai.decide
    async def decide(context):
        result=await original_decide(context)
        market.now=args[4]+64000
        return result
    monkeypatch.setattr(ai,'decide',decide)
    import app.engine as module
    original_plan=module.RiskEngine.plan;planned=[]
    def delayed_plan(*positional,**kwargs):
        plan=original_plan(*positional,**kwargs)
        planned.append(plan)
        market.now=args[4]+65001
        return plan
    monkeypatch.setattr(module.RiskEngine,'plan',delayed_plan)
    await engine._decide_and_execute(*args)
    assert planned and store.position() is None
    assert '>65 s' in store.rows('SELECT rejection FROM decisions ORDER BY id DESC LIMIT 1')[0]['rejection']


@pytest.mark.asyncio
async def test_existing_position_management_is_not_limited_by_entry_deadline(rig,monkeypatch):
    engine,store,market,ai,args=await entry_fixture(rig,monkeypatch)
    await engine._decide_and_execute(*args)
    held=store.position('CAP:NVDA');assert held
    ai.proposal=Decision(symbol='CAP:NVDA',action='HOLD',reason='Plan conservé.',entry=None,
        stop_loss=None,take_profit=None,invalidation=None,confirmation='Aucune nouvelle entrée.',
        risks=[],setup='none',confirmation_met=False)
    market.now=args[4]+90000
    context={**args[0],'mode':'management','managed_symbol':'CAP:NVDA','positions':[held]}
    await engine._decide_and_execute(context,*args[1:])
    assert len(ai.contexts)==2 and store.position('CAP:NVDA')['id']==held['id']
    record=store.rows('SELECT action,rejection FROM decisions ORDER BY id DESC LIMIT 1')[0]
    assert record=={'action':'HOLD','rejection':None}
