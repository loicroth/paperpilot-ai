"""Fixture checks, not live trades or profitability evidence."""
import pytest
from app.market import BinanceClient, MarketError
from app.config import Settings
from test_engine import rig

@pytest.mark.asyncio
async def test_universe_filters(monkeypatch):
    client=BinanceClient(); now=1800000000000
    async def clock(): return now
    rows=[]; tickers=[]
    for base,volume,status,age in [('BTC',9e6,'TRADING',0),('ETH',8e6,'TRADING',0),('USDC',1e9,'TRADING',0),('BAD',8e9,'BREAK',0),('OLD',9e8,'TRADING',130000),('TINY',100,'TRADING',0),('BTCUP',9e7,'TRADING',0),('SNDKB',9e8,'TRADING',0),('RLUSD',9e8,'TRADING',0)]:
        rows.append(dict(symbol=base+'USDT',baseAsset=base,quoteAsset='USDT',status=status,isSpotTradingAllowed=True))
        tickers.append(dict(symbol=base+'USDT',quoteVolume=str(volume),closeTime=now-age))
    async def get(endpoint,params=None): return {'symbols':rows} if endpoint=='exchangeInfo' else tickers
    monkeypatch.setattr(client,'server_time',clock);monkeypatch.setattr(client,'_get',get)
    assert await client.liquid_symbols()==['BTCUSDT','ETHUSDT']
    await client.close()

def enable(rig,monkeypatch):
    engine,store,market,ai,env=rig
    store.set('settings',Settings(auto_scanner=True,ai_shortlist=2).model_dump())
    async def universe(limit): return ['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT']
    monkeypatch.setattr(market,'liquid_symbols',universe,raising=False)
    return rig

@pytest.mark.asyncio
async def test_shortlist_and_paper_buy_and_dedup(rig,monkeypatch):
    engine,store,market,ai,_=enable(rig,monkeypatch)
    monkeypatch.setattr('app.engine.candidates',lambda a,b:['breakout'])
    await engine._analyze()
    assert len(engine.scan['rows'])==4
    assert len(ai.contexts)==1
    assert ai.contexts[0]['symbols']==['BTCUSDT','ETHUSDT']
    assert store.position()['symbol']=='BTCUSDT'
    await engine._analyze()
    assert len(ai.contexts)==1

@pytest.mark.asyncio
async def test_no_candidates_no_ai_call(rig,monkeypatch):
    engine,store,_,ai,_=enable(rig,monkeypatch)
    monkeypatch.setattr('app.engine.candidates',lambda a,b:[])
    await engine._analyze()
    assert not ai.contexts and store.position() is None
    assert store.rows('SELECT action FROM decisions')[0]['action']=='WAIT'
    assert len(engine.scan['rows'])==4

@pytest.mark.asyncio
async def test_unavailable_market_excluded(rig,monkeypatch):
    engine,store,market,ai,_=enable(rig,monkeypatch)
    monkeypatch.setattr('app.engine.candidates',lambda a,b:['breakout'])
    original=market.candles
    async def candles(symbol,*args,**kwargs):
        if symbol=='SOLUSDT': raise MarketError('fixture unavailable')
        return await original(symbol,*args,**kwargs)
    monkeypatch.setattr(market,'candles',candles)
    await engine._analyze()
    assert len(ai.contexts)==1 and 'SOLUSDT' not in ai.contexts[0]['symbols']
    assert engine.scan['rows'][2]['status']=='INDISPONIBLE'

@pytest.mark.asyncio
async def test_discovery_failure_blocks(rig,monkeypatch):
    engine,store,market,ai,_=enable(rig,monkeypatch)
    async def failed(limit): raise MarketError('discovery unavailable')
    monkeypatch.setattr(market,'liquid_symbols',failed)
    await engine._analyze()
    assert not ai.contexts and store.position() is None
    assert engine.scan['error']=='discovery unavailable'

@pytest.mark.asyncio
async def test_held_position_always_analyzed(rig,monkeypatch):
    engine,store,market,ai,_=enable(rig,monkeypatch)
    monkeypatch.setattr('app.engine.candidates',lambda a,b:['breakout'])
    await engine._analyze()
    assert store.position()
    async def universe(limit): return ['ETHUSDT','SOLUSDT','XRPUSDT']
    monkeypatch.setattr(market,'liquid_symbols',universe)
    monkeypatch.setattr('app.engine.candidates',lambda a,b:[])
    market.now+=300000
    await engine._analyze()
    assert len(ai.contexts)==2 and ai.contexts[-1]['symbols']==['BTCUSDT']
    assert store.position()['symbol']=='BTCUSDT'

def test_config_persistence_bounds(tmp_path):
    from app.storage import Store
    path=tmp_path/'scanner.sqlite3';db=Store(path)
    db.set('settings',Settings(auto_scanner=True,scanner_limit=40).model_dump());db.close()
    db=Store(path);assert Settings.model_validate(db.get('settings')).auto_scanner;db.close()
    with pytest.raises(ValueError): Settings(scanner_limit=41)
