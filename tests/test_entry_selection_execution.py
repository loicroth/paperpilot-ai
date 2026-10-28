"""Synthetic end-to-end selection, risk checks and SQLite paper execution."""
import json
import httpx
import pytest
from app.ai import AIClient
from test_engine import rig, MarketFixture
from test_multi_engine import configure


@pytest.mark.asyncio
@pytest.mark.parametrize('short',[False,True])
async def test_real_ai_selection_opens_cfd_through_risk_and_execution(rig,monkeypatch,short):
    engine,store,market,_,_=configure(rig,monkeypatch)
    if not short:
        async def bullish_candles(symbol,interval,limit=150,start_time=None,end_time=None):
            return await MarketFixture.candles(market,symbol,interval,limit,start_time,end_time)
        monkeypatch.setattr(market,'candles',bullish_candles)
        monkeypatch.setattr('app.engine.candidates',lambda *a,**k:['breakout'])
        monkeypatch.setattr('app.engine.short_candidates',lambda *a,**k:[])
    chosen=[]
    def handler(request):
        payload=json.loads(request.content)
        context=json.loads(payload['input'][0]['content'][0]['text'])
        assert context['mode']=='entry'
        plan=next(p for p in context['entry_plans'] if p['symbol']=='CAP:NVDA' and p['action']==('SHORT' if short else 'BUY'))
        chosen.append(plan)
        selection=dict(choice=dict(id=plan['id'],risk_pct=None),reason='Structure observée.',confirmation='Configuration clôturée.',risks=['Gap'])
        return httpx.Response(200,json=dict(status='completed',usage=dict(input_tokens=100,output_tokens=50),
            output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(selection))])]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ai=AIClient('fixture-never-live',client=http);ai.test_enabled=True;engine.ai=ai
        await engine._analyze()
    assert len(chosen)==1
    p=store.position('CAP:NVDA')
    assert p is not None and p['kind']=='cfd' and p['direction']==('SHORT' if short else 'LONG')
    assert p['detail']['plan']['structure_audit']['validated']
    assert p['detail']['plan']['rr']>=2
    assert p['risk_usdt']<=12.6 and p['quantity']>0 and 0<store.get('cash')<900
    for key in ('entry','stop_loss','take_profit','invalidation','setup'):
        assert p['detail']['proposal'][key]==chosen[0][key]
    assert store.rows('SELECT action,rejection FROM decisions')[-1]=={'action':'SHORT' if short else 'BUY','rejection':None}


@pytest.mark.asyncio
async def test_wait_selection_on_feasible_catalog_never_opens_position(rig):
    engine,store,market,_,_=rig
    def handler(request):
        context=json.loads(json.loads(request.content)['input'][0]['content'][0]['text'])
        assert context['entry_plans']
        selection=dict(choice=dict(id='WAIT',risk_pct=None),reason='Avantage insuffisant.',confirmation='Aucune entrée retenue.',risks=[])
        return httpx.Response(200,json=dict(status='completed',usage=dict(input_tokens=100,output_tokens=50),
            output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(selection))])]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ai=AIClient('fixture-never-live',client=http);ai.test_enabled=True;engine.ai=ai
        await engine._analyze()
    assert store.positions()==[] and store.get('cash')==900
    assert store.rows('SELECT action,rejection FROM decisions')==[{'action':'WAIT','rejection':None}]
