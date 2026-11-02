"""Local exit invariants only; fixture results are not profitability evidence."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json

import pytest

from app.ai import AIResult, Decision
from app.management import EXIT_REASON, POLICY, management_decision, opening_plan, uses_v2_policy
from app.market import Quote
from app.risk import OrderPlan
from test_engine import rig, buy_decision


NOW = int(datetime(2026, 11, 2, 12, 5, 5, tzinfo=timezone.utc).timestamp()*1000)


def position(short=False, profile='expanded_v2'):
    return {'symbol': 'CAP:TEST' if short else 'BTCUSDT', 'kind': 'cfd' if short else 'spot',
            'direction': 'SHORT' if short else 'LONG',
            'opened_at': '2026-11-02T10:05:00+00:00', 'entry': 100.,
            'stop_loss': 102. if short else 98., 'take_profit': 92. if short else 108.,
            'unrealized_pnl': -1.,
            'detail': {'context': {'rules': {'strategy_profile': profile}, 'secret': 'never-copy'},
                       'proposal': {'setup': 'pullback', 'invalidation': 101.8 if short else 98.2},
                       'plan': {'structure_audit': {
                           'stop_reference': {'price': 101.7 if short else 98.3, 'timeframe': 'M5',
                                              'source': 'fixture', 'unknown': 'never-copy'},
                           'target_obstacle': {'price': 91.9 if short else 108.1, 'timeframe': 'H1'}}},
                       'secret': 'never-copy'}}


def market(short=False):
    rows=[]
    close=102. if short else 99.
    end=NOW//3_600_000*3_600_000-1
    for index in range(3):
        stop=end-(2-index)*3_600_000
        rows.append({'open_time':stop-3_600_000+1,'close_time':stop,
                     'open':close,'high':close+.2,'low':close-.2,'close':close,'volume':100.})
    return {'h1':{'candles':rows,'last':rows[-1],'previous':rows[-2],
                  'ema21':101. if short else 100.,'ema50':100. if short else 101.,
                  'ema21_previous':100.8 if short else 100.2},
            'candidates': [], 'directions': []}


@pytest.mark.parametrize('short',[False,True])
def test_full_opposite_closed_h1_after_opening_exits(short):
    decision,audit=management_decision(position(short),market(short),now_ms=NOW)
    assert decision.action=='SELL' and audit['exit_reason']==EXIT_REASON
    assert audit['condition_met'] and audit['data_valid']
    assert all(getattr(decision,key) is None for key in ('entry','stop_loss','take_profit','invalidation','risk_pct'))


@pytest.mark.parametrize('short',[False,True])
@pytest.mark.parametrize('change',['close','ema_order','slope','equal_slope','equal_order','before_opening'])
def test_incomplete_or_old_invalidation_holds_despite_loss_and_missing_entry_candidates(short,change):
    p,m=position(short),market(short)
    h=m['h1']
    if change=='close':
        h['last']['close']=100.5 if not short else 100.5
        h['last']['high']=103.;h['last']['low']=98.
    elif change=='ema_order':h['ema50']=99.5 if not short else 101.5
    elif change=='slope':h['ema21_previous']=99.8 if not short else 101.2
    elif change=='equal_slope':h['ema21_previous']=h['ema21']
    elif change=='equal_order':h['ema50']=h['ema21']
    else:p['opened_at']='2026-11-02T12:00:00+00:00'
    decision,audit=management_decision(p,m,now_ms=NOW)
    assert decision.action=='HOLD' and not audit['condition_met'] and audit['exit_reason'] is None


@pytest.mark.parametrize('change',[
    'nan_ema','inf_close','bad_ohlc','future_bar','stale_bar','out_of_order','bad_duration',
    'boolean_timestamp','last_mismatch','previous_mismatch','missing_ema','empty_bars','naive_opening',
    'future_opening','bad_now','bad_direction','spot_short','legacy_profile','nan_entry','incoherent_stop','missing_invalidation'])
def test_malformed_or_untrusted_policy_input_never_sells(change):
    p,m=position(),market();now=NOW;h=m['h1']
    if change=='nan_ema':h['ema21']=float('nan')
    elif change=='inf_close':h['last']['close']=float('inf')
    elif change=='bad_ohlc':h['last']['low']=h['last']['high']+1
    elif change=='future_bar':
        for bar in h['candles']:bar['open_time']+=3_600_000;bar['close_time']+=3_600_000
    elif change=='stale_bar':
        for bar in h['candles']:bar['open_time']-=3_600_000;bar['close_time']-=3_600_000
    elif change=='out_of_order':h['candles'][0],h['candles'][1]=h['candles'][1],h['candles'][0]
    elif change=='bad_duration':h['last']['open_time']+=1
    elif change=='boolean_timestamp':h['last']['open_time']=True
    elif change=='last_mismatch':h['last']={**h['last'],'close':98.9}
    elif change=='previous_mismatch':h['previous']={**h['previous'],'close':98.9}
    elif change=='missing_ema':del h['ema50']
    elif change=='empty_bars':h['candles']=[]
    elif change=='naive_opening':p['opened_at']='2026-11-02T10:00:00'
    elif change=='future_opening':p['opened_at']='2026-11-03T10:00:00Z'
    elif change=='bad_now':now=float('nan')
    elif change=='bad_direction':p['direction']='UNKNOWN'
    elif change=='spot_short':p['direction']='SHORT'
    elif change=='nan_entry':p['entry']=float('nan')
    elif change=='incoherent_stop':p['stop_loss']=103.
    elif change=='missing_invalidation':del p['detail']['proposal']['invalidation']
    else:p['detail']['context']['rules']['strategy_profile']='expanded_v1'
    decision,audit=management_decision(p,m,now_ms=now)
    assert decision.action=='HOLD' and not audit['condition_met'] and not audit['data_valid']


def test_opening_plan_is_a_sanitized_whitelist_not_full_detail():
    p=position();clean=opening_plan(p)
    assert clean['setup']=='pullback' and clean['invalidation']==98.2
    assert clean['stop_reference']['price']==98.3
    assert 'never-copy' not in json.dumps(clean)
    assert uses_v2_policy(p)
    assert not uses_v2_policy({'detail':None}) and not uses_v2_policy(None)


def install_v2_position(rig,monkeypatch,*,short=False,opposite=True,profile='expanded_v2'):
    engine,store,feed,ai,_=rig
    feed.now=NOW;engine.started_ms=NOW-10_000
    monkeypatch.setattr('app.engine.time.time',lambda:NOW/1000)
    settings=engine.settings().model_copy(update={'strategy_profile':'expanded_v2','cfd_enabled':short})
    store.set('settings',settings.model_dump())
    symbol='CAP:TEST' if short else 'BTCUSDT'
    decision=buy_decision().model_copy(update={'symbol':symbol,'action':'SHORT' if short else 'BUY',
        'entry':100. if short else 100.02,'stop_loss':102. if short else 98.,
        'take_profit':92. if short else 108.,'invalidation':102. if short else 98.})
    q=Quote(symbol,100.,100.02,NOW,currency='USD' if short else 'USDT',conversion_bid=1.,conversion_ask=1.)
    plan=OrderPlan(symbol,.5,99.95 if short else 100.07,102. if short else 98.,92. if short else 108.,
                   0. if short else .050035,1.2,2.5,kind='cfd' if short else 'spot',
                   direction='SHORT' if short else 'LONG',margin=25. if short else 0.,leverage=2. if short else 1.,
                   currency='USD' if short else 'USDT',fee_rate=0. if short else None)
    detail={'context':{'rules':{'strategy_profile':profile}},'proposal':decision.model_dump(),'plan':asdict(plan)}
    engine.paper.buy(plan,q,'management-fixture',decision,settings,detail)
    store.conn.execute('UPDATE trades SET opened_at=?',('2026-11-02T10:05:00+00:00',))
    engine.quotes={s:Quote(s,100.,100.02,NOW) for s in settings.symbols}
    engine.quotes[symbol]=q;engine._set_reconciliation(store.position(symbol),True)
    original=feed.candles
    async def candles(s,interval,*args,**kwargs):
        bars=await original(s,interval,*args,**kwargs)
        if interval=='1h':
            falling=(not short) if opposite else short
            for index,bar in enumerate(bars):
                close=110-index*.1 if falling else 85+index*.1
                bars[index]=replace(bar,open=close,high=close+.2,low=close-.2,close=close)
        return bars
    async def quote(s):
        return Quote(s,100.,100.02,NOW,currency='USD' if s.startswith('CAP:') else 'USDT',conversion_bid=1.,conversion_ask=1.)
    monkeypatch.setattr(feed,'candles',candles);monkeypatch.setattr(feed,'quote',quote)
    monkeypatch.setattr('app.engine.candidates',lambda *a,**k:[])
    monkeypatch.setattr('app.engine.short_candidates',lambda *a,**k:[])
    monkeypatch.setattr('app.engine.pullback_reference',lambda *a,**k:None)
    return engine,store,feed,ai,symbol


@pytest.mark.asyncio
@pytest.mark.parametrize('short',[False,True])
async def test_v2_engine_closes_opposite_h1_without_api_and_preserves_api_failure(rig,monkeypatch,short):
    engine,store,feed,ai,symbol=install_v2_position(rig,monkeypatch,short=short)
    engine.health.update(ai_ok=False,ai_error='API credits exhausted',ai_error_kind='quota')
    await engine._analyze()
    assert not ai.contexts and not store.rows('SELECT * FROM usage')
    assert store.position(symbol) is None
    trade=store.trades()[0]
    assert trade['exit_reason']==EXIT_REASON
    assert trade['detail']['exit']['management_policy']['condition_met']
    assert trade['detail']['exit']['decision_source']=='local_closed_h1_v2'
    assert engine.health['ai_ok'] is False and engine.health['ai_error_kind']=='quota'


@pytest.mark.asyncio
@pytest.mark.parametrize('short',[False,True])
async def test_v2_engine_holds_when_its_entry_signal_disappears(rig,monkeypatch,short):
    engine,store,feed,ai,symbol=install_v2_position(rig,monkeypatch,short=short,opposite=False)
    await engine._analyze()
    assert not ai.contexts and store.position(symbol)
    row=store.rows('SELECT * FROM decisions')[-1]
    assert row['action']=='HOLD' and row['rejection'] is None
    detail=json.loads(row['detail'])
    assert detail['context']['opening_plan']['setup']=='breakout'
    assert detail['context']['management_policy']==POLICY


@pytest.mark.asyncio
async def test_local_management_plus_expired_entry_cannot_clear_api_fault(rig,monkeypatch):
    engine,store,feed,ai,symbol=install_v2_position(rig,monkeypatch,opposite=False)
    engine.health.update(ai_ok=False,ai_error='API credits exhausted',ai_error_kind='quota')
    def ready_then_expired(*args):
        # Entry planning can consume the remaining 65-second window. Its
        # early return is not a successful API response either.
        monkeypatch.setattr('app.engine.time.time',lambda:(NOW+70_000)/1000)
        return [{'id':'expired-fixture'}]
    monkeypatch.setattr('app.engine.build_entry_plans',ready_then_expired)
    await engine._analyze()
    assert not ai.contexts and store.position(symbol)
    assert engine.health['ai_ok'] is False and engine.health['ai_error_kind']=='quota'
    assert not store.rows('SELECT * FROM usage')


@pytest.mark.asyncio
async def test_legacy_open_position_keeps_ai_policy_even_when_current_settings_are_v2(rig,monkeypatch):
    engine,store,feed,ai,symbol=install_v2_position(rig,monkeypatch,profile='expanded_v1')
    ai.proposal=Decision(symbol=symbol,action='HOLD',reason='Legacy fixture',entry=None,stop_loss=None,
        take_profit=None,invalidation=None,confirmation='Legacy fixture',risks=[],setup='none',confirmation_met=False)
    await engine._analyze()
    assert len(ai.contexts)==1 and ai.contexts[0]['mode']=='management'
    assert 'management_policy' not in ai.contexts[0]
    assert store.position(symbol)


@pytest.mark.asyncio
async def test_v2_final_gate_rejects_changed_opening_cohort(rig,monkeypatch):
    engine,store,feed,ai,symbol=install_v2_position(rig,monkeypatch)
    async def changed(*args,**kwargs):
        p=store.position(symbol);p['detail']['context']['rules']['strategy_profile']='expanded_v1'
        store.conn.execute('UPDATE trades SET detail=? WHERE id=?',(json.dumps(p['detail']),p['id']))
    monkeypatch.setattr(engine,'reconcile',changed)
    await engine._analyze()
    assert store.position(symbol) and not ai.contexts
    assert 'Invalidation H1 v2 absente' in store.rows('SELECT * FROM decisions')[-1]['rejection']


@pytest.mark.asyncio
@pytest.mark.parametrize('price,expected_reason',[(97.,'STOP_LOSS'),(110.,'TAKE_PROFIT')])
async def test_v2_local_exit_never_preempts_fresh_stop_or_target(rig,monkeypatch,price,expected_reason):
    engine,store,feed,ai,symbol=install_v2_position(rig,monkeypatch)
    count=0
    async def quote(s):
        nonlocal count
        if s==symbol:count+=1
        value=price if s==symbol and count>=2 else 100.
        return Quote(s,value,value+.02,NOW)
    monkeypatch.setattr(feed,'quote',quote)
    await engine._analyze()
    assert not store.position(symbol) and not ai.contexts
    assert store.trades()[0]['exit_reason']==expected_reason
