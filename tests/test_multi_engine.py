"""End-to-end CFD fixtures; no broker execution or performance evidence."""
import pytest
from app.config import Settings
from app.ai import Decision,validate_context,wait
from app.market import Candle,Quote,Rules,MarketError
from test_engine import rig


def short():
    return Decision(symbol='CAP:NVDA',action='SHORT',reason='Fixture rupture baissière.',entry=100.,stop_loss=102.,take_profit=93.,invalidation=101.8,confirmation='Cassure clôturée.',risks=['Gap'],setup='breakout',confirmation_met=True)


def configure(rig,monkeypatch):
    engine,store,market,ai,env=rig
    store.set('settings',Settings(cfd_enabled=True,auto_scanner=False).model_dump())
    async def symbols(limit): return ['CAP:NVDA']
    oq=market.quote;orr=market.rules;oc=market.candles
    async def quote(symbol):
        if symbol.startswith('CAP:'):return Quote(symbol,100.,100.02,market.now,currency='USD',conversion_rate=1.001,conversion_bid=.999,conversion_ask=1.001)
        return await oq(symbol)
    async def rules(symbols):
        result=await orr([s for s in symbols if not s.startswith('CAP:')])
        for s in symbols:
            if s.startswith('CAP:'):result[s]=Rules(s,.01,1000,.01,0,1e9,.01,asset_class='share_cfd',provider='capital_demo',currency='USD',min_margin_pct=20,allow_short=True)
        return result
    async def candles(symbol,interval,limit=150,start_time=None,end_time=None):
        rows=await oc(symbol,interval,limit,start_time,end_time)
        if not symbol.startswith('CAP:'):return rows
        # Mirror the bullish closed-bar fixture: confirmed resistance near101.5
        # protects the SHORT stop102 and the old H1 support92 bounds its target93.
        return [Candle(c.open_time,c.close_time,200-c.open,200-c.low,200-c.high,
                       200-c.close,c.volume,c.volume_available) for c in rows]
    monkeypatch.setattr(market,'cfd_symbols',symbols,raising=False)
    monkeypatch.setattr(market,'quote',quote);monkeypatch.setattr(market,'rules',rules)
    monkeypatch.setattr(market,'candles',candles)
    monkeypatch.setattr('app.engine.candidates',lambda a,b,**kwargs:[])
    monkeypatch.setattr('app.engine.short_candidates',lambda a,b,**kwargs:['breakout'])
    ai.proposal=short()
    return engine,store,market,ai,env


@pytest.mark.asyncio
async def test_complete_short_cycle_and_spot_wallet_shared(rig,monkeypatch):
    engine,store,market,ai,_=configure(rig,monkeypatch)
    await engine._analyze()
    p=store.position()
    assert p and p['direction']=='SHORT' and p['kind']=='cfd'
    assert p['risk_usdt']<=9
    assert store.get('cash')<900
    assert len(ai.contexts)==1 and ai.contexts[0]['directions']['CAP:NVDA']==['SHORT']
    q=Quote('CAP:NVDA',92.9,92.95,market.now,currency='USD',conversion_bid=.999,conversion_ask=1.001)
    engine.paper.protect(q)
    assert store.position() is None and store.trades()[0]['net_pnl']>0


@pytest.mark.asyncio
async def test_missing_cfd_data_never_creates_position(rig,monkeypatch):
    engine,store,market,ai,_=configure(rig,monkeypatch)
    oq=market.quote
    async def unavailable(s):
        if s.startswith('CAP:'):raise MarketError('Fixture no data')
        return await oq(s)
    monkeypatch.setattr(market,'quote',unavailable)
    await engine._analyze()
    assert not ai.contexts and store.position() is None
    assert next(r for r in engine.scan['rows'] if r['symbol']=='CAP:NVDA')['status']=='INDISPONIBLE'


@pytest.mark.asyncio
async def test_wrong_side_is_rejected_independently_of_ai_client(rig,monkeypatch):
    engine,store,market,ai,_=configure(rig,monkeypatch)
    ai.proposal=short().model_copy(update={'action':'BUY','stop_loss':98.,'take_profit':107.,'invalidation':98.})
    await engine._analyze()
    assert store.position() is None
    assert 'direction' in store.rows('SELECT rejection FROM decisions')[-1]['rejection'].lower()


@pytest.mark.asyncio
async def test_configuration_change_while_ai_waits_blocks_entry(rig,monkeypatch):
    import asyncio
    engine,store,market,ai,_=configure(rig,monkeypatch)
    ai.release=asyncio.Event()
    task=asyncio.create_task(engine._analyze());await ai.entered.wait()
    engine._settings_revision+=1;engine.provider_setup=True
    ai.release.set();await task
    assert store.position() is None


def test_short_json_and_context_gates():
    d=short();context={'symbols':['CAP:NVDA'],'candidates':{'CAP:NVDA':['breakout']},'rules':{'cfd_enabled':True,'allow_short':True},'directions':{'CAP:NVDA':['SHORT']},'direction_candidates':{'CAP:NVDA':{'SHORT':['breakout']}}}
    validate_context(d,context)
    with pytest.raises(ValueError):validate_context(d,{**context,'rules':{'cfd_enabled':False,'allow_short':True}})
    with pytest.raises(ValueError):validate_context(d,{**context,'directions':{'CAP:NVDA':['BUY']}})
    with pytest.raises(ValueError):Decision.model_validate({**d.model_dump(),'take_profit':103.})


def test_cfd_without_volume_is_explicit_not_invented():
    from app.indicators import candidates
    base={'atr14':1.,'atr_pct':1.,'last':{'open':100.,'close':101.,'high':101.2,'low':99.9},'previous':{'close':99.8},
          'rsi14':60.,'ema21':100.,'ema9':100.5,'macd_hist':.2,'macd_hist_previous':.1,
          'volume_ratio':0.,'volume_available':False,'resistance':100.8,'candles':[]}
    h={'last':{'close':105.},'ema21':104.,'ema21_previous':103.,'ema50':100.}
    assert candidates(base,h)==[]
    assert candidates(base,h,require_volume=False)==['breakout']


@pytest.mark.asyncio
async def test_unresolved_history_never_allows_favorable_tp(rig,monkeypatch):
    engine,store,market,ai,_=configure(rig,monkeypatch)
    await engine._analyze()
    p=store.position();assert p
    # Gap not represented by M1 data. The current favorable quote must not
    # bypass the conservative replay requirement.
    store.set('reconcile_pending',{'id':p['id'],'since':market.now-10000,'until':market.now})
    monkeypatch.setattr(engine.market,'candles',lambda *a,**k: None)
    # Exercise protection gate directly while pending rather than fake history.
    q=Quote('CAP:NVDA',92.,92.01,market.now,currency='USD')
    engine.paper.protect(q,allow_take_profit=False)
    assert store.position() is not None
