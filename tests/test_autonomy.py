import pytest
from types import SimpleNamespace
from app.storage import Store
from app.engine import TradingEngine
from test_engine import MarketFixture,AIFixture,rig


def engine(store):
    return TradingEngine(store,SimpleNamespace(api_key='',model='fixture'),market=MarketFixture(1800000000000),ai=AIFixture())

@pytest.mark.parametrize('mode',['running','paused','stopped'])
def test_user_run_intent_survives_restart(tmp_path,mode):
    db=Store(tmp_path/'test.sqlite3');db.set('mode',mode)
    bot=engine(db)
    assert db.get('mode')==mode
    assert bot.started_ms>0 and bot.health['market_ok'] is False
    db.close()


def test_one_time_explicit_start_never_overrides_later_manual_stop(tmp_path,monkeypatch):
    db=Store(tmp_path/'test.sqlite3');monkeypatch.setenv('START_PAPER_ONCE','request-fixture')
    engine(db);assert db.get('mode')=='running'
    db.set('mode','stopped');engine(db)
    assert db.get('mode')=='stopped'
    db.close()


def test_diagnostic_has_no_api_secret(tmp_path):
    db=Store(tmp_path/'test.sqlite3');bot=engine(db);bot.env.api_key='not-a-real-key-SECRET'
    state=bot.diagnostic()
    assert state['ai_configured'] is True and 'SECRET' not in str(state)
    db.close()

@pytest.mark.asyncio
async def test_slow_symbol_does_not_block_ready_candidates(rig,monkeypatch):
    import asyncio
    from app.config import Settings
    bot,db,market,ai,_=rig
    db.set('settings',Settings(auto_scanner=True).model_dump())
    async def symbols(limit):return ['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT']
    original_quote=market.quote
    async def quote(s):
        if s=='XRPUSDT':await asyncio.sleep(.3)
        return await original_quote(s)
    original_wait=asyncio.wait
    async def bounded(tasks,**kwargs):return await original_wait(tasks,timeout=.03)
    monkeypatch.setattr(market,'liquid_symbols',symbols,raising=False)
    monkeypatch.setattr(market,'quote',quote)
    monkeypatch.setattr('app.engine.asyncio.wait',bounded)
    monkeypatch.setattr('app.engine.candidates',lambda a,b:['breakout'])
    await bot._analyze()
    assert db.position()['symbol']=='BTCUSDT'
    assert next(r for r in bot.scan['rows'] if r['symbol']=='XRPUSDT')['status']=='INDISPONIBLE'
    assert len(ai.contexts)==1

@pytest.mark.asyncio
async def test_families_get_shortlist_place_despite_incomparable_volumes(rig,monkeypatch):
    from test_multi_engine import configure
    bot,db,market,ai,_=configure(rig,monkeypatch)
    cfg=bot.settings().model_copy(update={'ai_shortlist':2});db.set('settings',cfg.model_dump())
    monkeypatch.setattr('app.engine.candidates',lambda a,b,**kw:['breakout'])
    monkeypatch.setattr(market,'describe',lambda s:{'asset_class':'share_cfd' if s.startswith('CAP:') else 'crypto','provider':'fixture'},raising=False)
    await bot._analyze()
    assert 'CAP:NVDA' in bot.scan['selected']
    assert len(bot.scan['selected'])==2

@pytest.mark.asyncio
async def test_startup_ai_check_is_budgeted_and_never_a_trade(rig):
    from app.ai import wait
    bot,db,market,ai,_=rig
    bot.env.api_key='fixture-test-not-secret';ai.proposal=wait('Aucun marché fourni pour ce contrôle de connexion.')
    await bot.verify_ai()
    assert bot.health['ai_ok'] is True
    assert db.position() is None and not db.rows('SELECT * FROM decisions')
    assert db.rows('SELECT calls FROM usage')[0]['calls']==1
    assert ai.contexts[0]['diagnostic_only'] is True

@pytest.mark.asyncio
async def test_failed_startup_ai_check_never_opens_position(rig):
    bot,db,market,ai,_=rig
    ai.error='Quota fixture indisponible'
    await bot.verify_ai()
    assert bot.health['ai_ok'] is False and 'Quota' in bot.health['ai_error']
    assert db.position() is None and not db.rows('SELECT * FROM decisions')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['running','paused','stopped'])
async def test_rejected_response_keeps_run_intent_without_claiming_ai_is_valid(rig,mode):
    bot,db,market,ai,_=rig
    db.set('mode',mode)
    bot.health.update(ai_ok=False,ai_error='Niveaux BUY contradictoires.',ai_error_kind='invalid_decision')
    await bot.tick()
    assert db.get('mode')==mode and bot.health['ai_ok'] is False
    assert bot.health['status']==('ACTIVE · RÉPONSE IA REJETÉE' if mode=='running' else 'PAUSE')
    assert 'Niveaux BUY' in bot.health['message']
    assert db.position() is None


@pytest.mark.asyncio
async def test_authentication_failure_remains_an_error(rig):
    bot,db,market,ai,_=rig
    bot.health.update(ai_ok=False,ai_error='Authentification refusée.',ai_error_kind='authentication')
    await bot.tick()
    assert bot.health['status']=='ERREUR' and bot.health['ai_ok'] is False
    assert db.position() is None


@pytest.mark.asyncio
async def test_invalid_cycle_is_logged_without_trade_and_next_valid_cycle_can_execute(rig,monkeypatch):
    from app.ai import AIResult,wait
    from test_engine import buy_decision
    bot,db,market,ai,_=rig
    monkeypatch.setattr('app.engine.candidates',lambda *args,**kwargs:['breakout'])
    monkeypatch.setattr('app.engine.time.time',lambda:market.now/1000)
    diagnostic={'stage':'decision_schema','code':'invalid_levels','reason':'Niveaux BUY contradictoires'}
    async def rejected(context):
        return AIResult(wait('Réponse rejetée'), 'Niveaux BUY contradictoires',400,100,
            error_kind='invalid_decision',diagnostic=diagnostic)
    monkeypatch.setattr(ai,'decide',rejected)
    await bot._analyze()
    assert db.position() is None and db.get('cash')==900
    row=db.rows('SELECT * FROM decisions')[-1]
    import json
    assert json.loads(row['detail'])['ai_diagnostic']==diagnostic
    assert row['action']=='WAIT' and row['rejection']
    assert bot.health['ai_error_kind']=='invalid_decision' and bot.health['ai_ok'] is False
    async def valid(context):
        return AIResult(buy_decision(),None,400,100)
    monkeypatch.setattr(ai,'decide',valid)
    market.now+=300000
    await bot._analyze()
    assert db.position() is not None
    assert bot.health['ai_error'] is None and bot.health['ai_error_kind'] is None
    assert bot.health['ai_ok'] is True
