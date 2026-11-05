"""CFD-only API scope and accounting checks; no broker or AI calls."""
from types import SimpleNamespace
import copy
import time
import pytest
from app.engine import TradingEngine
from app.market import Quote
from app.metrics import statistics
from app.performance import strategy_performance
from app.storage import Store
from app.web import create_app, cfd_public_detail
from test_performance import trade
from test_web import FixtureMarket, NoNetworkAI, client, token


@pytest.fixture
def cfd_web(tmp_path):
    store=Store(tmp_path/'cfd-web.sqlite3')
    env=SimpleNamespace(cfd_only=True,api_key='',model='gpt-4o-mini',
        binance_url='https://data-api.binance.vision',password='',data_dir=tmp_path,
        host='127.0.0.1',port=8000)
    market=FixtureMarket()
    bot=TradingEngine(store,env,market=market,ai=NoNetworkAI())
    app=create_app(env=env,store=store,engine=bot,background=False)
    yield SimpleNamespace(app=app,bot=bot,store=store,env=env,market=market)
    store.close()


@pytest.mark.asyncio
async def test_settings_cannot_reactivate_crypto_or_disable_cfd_scope(cfd_web):
    async with client(cfd_web) as c:
        headers=await token(c)
        settings=(await c.get('/api/state')).json()['settings']
        settings.update(cfd_only=False,cfd_enabled=False,auto_scanner=True,
                        symbols=['BTCUSDT'],scanner_limit='obsolete',fee_rate='obsolete',risk_pct=.5)
        assert (await c.put('/api/settings',json=settings,headers=headers)).status_code==200
        saved=cfd_web.store.get('settings')
        assert saved['cfd_only'] and saved['cfd_enabled']
        assert saved['symbols']==[] and not saved['auto_scanner']
        assert saved['risk_pct']==.5
        # Saving CFD settings requires no request to the retired Spot provider.
        assert cfd_web.market.rules_calls==[]
        assert (await c.put('/api/settings',json=settings)).status_code==403


@pytest.mark.asyncio
async def test_public_state_excludes_old_benchmark_and_backup_metadata(cfd_web):
    now=int(time.time()*1000)
    cfd_web.bot.quotes={'BTCUSDT':Quote('BTCUSDT',100.,100.01,now),
                        'CAP:US500':Quote('CAP:US500',100.,100.01,now)}
    cfd_web.store.set('btc_baseline',100.)
    cfd_web.store.set('crypto_retirement_v1',{
        'at':'2026-11-05T09:00:00+00:00','removed_closed':5,'removed_open':0,
        'removed_net_pnl':-12.,'backup_name':'private.sqlite3','cash_before':888.})
    async with client(cfd_web) as c:
        state=(await c.get('/api/state')).json()
    assert state['cfd_only'] is True
    assert [q['symbol'] for q in state['quotes']]==['CAP:US500']
    stats=state['stats']
    assert stats['btc_benchmark_equity'] is None
    assert stats['history_scope']=='cfd_only'
    assert stats['crypto_cleanup']=={
        'at':'2026-11-05T09:00:00+00:00','removed_closed':5,'removed_open':0,'removed_net_pnl':-12.}
    assert [g['market'] for g in stats['strategy_performance']['current_groups']]==['cfd']
    assert 'private.sqlite3' not in str(state)


def test_equity_drawdown_starts_at_new_curve_not_the_original_deposit(cfd_web):
    db=cfd_web.store
    db.set('cash',865.)
    db.set('statistics_scope_started_at','2026-11-05T09:00:00+00:00')
    db.conn.execute('DELETE FROM equity')
    for at,equity in [('2026-11-05T09:00:00+00:00',880.),('2026-11-05T09:05:00+00:00',860.)]:
        db.conn.execute('INSERT INTO equity(at,equity,cash,btc_equity) VALUES(?,?,?,?)',(at,equity,equity,None))
    result,_=statistics(db,{},cfd_only=True)
    assert result['initial_capital']==900.
    assert result['equity']==865.
    assert result['max_drawdown_pct']==pytest.approx(20/880*100)
    assert result['max_drawdown_scope']=='since_cfd_curve_start'
    assert result['equity_history_started_at']=='2026-11-05T09:00:00+00:00'


def test_cfd_review_preserves_profiles_and_excludes_retired_crypto_from_counts():
    rows=[trade(1,-12.),trade(2,8.,market='cfd'),trade(3,-2.,market='cfd'),
          trade(4,10.,profile='classic',market='cfd')]
    report=strategy_performance(rows,'expanded_v1',cfd_only=True)
    assert report['current']['closed_trades']==2
    assert report['current']['net_pnl']==6.
    assert report['remaining']==48
    assert len(report['current_groups'])==1
    assert all(group['market']=='cfd' for group in report['groups'])
    assert sum(group['net_pnl'] for group in report['groups'])==16.
    # Legacy tooling retains its explicitly requested mixed-market view.
    assert strategy_performance(rows,'expanded_v1')['current']['closed_trades']==3


def test_historical_detail_view_trims_market_context_without_rewriting_cfd_audit():
    detail={'context':{
        'symbols':['BTCUSDT','CAP:META','CAP:BTCUSD'],
        'markets':{'BTCUSDT':{'last':100.},'CAP:META':{'last':101.},
                   'CAP:BTCUSD':{'instrument':{'asset_class':'crypto'}}},
        'rules':{'strategy_profile':'expanded_v2','symbols':['BTCUSDT','CAP:META'],
                 'risk_pct':1.,'fee_rate':.001},
        'entry_plans':[{'symbol':'BTCUSDT','entry':100.},{'symbol':'CAP:META','entry':101.}],
        'positions':[{'symbol':'BTCUSDT'},{'symbol':'CAP:META'}]},
        'plan':{'entry':101.,'stop_loss':99.,'take_profit':107.,'rr':2.4},
        'proposal':{'symbol':'CAP:META','action':'BUY'},
        'exit':{'reason':'TAKE_PROFIT','context':{'symbols':['BTCUSDT','CAP:META']}}}
    before=copy.deepcopy(detail)
    public=cfd_public_detail(detail)
    assert detail==before
    assert public['context']['symbols']==['CAP:META']
    assert public['context']['markets']=={'CAP:META':{'last':101.}}
    assert public['context']['positions']==[{'symbol':'CAP:META'}]
    assert public['context']['entry_plans']==[{'symbol':'CAP:META','entry':101.}]
    assert public['context']['rules']['strategy_profile']=='expanded_v2'
    assert public['context']['rules']['risk_pct']==1.
    assert public['plan']==detail['plan'] and public['proposal']==detail['proposal']
    assert public['exit']['context']['symbols']==['CAP:META']
    assert public['history_view']
