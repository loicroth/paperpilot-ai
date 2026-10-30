"""Profile integration on frozen public candles; never profitability evidence."""
import json
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from app.ai import AIClient
from app.config import Settings
from app.engine import TradingEngine
from app.market import Candle, Quote, Rules
from app.storage import Store
from app.indicators import pullback_reference


DATA=json.loads((Path(__file__).parent/'fixtures'/'pullback_pump_closed.json').read_text())


class FrozenMarket:
    def __init__(self,spread=0):
        self.now=DATA['boundary_ms']+5000
        self.rows={tf:[Candle(**x) for x in DATA[tf]] for tf in ('m5','h1')}
        close=self.rows['m5'][-1].close
        self.price=Quote(DATA['symbol'],close*(1-spread/20000),close*(1+spread/20000),self.now)
    async def server_time(self):return self.now
    async def quote(self,symbol):assert symbol==self.price.symbol;return self.price
    async def rules(self,symbols):return {symbol:Rules(**DATA['rules']) for symbol in symbols}
    async def candles(self,symbol,interval,limit=150,*args,**kwargs):return self.rows['m5' if interval=='5m' else 'h1'][-limit:]
    async def close(self):pass


@pytest.mark.asyncio
@pytest.mark.parametrize('profile',['classic','expanded_v1'])
@pytest.mark.parametrize('spread',[0,5])
async def test_profile_selects_exact_pullback_plan_and_executes_with_real_indicators(tmp_path,monkeypatch,profile,spread):
    market=FrozenMarket(spread)
    monkeypatch.setattr('app.engine.time.time',lambda:market.now/1000)
    store=Store(tmp_path/'profile.sqlite3')
    cfg=Settings(symbols=[DATA['symbol']],strategy_profile=profile)
    store.set('settings',cfg.model_dump());store.set('mode','running')
    called=[]
    def handler(request):
        ctx=json.loads(json.loads(request.content)['input'][0]['content'][0]['text'])
        called.append(ctx)
        chosen=next(p for p in ctx['entry_plans'] if p['setup']=='pullback')
        assert 'h1_structure_candles' not in ctx['markets'][DATA['symbol']]
        answer={'choice':{'id':chosen['id'],'risk_pct':None},'reason':'Reprise observée vers structure H1.',
            'confirmation':'Bougie clôturée après repli.','risks':['Échec de reprise possible.']}
        return httpx.Response(200,json={'status':'completed','usage':{'input_tokens':100,'output_tokens':60},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(answer)}]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ai=AIClient('fixture-key',client=http);ai.test_enabled=True
        engine=TradingEngine(store,SimpleNamespace(api_key='',model='gpt-4o-mini'),market=market,ai=ai)
        engine.started_ms=market.now-10000;engine.health['market_ok']=True;engine.reconciled=True
        await engine._analyze()
    if profile=='classic':
        assert called==[] and store.positions()==[] and store.get('cash')==900
    else:
        assert len(called)==1
        p=store.position(DATA['symbol'])
        assert p and p['detail']['plan']['rr']>=2 and p['risk_usdt']<=9.00000001
        audit=p['detail']['plan']['structure_audit']
        assert audit['target_obstacle']['timeframe']=='H1'
        raw=p['detail']['structure_context']
        ref=pullback_reference(raw['m5'],raw['h1'],now_ms=market.now)
        assert audit['stop_reference']['price']==ref['price']
        assert len(raw['h1_structure_candles'])==150
        assert p['detail']['context']['rules']['strategy_profile']=='expanded_v1'
        assert store.rows('SELECT rejection FROM decisions')[-1]['rejection'] is None
    store.close()


def test_profile_is_persisted_once_without_resetting_capital_or_mode(tmp_path):
    store=Store(tmp_path/'profile_persistence.sqlite3')
    cfg=Settings().model_dump();cfg.pop('strategy_profile')
    store.set('settings',cfg);store.set('mode','paused');store.set('cash',873.)
    fake=SimpleNamespace(model='fixture')
    TradingEngine(store,SimpleNamespace(api_key='',model='gpt-4o-mini'),market=FrozenMarket(),ai=fake)
    assert store.get('settings')['strategy_profile']=='expanded_v1'
    assert store.get('cash')==873. and store.get('mode')=='paused'
    store.set('settings',{**store.get('settings'),'strategy_profile':'classic'})
    TradingEngine(store,SimpleNamespace(api_key='',model='gpt-4o-mini'),market=FrozenMarket(),ai=fake)
    assert store.get('settings')['strategy_profile']=='classic'
    store.close()
