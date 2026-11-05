"""CFD-only scope tests use synthetic prices; no broker orders or network I/O."""
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Environment, Settings
from app.engine import TradingEngine
from app.execution import PaperExecutionEngine
from app.market import MarketError, Quote, Rules
from app.multimarket import MultiMarket
from app.risk import RiskEngine, RiskRejected
from app.storage import Store
from test_engine import AIFixture, MarketFixture, buy_decision, rig
from test_execution_risk_storage import make_plan, decision as spot_decision, quote as spot_quote
from test_multi_engine import configure


def test_application_environment_cannot_enable_crypto(monkeypatch, tmp_path):
    monkeypatch.setenv('CFD_ONLY', 'false')
    monkeypatch.setenv('AUTO_SCANNER', 'true')
    monkeypatch.setenv('HOST', '127.0.0.1')
    monkeypatch.setenv('PORT', '8000')
    monkeypatch.setenv('PUBLIC_ORIGIN', '')
    monkeypatch.setenv('BINANCE_BASE_URL', 'https://data-api.binance.vision')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    assert Environment().cfd_only is True
    cfg=Settings(cfd_only=True,cfd_enabled=False,auto_scanner=True,symbols=['BTCUSDT'])
    assert cfg.symbols==[] and cfg.cfd_enabled and not cfg.auto_scanner
    with pytest.raises(ValidationError):Settings(symbols=[])


def test_startup_retires_crypto_before_history_and_preserves_settings(tmp_path, monkeypatch, caplog):
    store=Store(tmp_path/'startup.sqlite3')
    store.set('management_v2_migrated', True)
    cfg=Settings(strategy_profile='classic',risk_pct=.6,max_positions=4)
    store.set('settings',cfg.model_dump());store.set('mode','paused')
    seen=[]
    def history(engine):
        seen.append(engine.store.get('crypto_retirement_v1'))
        assert engine.settings().cfd_only
    monkeypatch.setattr(TradingEngine,'history_diagnostic',history)
    caplog.set_level('INFO',logger='paperpilot')
    env=SimpleNamespace(cfd_only=True,api_key='',model='gpt-4o-mini')
    try:
        engine=TradingEngine(store,env,market=MarketFixture(int(time.time()*1000)),ai=AIFixture())
        saved=store.get('settings')
        assert seen and seen[0]['version']==1
        assert saved['cfd_only'] and saved['cfd_enabled'] and saved['symbols']==[] and not saved['auto_scanner']
        assert saved['strategy_profile']=='classic' and saved['risk_pct']==.6 and saved['max_positions']==4
        assert store.get('mode')=='paused' and 'PAPER_CRYPTO_RETIREMENT' in caplog.text
        # Editing the persisted JSON cannot re-enable crypto in this app.
        store.set('settings',cfg.model_dump())
        assert engine.settings().cfd_only and engine.settings().symbols==[]
    finally:store.close()


def test_failed_retirement_aborts_startup(tmp_path, monkeypatch):
    store=Store(tmp_path/'abort.sqlite3')
    def failed():raise ValueError('Sauvegarde indisponible')
    monkeypatch.setattr(store,'retire_crypto',failed)
    try:
        with pytest.raises(ValueError,match='Sauvegarde'):
            TradingEngine(store,SimpleNamespace(cfd_only=True),market=object(),ai=object())
    finally:store.close()


class NoBinance:
    """Any market-data call here is a regression, including rules([])."""
    async def close(self):pass
    def __getattr__(self,name):raise AssertionError('Binance called: '+name)


class CapitalData:
    configured=True
    def __init__(self):self.calls=[]
    async def server_time(self):self.calls.append(('clock',));return int(time.time()*1000)
    async def catalog(self,limit):
        self.calls.append(('catalog',limit))
        return [dict(symbol=s,asset_class=a,supported=True,market_status='TRADEABLE') for s,a in
            [('CAP:NVDA','share_cfd'),('CAP:COIN','share_cfd'),('CAP:BTCUSD','crypto_cfd'),('BTCUSDT','crypto')]]
    async def quote(self,symbol):
        self.calls.append(('quote',symbol))
        return Quote(symbol,100.,100.02,int(time.time()*1000),currency='USD')
    async def rules(self,symbols):
        self.calls.append(('rules',list(symbols)))
        return {s:Rules(s,.01,1000.,.01,0.,1e9,.01,asset_class='share_cfd',currency='USD') for s in symbols}
    async def candles(self,symbol,*args,**kwargs):self.calls.append(('candles',symbol));return []
    async def close(self):pass


class DollarFX:
    cached=None;error=None
    async def rates(self):
        self.cached={'bid':.999,'ask':1.001,'at':int(time.time()*1000)}
        return self.cached
    async def close(self):pass


@pytest.mark.asyncio
async def test_cfd_router_never_uses_binance_and_keeps_accounting_fx(tmp_path):
    capital=CapitalData();fx=DollarFX()
    market=MultiMarket(SimpleNamespace(cfd_only=True,data_dir=tmp_path),binance=NoBinance(),capital=capital,fx=fx)
    try:
        assert await market.server_time()>0
        assert await market.liquid_symbols(30)==[]
        assert await market.cfd_symbols(20)==['CAP:NVDA','CAP:COIN']
        assert await market.rules([])=={}
        assert list(await market.rules(['CAP:NVDA']))==['CAP:NVDA']
        q=await market.quote('CAP:NVDA')
        assert q.conversion_bid==.999 and q.conversion_ask==1.001 and q.currency=='USD'
        assert await market.candles('CAP:NVDA','1h')==[]
        before=list(capital.calls)
        for request in (market.quote('BTCUSDT'),market.candles('BTCUSDT','5m'),market.rules(['BTCUSDT'])):
            with pytest.raises(MarketError,match='CFD uniquement'):await request
        assert capital.calls==before
    finally:await market.close()


@pytest.mark.asyncio
async def test_missing_capital_has_no_binance_clock_fallback(tmp_path):
    capital=CapitalData();capital.configured=False
    market=MultiMarket(SimpleNamespace(cfd_only=True,data_dir=tmp_path),binance=NoBinance(),capital=capital,fx=DollarFX())
    try:
        with pytest.raises(MarketError,match='non connecté'):await market.server_time()
        assert capital.calls==[]
    finally:await market.close()


def test_spot_cannot_bypass_risk_or_execution(tmp_path):
    store=Store(tmp_path/'blocked.sqlite3')
    legacy=Settings();cfg=Settings(cfd_only=True)
    rule=Rules('BTCUSDT',.001,100000.,.001,5.,1e6,.01)
    plan=make_plan(legacy,rule)
    try:
        with pytest.raises(RiskRejected,match='CFD uniquement'):
            RiskEngine.plan(spot_decision(),spot_quote(),rule,cfg,900.,900.,atr=.8)
        for bypass in (plan,SimpleNamespace(kind='cfd',symbol='BTCUSDT'),SimpleNamespace(kind='spot',symbol='CAP:NVDA')):
            with pytest.raises(ValueError,match='CFD uniquement'):
                PaperExecutionEngine(store).buy(bypass,spot_quote(),'blocked',spot_decision(),cfg,{})
        assert not store.positions() and not store.trades() and store.get('cash')==900.
    finally:store.close()


def test_crypto_cfd_class_cannot_bypass_risk():
    d=buy_decision().model_copy(update={'symbol':'CAP:BTCUSD'})
    q=Quote(d.symbol,100.,100.02,int(time.time()*1000),currency='USD')
    rule=Rules(d.symbol,.01,1000.,.01,0.,1e9,.01,asset_class='crypto_cfd',currency='USD')
    with pytest.raises(RiskRejected,match='hors cryptomonnaies'):
        RiskEngine.plan(d,q,rule,Settings(cfd_only=True).model_copy(update={'symbols':[d.symbol]}),900.,900.,atr=1.)


@pytest.mark.asyncio
@pytest.mark.parametrize('short',[False,True])
async def test_cfd_only_tick_and_scan_still_open_long_and_short(rig,monkeypatch,short):
    engine,store,market,ai,env=configure(rig,monkeypatch)
    env.cfd_only=True
    market.capital=SimpleNamespace(configured=True)
    engine._universe=['BTCUSDT'];engine._h1['BTCUSDT']=[]
    requests=[]
    oq,oc,orr=market.quote,market.candles,market.rules
    async def quote(symbol):
        requests.append(('quote',symbol));assert symbol.startswith('CAP:')
        return await oq(symbol)
    async def candles(symbol,*args,**kwargs):
        requests.append(('candles',symbol));assert symbol.startswith('CAP:')
        return await (oc(symbol,*args,**kwargs) if short else MarketFixture.candles(market,symbol,*args,**kwargs))
    async def rules(symbols):
        assert all(s.startswith('CAP:') for s in symbols)
        return await orr(symbols)
    async def cfd_symbols(limit):return ['BTCUSDT','CAP:NVDA']
    async def forbidden_scanner(limit):raise AssertionError('Crypto scanner called')
    monkeypatch.setattr(market,'quote',quote);monkeypatch.setattr(market,'candles',candles)
    monkeypatch.setattr(market,'rules',rules);monkeypatch.setattr(market,'cfd_symbols',cfd_symbols)
    monkeypatch.setattr(market,'liquid_symbols',forbidden_scanner,raising=False)
    if not short:
        monkeypatch.setattr('app.engine.candidates',lambda a,b,**kw:['breakout'])
        monkeypatch.setattr('app.engine.short_candidates',lambda a,b,**kw:[])
        ai.proposal=buy_decision().model_copy(update={'symbol':'CAP:NVDA'})
    await engine.tick()
    assert engine.health['market_ok'] and not store.get('btc_baseline')
    await engine._analyze()
    p=store.position();assert p and p['symbol']=='CAP:NVDA' and p['kind']=='cfd'
    assert p['direction']==('SHORT' if short else 'LONG')
    assert requests and all(s.startswith('CAP:') for _,s in requests)
    assert list(engine._h1)==['CAP:NVDA'] and engine._universe==[]
    assert all(s.startswith('CAP:') for s in engine.quotes)
    assert all(s.startswith('CAP:') for c in ai.contexts for s in c['symbols'])
    assert all(row['symbol'].startswith('CAP:') for row in engine.scan['rows'])


@pytest.mark.asyncio
async def test_engine_rejects_malicious_spot_entry_before_market_io(rig,monkeypatch):
    engine,store,market,ai,env=rig
    env.cfd_only=True
    async def forbidden(*args,**kwargs):raise AssertionError('Spot data requested')
    monkeypatch.setattr(market,'quote',forbidden);monkeypatch.setattr(market,'rules',forbidden)
    monkeypatch.setattr('app.engine.validate_context',lambda *args:None)
    expected=market.now-5001
    # A corrupt context and a legacy cfg must not bypass application intent.
    context={'mode':'entry','symbols':['BTCUSDT'],'markets':{'BTCUSDT':{}},'positions':[],'rules':{}}
    await engine._decide_and_execute(context,{},Settings(),engine._settings_revision,expected,'malicious','2026-11-05')
    assert not store.positions() and store.get('cash')==900.
    assert 'CFD uniquement' in store.rows('SELECT rejection FROM decisions')[-1]['rejection']
