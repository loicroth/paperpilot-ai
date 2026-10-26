"""Independent portfolio invariants on fixtures, never live trades/performance."""
import asyncio
from dataclasses import replace
import pytest
from app.ai import AIResult, Decision, wait
from app.market import MarketError, Quote
from app.risk import OrderPlan
from test_engine import rig, buy_decision


def seed_positions(rig, symbols=('BTCUSDT','ETHUSDT','SOLUSDT')):
    engine,store,market,_,_=rig
    cfg=engine.settings()
    for symbol in symbols:
        decision=buy_decision().model_copy(update={'symbol':symbol})
        q=Quote(symbol,100.,100.02,market.now)
        plan=OrderPlan(symbol=symbol,quantity=.5,entry=100.07,stop_loss=98.,take_profit=107.,
                       entry_fee=.050035,risk_usdt=1.2,rr=2.5)
        engine.paper.buy(plan,q,'fixture:'+symbol,decision,cfg,{'fixture':True})
        engine.quotes[symbol]=q
    return engine,store,market


@pytest.mark.asyncio
async def test_one_tick_protects_every_open_position(rig):
    engine,store,market=seed_positions(rig)
    market.price=97.
    await engine.tick()
    assert store.positions()==[]
    assert len(store.trades())==3
    assert {row['symbol'] for row in store.trades()}=={'BTCUSDT','ETHUSDT','SOLUSDT'}
    assert all(row['exit_reason']=='STOP_LOSS' for row in store.trades())
    assert store.get('cash')<900


@pytest.mark.asyncio
async def test_one_missing_quote_does_not_skip_other_position_stops(rig,monkeypatch):
    engine,store,market=seed_positions(rig)
    async def quotes(symbol):
        if symbol=='ETHUSDT':raise MarketError('ETH fixture feed unavailable')
        return Quote(symbol,97.,97.02,market.now)
    monkeypatch.setattr(market,'quote',quotes)
    await engine.tick()
    assert [row['symbol'] for row in store.positions()]==['ETHUSDT']
    assert {row['symbol'] for row in store.trades()}=={'BTCUSDT','SOLUSDT'}
    assert not engine.health['market_ok']


@pytest.mark.asyncio
async def test_incomplete_history_for_one_symbol_does_not_disable_other_protection(rig,monkeypatch):
    engine,store,market=seed_positions(rig)
    store.conn.execute('UPDATE trades SET last_market_at=? WHERE symbol=?',(market.now-20000,'ETHUSDT'))
    original=market.candles
    async def candles(symbol,*args,**kwargs):
        if symbol=='ETHUSDT':raise MarketError('ETH fixture M1 history unavailable')
        return await original(symbol,*args,**kwargs)
    async def quotes(symbol):
        price=100. if symbol=='ETHUSDT' else 108.
        return Quote(symbol,price,price+.02,market.now)
    monkeypatch.setattr(market,'candles',candles)
    monkeypatch.setattr(market,'quote',quotes)
    await engine.tick()
    assert [row['symbol'] for row in store.positions()]==['ETHUSDT']
    assert {row['symbol'] for row in store.trades()}=={'BTCUSDT','SOLUSDT'}
    assert all(row['exit_reason']=='TAKE_PROFIT' for row in store.trades())
    assert not engine.reconciled


@pytest.mark.asyncio
async def test_held_symbol_is_protected_after_removal_from_manual_watchlist(rig):
    engine,store,market=seed_positions(rig,('XRPUSDT','SOLUSDT'))
    market.price=97.
    await engine.tick()
    assert not store.positions()
    assert {row['symbol'] for row in store.trades()}=={'XRPUSDT','SOLUSDT'}


def test_multiple_position_roundtrip_and_duplicate_symbol_are_atomic(rig):
    engine,store,market=seed_positions(rig,('BTCUSDT','ETHUSDT'))
    before=store.get('cash')
    d=buy_decision();q=Quote('BTCUSDT',100.,100.02,market.now)
    p=OrderPlan('BTCUSDT',.5,100.07,98.,107.,.050035,1.2,2.5)
    with pytest.raises(ValueError):engine.paper.buy(p,q,'second-signal',d,engine.settings(),{})
    assert len(store.positions())==2 and store.get('cash')==before
    engine.paper.sell(Quote('ETHUSDT',101.,101.02,market.now),'FIXTURE_CLOSE')
    assert [x['symbol'] for x in store.positions()]==['BTCUSDT']
    assert store.trades()[0]['symbol']=='ETHUSDT'


def test_v2_open_position_migrates_without_losing_cash_or_trade(rig,tmp_path):
    from app.storage import Store
    engine,store,market=seed_positions(rig,('BTCUSDT',))
    old_cash=store.get('cash');old_id=store.positions()[0]['id']
    # Re-create the v2 exclusivity constraint on a populated database, then
    # re-open through the production migration instead of rebuilding the state.
    store.conn.execute('DROP INDEX IF EXISTS one_position_per_symbol')
    store.conn.execute('CREATE UNIQUE INDEX one_position ON trades((1)) WHERE closed_at IS NULL')
    store.conn.execute('PRAGMA user_version=2');store.close()
    migrated=Store(tmp_path/'engine.sqlite3')
    try:
        assert migrated.get('cash')==old_cash
        assert migrated.positions()[0]['id']==old_id
        assert migrated.conn.execute('PRAGMA user_version').fetchone()[0]>=3
        assert list((tmp_path/'backups').glob('avant_multi_positions_*.sqlite3'))
        engine.store=migrated;engine.paper.store=migrated
        cfg=engine.settings()
        for symbol in ('ETHUSDT','SOLUSDT'):
            decision=buy_decision().model_copy(update={'symbol':symbol})
            plan=OrderPlan(symbol,.5,100.07,98.,107.,.050035,1.2,2.5)
            engine.paper.buy(plan,Quote(symbol,100.,100.02,market.now),'after-migrate:'+symbol,decision,cfg,{})
        assert len(migrated.positions())==3
        with pytest.raises(ValueError):
            engine.paper.buy(OrderPlan('BTCUSDT',.5,100.07,98.,107.,.050035,1.2,2.5),
                Quote('BTCUSDT',100.,100.02,market.now),'duplicate-after-migration',buy_decision(),cfg,{})
    finally:migrated.close()


def install_entry_structure(rig,monkeypatch):
    """Closed-bar fixtures with confirmed support98.2 and resistance108.2."""
    engine,_,market,_,_=rig
    original=market.candles
    async def candles(symbol,*args,**kwargs):
        rows=await original(symbol,*args,**kwargs)
        rows=[replace(c,open=100.,close=100.,high=100.2,low=99.5,volume=100.) for c in rows]
        rows[-6]=replace(rows[-6],high=108.2)
        rows[-5]=replace(rows[-5],low=98.2)
        return rows
    monkeypatch.setattr(market,'candles',candles)
    monkeypatch.setattr('app.engine.candidates',lambda *a,**k:['breakout'])


class PortfolioAI:
    test_enabled=True
    model='fixture'
    def __init__(self):
        self.contexts=[];self.entry_entered=asyncio.Event();self.entry_release=None;self.close_managed=False
    async def close(self):pass
    async def decide(self,context):
        self.contexts.append(context)
        if context['mode']=='management':
            action='SELL' if self.close_managed else 'HOLD'
            return AIResult(Decision(symbol=context['managed_symbol'],action=action,reason='Gestion fixture.',
                entry=None,stop_loss=None,take_profit=None,invalidation=None,risk_pct=None,
                confirmation='Gestion position existante.',risks=[],setup='none',confirmation_met=False))
        self.entry_entered.set()
        if self.entry_release is not None:await self.entry_release.wait()
        symbol=context['symbols'][0]
        return AIResult(buy_decision().model_copy(update={'symbol':symbol}))


@pytest.mark.asyncio
async def test_three_full_slots_still_manage_one_position_per_m5_and_rotate_across_restart(rig,monkeypatch):
    from app.engine import TradingEngine
    engine,store,market=seed_positions(rig)
    ai=PortfolioAI();engine.ai=ai
    monkeypatch.setattr('app.engine.candidates',lambda *a,**k:[])
    expected=['BTCUSDT','ETHUSDT','SOLUSDT','BTCUSDT']
    for index,symbol in enumerate(expected):
        if index==2:
            engine=TradingEngine(store,rig[-1],market=market,ai=ai)
            engine.started_ms=market.now-10000
        engine.health['market_ok']=True
        await engine._analyze()
        await engine._analyze()
        assert len(ai.contexts)==index+1
        assert ai.contexts[-1]['mode']=='management'
        assert ai.contexts[-1]['managed_symbol']==symbol
        assert len(store.positions())==3
        market.now+=300000


@pytest.mark.asyncio
async def test_management_sell_closes_only_rotated_target_when_portfolio_full(rig,monkeypatch):
    engine,store,market=seed_positions(rig)
    ai=PortfolioAI();engine.ai=ai
    monkeypatch.setattr('app.engine.candidates',lambda *a,**k:[])
    store.set('last_managed_position_id',store.position('BTCUSDT')['id'])
    ai.close_managed=True
    await engine._analyze()
    assert ai.contexts[0]['managed_symbol']=='ETHUSDT'
    assert {p['symbol'] for p in store.positions()}=={'BTCUSDT','SOLUSDT'}
    assert store.trades()[0]['symbol']=='ETHUSDT'
    assert store.trades()[0]['exit_reason']=='SIGNAL_IA'


@pytest.mark.asyncio
async def test_concurrent_analysis_same_m5_opens_at_most_one_position(rig,monkeypatch):
    engine,store,market,_,_=rig
    install_entry_structure(rig,monkeypatch)
    ai=PortfolioAI();ai.entry_release=asyncio.Event();engine.ai=ai
    first=asyncio.create_task(engine._analyze())
    await ai.entry_entered.wait()
    second=asyncio.create_task(engine._analyze())
    await asyncio.sleep(0)
    ai.entry_release.set()
    await asyncio.gather(first,second)
    assert len([c for c in ai.contexts if c['mode']=='entry'])==1
    assert len(store.positions())==1
    assert len(store.rows('SELECT id FROM trades'))==1


@pytest.mark.asyncio
async def test_entry_rechecks_actual_slots_after_ai_response(rig,monkeypatch):
    engine,store,market=seed_positions(rig,('BTCUSDT',))
    install_entry_structure(rig,monkeypatch)
    ai=PortfolioAI();ai.entry_release=asyncio.Event();engine.ai=ai
    analysis=asyncio.create_task(engine._analyze())
    await ai.entry_entered.wait()
    # Another producer fills remaining slots after the model saw only one.
    seed_positions(rig,('SOLUSDT','XRPUSDT'))
    ai.entry_release.set();await analysis
    assert {p['symbol'] for p in store.positions()}=={'BTCUSDT','SOLUSDT','XRPUSDT'}
    rejected=store.rows("SELECT rejection FROM decisions WHERE action='BUY'")
    assert rejected and 'maximal' in rejected[-1]['rejection']


@pytest.mark.asyncio
async def test_entry_rechecks_duplicate_symbol_after_ai_response(rig,monkeypatch):
    engine,store,market,_,_=rig
    install_entry_structure(rig,monkeypatch)
    ai=PortfolioAI();ai.entry_release=asyncio.Event();engine.ai=ai
    analysis=asyncio.create_task(engine._analyze());await ai.entry_entered.wait()
    seed_positions(rig,('BTCUSDT',))
    paid_cash=store.get('cash')
    ai.entry_release.set();await analysis
    assert len(store.positions())==1 and store.get('cash')==paid_cash
    assert store.rows("SELECT rejection FROM decisions WHERE action='BUY'")[-1]['rejection']


@pytest.mark.asyncio
async def test_entry_recomputes_remaining_portfolio_risk_after_ai_response(rig,monkeypatch):
    from app.metrics import statistics
    from app.risk import portfolio_remaining_risk
    engine,store,market=seed_positions(rig,('BTCUSDT',))
    install_entry_structure(rig,monkeypatch)
    ai=PortfolioAI();ai.entry_release=asyncio.Event();engine.ai=ai
    analysis=asyncio.create_task(engine._analyze());await ai.entry_entered.wait()
    # Risk changes while the model is thinking, without exhausting the 3 slots.
    d=buy_decision().model_copy(update={'symbol':'SOLUSDT'})
    q=Quote('SOLUSDT',100.,100.02,market.now)
    engine.paper.buy(OrderPlan('SOLUSDT',5.,100.07,98.,107.,.50035,12.,2.5),q,
        'concurrent-risk',d,engine.settings(),{'fixture':True})
    engine.quotes['SOLUSDT']=q
    used=portfolio_remaining_risk(store.positions(),engine.quotes,market.now)
    stats,_=statistics(store,engine.quotes);day=engine.day_state(stats['equity'])
    allowed=min(stats['equity']*.02,max(0,day['start_equity']*.02+(stats['equity']-day['start_equity'])))
    ai.entry_release.set();await analysis
    entry=store.position('ETHUSDT')
    assert entry is not None
    assert len(store.positions())==3
    assert entry['risk_usdt']<stats['equity']*.01
    assert used+entry['risk_usdt']<=allowed+1e-8
    assert entry['stop_loss']==98. and entry['take_profit']==107.


@pytest.mark.asyncio
async def test_slow_entry_metadata_cannot_block_stop_protection_on_another_position(rig,monkeypatch):
    engine,store,market=seed_positions(rig,('BTCUSDT',))
    install_entry_structure(rig,monkeypatch)
    ai=PortfolioAI();engine.ai=ai
    original_rules=market.rules;metadata_entered=asyncio.Event();metadata_release=asyncio.Event()
    async def slow_entry_rules(symbols):
        if len(symbols)==1 and symbols[0]=='ETHUSDT' and ai.entry_entered.is_set():
            metadata_entered.set();await metadata_release.wait()
        return await original_rules(symbols)
    monkeypatch.setattr(market,'rules',slow_entry_rules)
    analysis=asyncio.create_task(engine._analyze())
    await asyncio.wait_for(metadata_entered.wait(),timeout=1)
    original_quote=market.quote
    async def stop_quote(symbol):
        if symbol=='BTCUSDT':return Quote(symbol,97.,97.02,market.now)
        return await original_quote(symbol)
    monkeypatch.setattr(market,'quote',stop_quote)
    protection=asyncio.create_task(engine.tick())
    protected_before_metadata=False
    try:
        await asyncio.wait_for(asyncio.shield(protection),timeout=.1)
        protected_before_metadata=store.position('BTCUSDT') is None
    except asyncio.TimeoutError:
        pass
    finally:
        metadata_release.set()
        await asyncio.gather(analysis,protection)
    assert protected_before_metadata, 'Une requête de métadonnées d’entrée bloque le stop d’une position existante.'


@pytest.mark.asyncio
async def test_slow_history_recovery_cannot_delay_other_position_stops(rig,monkeypatch):
    engine,store,market=seed_positions(rig)
    store.conn.execute('UPDATE trades SET last_market_at=? WHERE symbol=?',(market.now-20000,'ETHUSDT'))
    history_entered=asyncio.Event();history_release=asyncio.Event();original=market.candles
    async def slow_history(symbol,*args,**kwargs):
        if symbol=='ETHUSDT' and args and args[0]=='1m':
            history_entered.set();await history_release.wait()
        return await original(symbol,*args,**kwargs)
    async def quotes(symbol):return Quote(symbol,100. if symbol=='ETHUSDT' else 97.,100.02 if symbol=='ETHUSDT' else 97.02,market.now)
    monkeypatch.setattr(market,'candles',slow_history);monkeypatch.setattr(market,'quote',quotes)
    try:
        await asyncio.wait_for(engine.tick(),timeout=.2)
        await asyncio.wait_for(history_entered.wait(),timeout=1)
        assert {p['symbol'] for p in store.positions()}=={'ETHUSDT'}
        assert {p['symbol'] for p in store.trades()}=={'BTCUSDT','SOLUSDT'}
        assert not engine.health['market_ok'] and not engine.reconciled
    finally:
        history_release.set();await engine.reset_reconciliation_tasks()


def replace_open_position(engine,store,market,symbol):
    old=store.position(symbol)
    engine.paper.sell(Quote(symbol,100.,100.02,market.now),'FIXTURE_REPLACEMENT')
    new_signal='replacement:'+str(old['id'])
    decision=buy_decision().model_copy(update={'symbol':symbol})
    engine.paper.buy(OrderPlan(symbol,.5,100.07,98.,107.,.050035,1.2,2.5),
        Quote(symbol,100.,100.02,market.now),new_signal,decision,engine.settings(),{})
    return store.position(symbol)


@pytest.mark.asyncio
@pytest.mark.parametrize('action',['HOLD','SELL'])
async def test_management_response_never_targets_reopened_same_symbol(rig,monkeypatch,action):
    engine,store,market=seed_positions(rig,('BTCUSDT',))
    ai=PortfolioAI();ai.close_managed=action=='SELL';engine.ai=ai
    monkeypatch.setattr('app.engine.candidates',lambda *a,**k:[])
    entered=asyncio.Event();release=asyncio.Event();original=ai.decide
    async def delayed(context):
        entered.set();await release.wait();return await original(context)
    monkeypatch.setattr(ai,'decide',delayed)
    analysis=asyncio.create_task(engine._analyze());await entered.wait()
    reopened=replace_open_position(engine,store,market,'BTCUSDT')
    release.set();await analysis
    assert store.position('BTCUSDT')['id']==reopened['id']
    assert len(store.trades())==1
    assert 'remplacée' in store.rows('SELECT rejection FROM decisions')[-1]['rejection']


@pytest.mark.asyncio
@pytest.mark.parametrize('reset_wallet',[False,True])
async def test_history_after_await_cannot_close_replacement_or_erase_new_generation_pending(rig,monkeypatch,reset_wallet):
    from app.market import Candle
    engine,store,market=seed_positions(rig,('ETHUSDT',))
    store.conn.execute('UPDATE trades SET last_market_at=? WHERE symbol=?',(market.now-20000,'ETHUSDT'))
    entered=asyncio.Event();release=asyncio.Event()
    async def history(symbol,interval,limit=150,start_time=None,end_time=None):
        entered.set();await release.wait()
        return [Candle(start_time,start_time+59999,100.,110.,90.,100.,100.)]
    monkeypatch.setattr(market,'candles',history)
    task=asyncio.create_task(engine.reconcile(Quote('ETHUSDT',100.,100.02,market.now)))
    await entered.wait()
    if reset_wallet:
        store.reset(force=True);engine._settings_revision+=1;engine.started_ms+=1
        await engine.reset_reconciliation_tasks()
        seed_positions(rig,('ETHUSDT',))
    else:replace_open_position(engine,store,market,'ETHUSDT')
    reopened=store.position('ETHUSDT')
    marker={'id':reopened['id'],'since':market.now-1000,'until':market.now}
    store.set('reconcile_pending_by_id',{str(reopened['id']):marker})
    store.set('reconcile_pending',marker)
    release.set();await task
    assert store.position('ETHUSDT')['id']==reopened['id']
    assert store.get('reconcile_pending_by_id')=={str(reopened['id']):marker}


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_mode',['management','entry'])
@pytest.mark.parametrize('error_kind',['invalid_decision','quota'])
async def test_successful_parallel_ai_response_cannot_hide_other_call_failure(rig,monkeypatch,failed_mode,error_kind):
    engine,store,market=seed_positions(rig,('BTCUSDT',))
    install_entry_structure(rig,monkeypatch)
    ai=PortfolioAI();engine.ai=ai
    failed=asyncio.Event();valid_release=asyncio.Event();original=ai.decide
    reason=f'Fixture {failed_mode} {error_kind}'
    async def mixed(context):
        if context['mode']==failed_mode:
            failed.set()
            return AIResult(wait(reason),error=reason,error_kind=error_kind)
        await valid_release.wait()
        return await original(context)
    monkeypatch.setattr(ai,'decide',mixed)
    task=asyncio.create_task(engine._analyze())
    await asyncio.wait_for(failed.wait(),timeout=1)
    await asyncio.sleep(0)
    assert engine.health['ai_ok'] is False
    assert engine.health['ai_error']==reason
    valid_release.set();await task
    assert engine.health['ai_ok'] is False
    assert engine.health['ai_error_kind']==error_kind
    assert engine.health['ai_error']==reason
    assert any(row['rejection']==reason for row in store.rows('SELECT rejection FROM decisions'))


@pytest.mark.asyncio
@pytest.mark.parametrize('operational_mode',['management','entry'])
async def test_parallel_ai_health_prioritizes_operational_fault_without_losing_invalid_response(rig,monkeypatch,operational_mode):
    engine,store,market=seed_positions(rig,('BTCUSDT',))
    install_entry_structure(rig,monkeypatch)
    ai=PortfolioAI();engine.ai=ai
    async def errors(context):
        kind='quota' if context['mode']==operational_mode else 'invalid_decision'
        reason=f'Fixture {context["mode"]} {kind}'
        return AIResult(wait(reason),error=reason,error_kind=kind)
    monkeypatch.setattr(ai,'decide',errors)
    await engine._analyze()
    assert engine.health['ai_ok'] is False
    assert engine.health['ai_error_kind']=='quota'
    assert 'quota' in engine.health['ai_error'] and 'invalid_decision' in engine.health['ai_error']
    assert len(store.rows('SELECT rejection FROM decisions'))==2
