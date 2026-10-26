"""HTTP actions with fixture quotes. No network or paid API calls are made."""
from types import SimpleNamespace
import csv
import io
import sqlite3
import time
import httpx
import pytest
from app.ai import Decision
from app.config import Environment, Settings
from app.engine import TradingEngine
from app.market import Quote, Rules
from app.risk import RiskEngine
from app.storage import Store
from app.web import create_app

class FixtureMarket:
    def __init__(self):
        self.bid=100.0; self.rules_calls=[]; self.quote_calls=[]
    async def server_time(self): return int(time.time()*1000)
    async def quote(self,symbol):
        self.quote_calls.append(symbol)
        return Quote(symbol,self.bid,self.bid+.01,int(time.time()*1000))
    async def rules(self,symbols):
        self.rules_calls.append(list(symbols))
        return {s:Rules(s,.001,100000.0,.001,5.0,1000000.0,.01) for s in symbols}
    async def close(self): pass

class NoNetworkAI:
    async def decide(self,context): raise AssertionError('No AI call allowed in web fixture')
    async def close(self): pass

@pytest.fixture
def web(tmp_path):
    store=Store(tmp_path/'web.sqlite3')
    env=SimpleNamespace(api_key='',model='gpt-4o-mini',binance_url='https://data-api.binance.vision',
        password='',data_dir=tmp_path,host='127.0.0.1',port=8000)
    market=FixtureMarket()
    bot=TradingEngine(store,env,market=market,ai=NoNetworkAI())
    app=create_app(env=env,store=store,engine=bot,background=False)
    yield SimpleNamespace(app=app,bot=bot,store=store,env=env,market=market)
    store.close()

def client(web,auth=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),base_url='http://testserver',auth=auth)

async def token(c):
    response=await c.get('/api/state')
    assert response.status_code==200
    return {'X-CSRF-Token':response.json()['csrf_token']}

def open_fixture(web):
    q=Quote('BTCUSDT',100.0,100.01,int(time.time()*1000))
    d=Decision(symbol='BTCUSDT',action='BUY',reason='Fixture HTTP : cassure.',entry=100.01,
        stop_loss=99.0,take_profit=103.5,invalidation=99.2,confirmation='Clôture fixture.',
        risks=['Données synthétiques de test'],setup='breakout',confirmation_met=True)
    cfg=Settings(); rules=Rules('BTCUSDT',.001,100000.0,.001,5.0,1000000.0,.01)
    # Coherent observed fixture ranges justify both the structural stop and target.
    context={}
    for name,duration in [('m5',300000),('h1',3600000)]:
        end=q.time_ms//duration*duration-1
        bars=[{'open_time':end-(5-i)*duration-duration+1,'close_time':end-(5-i)*duration,
               'open':100.,'high':104.,'low':99.2,'close':100.,'volume':100.} for i in range(6)]
        context[name]={'candles':bars,'atr14':.8,'last':bars[-1]}
    plan=RiskEngine.plan(d,q,rules,cfg,900,900,atr=.8,structure_context=context)
    return web.bot.paper.buy(plan,q,'fixture-http-m5',d,cfg,{'fixture':True})

@pytest.mark.asyncio
async def test_dashboard_static_assets_and_empty_state(web):
    async with client(web) as c:
        response=await c.get('/')
        assert response.status_code==200
        assert 'PaperPilot' in response.text
        for path in ('/static/style.css','/static/app.js'):
            r=await c.get(path); assert r.status_code==200; assert r.content
        state=(await c.get('/api/state')).json()
        assert state['paper_only'] is True
        assert state['ai_configured'] is False
        assert state['control']['mode']=='stopped'
        assert state['position'] is None
        assert state['trades']==[] and state['decisions']==[]
        assert state['stats']['cash']==900 and state['stats']['equity']==900
        assert state['csrf_token']
        assert 'api_key' not in state
        assert response.headers['x-frame-options']=='DENY'
        assert response.headers['cache-control']=='no-store'
        assert "default-src 'self'" in response.headers['content-security-policy']

@pytest.mark.asyncio
async def test_control_actions_and_unknown_mode(web):
    async with client(web) as c:
        headers=await token(c)
        for action,mode in [('start','running'),('pause','paused'),('stop','stopped')]:
            r=await c.post('/api/control',json={'action':action},headers=headers)
            assert r.status_code==200
            assert r.json()['mode']==mode and web.store.get('mode')==mode
        assert (await c.post('/api/control',json={'action':'live'},headers=headers)).status_code==400
        for path in ('/api/live','/docs','/openapi.json'):
            assert (await c.get(path)).status_code==404

@pytest.mark.asyncio
@pytest.mark.parametrize('action',['pause','stop'])
async def test_pause_or_stop_retains_holdings_and_protection_still_closes_stop(web,action):
    p=open_fixture(web)
    async with client(web) as c:
        headers=await token(c)
        assert (await c.post('/api/control',json={'action':action},headers=headers)).status_code==200
        assert web.store.position()['id']==p['id']
        web.market.bid=98.0
        await web.bot.tick()
        assert 'BTCUSDT' in web.market.quote_calls
        assert web.store.position() is None
        assert web.store.trades()[0]['exit_reason']=='STOP_LOSS'
        assert web.store.get('cash')<900

@pytest.mark.asyncio
async def test_settings_persist_and_are_validated_with_binance_rules(web):
    async with client(web) as c:
        headers=await token(c)
        settings=(await c.get('/api/state')).json()['settings']
        settings.update(symbols=['BTC/USDT','ETHUSDT'],risk_pct=.5,max_daily_calls=20)
        assert (await c.put('/api/settings',json=settings,headers=headers)).status_code==200
        assert web.market.rules_calls[-1]==['BTCUSDT','ETHUSDT']
        saved=web.store.get('settings')
        assert saved['risk_pct']==.5 and saved['symbols']==['BTCUSDT','ETHUSDT']
        assert saved['max_daily_calls']==20 and web.bot._settings_revision==1
        assert (await c.get('/api/state')).json()['settings']==saved
        settings['risk_pct']=2
        assert (await c.put('/api/settings',json=settings,headers=headers)).status_code==422
        settings['risk_pct']=.5; settings['live_mode']=True
        assert (await c.put('/api/settings',json=settings,headers=headers)).status_code==422

@pytest.mark.asyncio
async def test_cannot_remove_open_position_market(web):
    p=open_fixture(web)
    async with client(web) as c:
        headers=await token(c)
        settings=(await c.get('/api/state')).json()['settings']; settings['symbols']=['ETHUSDT']
        assert (await c.put('/api/settings',json=settings,headers=headers)).status_code==409
        assert web.store.position()['id']==p['id']

@pytest.mark.asyncio
async def test_csrf_required_and_cross_origin_mutations_rejected(web):
    async with client(web) as c:
        assert (await c.post('/api/control',json={'action':'start'})).status_code==403
        assert (await c.post('/api/control',json={'action':'start'},headers={'X-CSRF-Token':'wrong'})).status_code==403
        headers=await token(c); headers['Origin']='https://attacker.invalid'
        assert (await c.post('/api/control',json={'action':'start'},headers=headers)).status_code==403
        headers['Origin']='http://testserver'
        assert (await c.post('/api/control',json={'action':'start'},headers=headers)).status_code==200

@pytest.mark.asyncio
async def test_local_only_rejects_untrusted_host(web):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),base_url='http://attacker.invalid') as c:
        assert (await c.get('/api/state')).status_code==400

@pytest.mark.asyncio
async def test_password_authentication_protects_every_route(web):
    web.env.password='fixture-only-password-123456'
    web.app=create_app(env=web.env,store=web.store,engine=web.bot,background=False)
    async with client(web) as c:
        for path in ('/','/api/state','/static/style.css','/static/app.js','/api/export/trades.csv','/api/export/stats.csv'):
            r=await c.get(path); assert r.status_code==401,path
            assert r.headers['WWW-Authenticate'].startswith('Basic')
        assert (await c.get('/api/state',auth=('paperpilot','wrong'))).status_code==401
        assert (await c.get('/api/state',auth=('wrong',web.env.password))).status_code==401
    async with client(web,auth=('paperpilot',web.env.password)) as c:
        headers=await token(c)
        assert (await c.get('/')).status_code==200
        assert (await c.get('/static/app.js')).status_code==200
        assert (await c.post('/api/control',json={'action':'pause'},headers=headers)).status_code==200

def test_network_bind_requires_strong_password(monkeypatch):
    monkeypatch.setenv('HOST','0.0.0.0'); monkeypatch.setenv('DASHBOARD_PASSWORD','')
    with pytest.raises(ValueError,match='16 caractères'): Environment()
    monkeypatch.setenv('DASHBOARD_PASSWORD','fixture-local-password-123')
    assert Environment().host=='0.0.0.0'

@pytest.mark.asyncio
async def test_reset_requires_confirmation_archives_open_position_and_restores_900(web):
    p=open_fixture(web)
    assert web.store.reserve_usage('2026-10-23',288,10000,100)
    async with client(web) as c:
        headers=await token(c)
        assert (await c.post('/api/reset',json={'confirmation':'yes'},headers=headers)).status_code==400
        assert web.store.position()['id']==p['id']
        response=await c.post('/api/reset',json={'confirmation':'REINITIALISER'},headers=headers)
        assert response.status_code==200 and response.json()['capital']==900
        assert web.store.position() is None and web.store.get('cash')==900
        assert web.store.get('mode')=='stopped' and web.store.trades()==[]
        backup=web.env.data_dir/'backups'/response.json()['backup']
        assert backup.exists()
        with sqlite3.connect(backup) as archive:
            assert archive.execute('SELECT id FROM trades WHERE closed_at IS NULL').fetchone()[0]==p['id']
        assert web.store.rows('SELECT calls,tokens FROM usage')[0]=={'calls':1,'tokens':100}

@pytest.mark.asyncio
async def test_csv_exports_empty_and_closed_trades_with_fees(web):
    async with client(web) as c:
        r=await c.get('/api/export/trades.csv')
        assert r.status_code==200 and 'text/csv' in r.headers['content-type']
        assert list(csv.DictReader(io.StringIO(r.text.lstrip('\ufeff'))))==[]
        r=await c.get('/api/export/stats.csv')
        rows=list(csv.DictReader(io.StringIO(r.text.lstrip('\ufeff'))))
        assert any(row['statistique']=='initial_capital' and float(row['valeur'])==900 for row in rows)
        open_fixture(web)
        web.bot.paper.sell(Quote('BTCUSDT',101,101.01,int(time.time()*1000)),'Fixture fermeture HTTP')
        rows=list(csv.DictReader(io.StringIO((await c.get('/api/export/trades.csv')).text.lstrip('\ufeff'))))
        assert len(rows)==1 and rows[0]['symbol']=='BTCUSDT' and float(rows[0]['fees'])>0
        assert rows[0]['exit_reason']=='Fixture fermeture HTTP'
        state=(await c.get('/api/state')).json()
        assert state['stats']['closed_trades']==1 and state['trades'][0]['detail']['fixture'] is True

@pytest.mark.asyncio
async def test_realistic_quote_latency_does_not_mark_fresh_reception_as_future(web):
    import asyncio
    original=web.market.quote
    async def delayed_quote(symbol):
        await asyncio.sleep(.02)
        return await original(symbol)
    web.market.quote=delayed_quote
    await web.bot.tick()
    assert web.bot.health['market_ok'] is True
    assert len(web.bot.quotes)==3


def add_open_fixture(web, symbol='ETHUSDT', kind='spot'):
    """A second tiny holding for HTTP guards; not a strategy/backtest result."""
    first=dict(web.store.rows('SELECT * FROM trades WHERE closed_at IS NULL ORDER BY id LIMIT 1')[0])
    first.pop('id')
    first.update(symbol=symbol,signal='fixture-http-'+symbol,quantity=.1,risk_usdt=.1,
                 entry_fee=.01,fees=.01,kind=kind)
    if kind=='cfd':
        first.update(currency='USD',margin=5.,leverage=2.,direction='LONG',fx_open=1.)
    columns=list(first)
    web.store.conn.execute('INSERT INTO trades('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(first.values()))
    cost=(5. if kind=='cfd' else first['quantity']*first['entry'])+first['entry_fee']
    web.store.set('cash',web.store.get('cash')-cost)
    web.bot.quotes[symbol]=Quote(symbol,100.,100.01,int(time.time()*1000),currency='USD' if kind=='cfd' else 'USDT')
    return web.store.position(symbol)


@pytest.mark.asyncio
async def test_state_exposes_all_open_positions_and_compatibility_first(web):
    first=open_fixture(web);second=add_open_fixture(web)
    web.bot.quotes['BTCUSDT']=Quote('BTCUSDT',100.,100.01,int(time.time()*1000))
    async with client(web) as c:
        response=await c.get('/api/state');assert response.status_code==200
        data=response.json()
        assert [p['id'] for p in data['positions']]==[first['id'],second['id']]
        assert data['position']['id']==first['id']
        assert len(data['stats']['positions'])==data['stats']['open_positions_count']==2
        assert all('unrealized_pnl' in p and 'current_price' in p for p in data['positions'])
        assert data['stats']['portfolio_risk_known'] is True
        assert data['stats']['portfolio_risk_used'] is not None


@pytest.mark.asyncio
async def test_settings_cannot_remove_second_open_spot_market(web):
    open_fixture(web);second=add_open_fixture(web)
    async with client(web) as c:
        headers=await token(c);settings=(await c.get('/api/state')).json()['settings']
        settings.update(symbols=['BTCUSDT'],auto_scanner=False)
        r=await c.put('/api/settings',json=settings,headers=headers)
        assert r.status_code==409
        assert web.store.position('ETHUSDT')['id']==second['id']


@pytest.mark.asyncio
async def test_settings_rechecks_all_holdings_after_rules_download(web,monkeypatch):
    open_fixture(web)
    original=web.market.rules
    async def rules(symbols):
        add_open_fixture(web)
        return await original(symbols)
    monkeypatch.setattr(web.market,'rules',rules)
    async with client(web) as c:
        headers=await token(c);settings=(await c.get('/api/state')).json()['settings']
        settings.update(symbols=['BTCUSDT'],auto_scanner=False)
        assert (await c.put('/api/settings',json=settings,headers=headers)).status_code==409
        assert len(web.store.positions())==2


@pytest.mark.asyncio
async def test_capital_replacement_blocked_when_cfd_is_not_first_position(web,monkeypatch):
    open_fixture(web);add_open_fixture(web,'CAP:FIXTURE','cfd')
    calls=[]
    async def setup(body):calls.append(body)
    monkeypatch.setattr(web.market,'setup_capital',setup,raising=False)
    async with client(web) as c:
        headers=await token(c)
        r=await c.post('/api/providers/capital/setup',json={'api_key':'fixture','identifier':'fixture','api_password':'fixture'},headers=headers)
        assert r.status_code==409 and calls==[]
        assert web.store.position()['symbol']=='BTCUSDT'
        assert len(web.store.positions())==2


@pytest.mark.asyncio
async def test_reset_archives_every_open_holding_before_clearing(web):
    first=open_fixture(web);second=add_open_fixture(web)
    async with client(web) as c:
        headers=await token(c)
        assert (await c.post('/api/reset',json={'confirmation':'incorrect'},headers=headers)).status_code==400
        assert len(web.store.positions())==2
        r=await c.post('/api/reset',json={'confirmation':'REINITIALISER'},headers=headers)
        assert r.status_code==200 and web.store.positions()==[]
        with sqlite3.connect(web.env.data_dir/'backups'/r.json()['backup']) as archive:
            assert [row[0] for row in archive.execute('SELECT id FROM trades WHERE closed_at IS NULL ORDER BY id')]==[first['id'],second['id']]
