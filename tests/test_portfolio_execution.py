"""Multi-position paper accounting fixtures; no live fills or performance evidence."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json, logging, sqlite3, time
from types import SimpleNamespace
import pytest
from app.config import Settings
from app.execution import PaperExecutionEngine, cfd_valuation
from app.market import Candle, Quote
from app.metrics import statistics
from app.risk import OrderPlan
from app.storage import Store


def q(symbol,bid,ask=None):
    return Quote(symbol,bid,bid+.02 if ask is None else ask,int(time.time()*1000),
        currency='USD' if symbol.startswith('CAP:') else 'USDT',conversion_bid=.999,conversion_ask=1.001)


def cfg():
    return SimpleNamespace(**{**Settings().model_dump(),'cfd_enabled':True,'fee_rate':.001,'slippage_bps':5})


def open_spot(db,engine,symbol='BTCUSDT',qty=1.,signal=None):
    plan=OrderPlan(symbol,qty,100.05,98.,108.,100.05*qty*.001,3.,2.5)
    plan.structure_audit={'stop_reference':98.,'target_reference':108.}
    plan.risk_pct_used=1.
    detail={'plan':{**asdict(plan),'rr':2.5,'structure_audit':plan.structure_audit,'risk_pct_used':1.}}
    p=engine.buy(plan,q(symbol,100.,100.01),signal or 'signal:'+symbol,
        SimpleNamespace(action='BUY',reason='Structure fixture'),cfg(),detail)
    return p,plan


def open_cfd(db,engine,symbol='CAP:NVDA',short=False,signal=None):
    entry=99.95 if short else 100.06;qty=1.
    plan=OrderPlan(symbol,qty,entry,103. if short else 97.,90. if short else 110.,entry*.001*1.001,4.,2.5,
        kind='cfd',direction='SHORT' if short else 'LONG',margin=entry*1.001/2,leverage=2.,currency='USD',
        fx_open=1.001,financing_daily=.001,maintenance_margin_pct=50.)
    p=engine.buy(plan,q(symbol,100.,100.01),signal or 'signal:'+symbol,
        SimpleNamespace(action='SHORT' if short else 'BUY',reason='Structure CFD fixture'),cfg(),{'plan':asdict(plan)})
    return p,plan


@pytest.fixture
def portfolio(tmp_path):
    db=Store(tmp_path/'portfolio.sqlite3');engine=PaperExecutionEngine(db)
    yield db,engine
    db.close()


def test_three_symbols_open_and_each_lookup_targets_correct_holding(portfolio):
    db,engine=portfolio
    first,_=open_spot(db,engine,'BTCUSDT')
    second,_=open_spot(db,engine,'ETHUSDT')
    third,_=open_cfd(db,engine,'CAP:NVDA',short=True)
    assert [p['id'] for p in db.positions()]==[first['id'],second['id'],third['id']]
    assert db.position()['id']==first['id']
    assert db.position('ETHUSDT')['id']==second['id']
    assert db.position('CAP:NVDA')['direction']=='SHORT'
    assert db.position('SOLUSDT') is None


def test_duplicate_symbol_rejected_by_executor_and_database(portfolio):
    db,engine=portfolio;first,_=open_spot(db,engine)
    before=db.get('cash')
    with pytest.raises(ValueError,match='déjà ouverte'):
        open_spot(db,engine,signal='different-signal')
    assert db.get('cash')==before and len(db.positions())==1
    raw=db.rows('SELECT * FROM trades WHERE id=?',(first['id'],))[0]
    raw.pop('id');raw['signal']='direct-sql-duplicate'
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute('INSERT INTO trades('+','.join(raw)+') VALUES('+','.join('?' for _ in raw)+')',tuple(raw.values()))
    assert len(db.positions())==1


@pytest.mark.parametrize('symbol,short',[('BTCUSDT',False),('CAP:NVDA',False),('CAP:NVDA',True)])
def test_close_only_named_quote_releases_only_its_proceeds_or_margin(portfolio,symbol,short):
    db,engine=portfolio
    if symbol=='BTCUSDT': chosen,_=open_spot(db,engine,symbol)
    else: chosen,_=open_cfd(db,engine,symbol,short=short)
    other,_=open_spot(db,engine,'ETHUSDT')
    held_before=db.position('ETHUSDT');cash=db.get('cash')
    exitq=q(symbol,101.,101.02)
    net=engine.sell(exitq,'SIGNAL_IA')
    assert db.position(symbol) is None and db.position('ETHUSDT')==held_before
    assert db.position()['id']==other['id']
    assert db.trades()[0]['id']==chosen['id']
    stats,_=statistics(db,{'ETHUSDT':q('ETHUSDT',100.)})
    assert stats['equity']==pytest.approx(900.+net+stats['unrealized_pnl'],abs=1e-6)
    with pytest.raises(ValueError,match='Aucune position'):
        engine.sell(exitq,'DUPLICATE_EXIT')
    assert len(db.trades())==1


@pytest.mark.parametrize('short',[False,True])
def test_stop_and_gap_reconciliation_are_symbol_specific(portfolio,short):
    db,engine=portfolio
    other,_=open_spot(db,engine,'BTCUSDT')
    target,_=open_cfd(db,engine,'CAP:NVDA',short=short)
    now=int(time.time()*1000)
    candle=Candle(now-60000,now-1,100.,115.,85.,100.,10.)
    assert engine.reconcile(q('CAP:NVDA',100.),[candle],since_time=now-30000)
    trade=db.trades()[0]
    assert trade['symbol']=='CAP:NVDA' and trade['exit_reason']=='STOP_GAP_APPROXIMATION'
    assert trade['detail']['exit']['both_touched'] is True
    assert db.position('BTCUSDT')['id']==other['id']
    assert db.position('CAP:NVDA') is None


def test_spot_stop_only_affects_target_among_multiple_holds(portfolio):
    db,engine=portfolio;open_spot(db,engine,'BTCUSDT');open_spot(db,engine,'ETHUSDT')
    engine.protect(q('ETHUSDT',97.),portfolio_quotes={'BTCUSDT':q('BTCUSDT',100.)})
    assert db.position('ETHUSDT') is None and db.position('BTCUSDT') is not None
    assert db.trades()[0]['symbol']=='ETHUSDT'


def test_statistics_sum_spot_cfd_long_short_values_fees_and_margin(portfolio):
    db,engine=portfolio
    a,pa=open_spot(db,engine,'BTCUSDT',qty=2.)
    b,pb=open_cfd(db,engine,'CAP:NVDA')
    c,pc=open_cfd(db,engine,'CAP:TSLA',short=True)
    quotes={'BTCUSDT':q('BTCUSDT',102.),'CAP:NVDA':q('CAP:NVDA',101.),'CAP:TSLA':q('CAP:TSLA',99.)}
    stats,first=statistics(db,quotes)
    assert len(stats['positions'])==stats['open_positions_count']==3
    assert first['id']==a['id']
    assert stats['margin_used']==pytest.approx(pb.margin+pc.margin)
    assert stats['total_fees']==pytest.approx(pa.entry_fee+pb.entry_fee+pc.entry_fee)
    assert stats['equity']==pytest.approx(900.+stats['unrealized_pnl'],abs=1e-6)
    assert stats['unrealized_pnl']==pytest.approx(sum(p['unrealized_pnl'] for p in stats['positions']))
    assert stats['open_risk_usdt']==pa.risk_usdt+pb.risk_usdt+pc.risk_usdt
    assert stats['portfolio_risk_known'] and stats['portfolio_risk_used'] is not None
    assert stats['portfolio_risk_cap']==pytest.approx(stats['equity']*.02)


def test_stale_quote_risk_is_unknown_not_zero(portfolio):
    db,engine=portfolio;open_spot(db,engine)
    stats,first=statistics(db,{})
    assert first['valuation_stale'] is True
    assert stats['portfolio_risk_used'] is None and stats['portfolio_risk_known'] is False
    assert stats['portfolio_risk_pct'] is None
    assert stats['open_risk_usdt']==3.
    json.dumps(stats,allow_nan=False)


def test_cfd_maintenance_counts_other_healthy_positions_in_equity(portfolio):
    db,engine=portfolio
    open_spot(db,engine,'BTCUSDT',qty=8.) # Most cash is an actual spot holding, not missing equity.
    held,_=open_cfd(db,engine,'CAP:NVDA')
    # Extend the structural stop only for this accounting fixture to observe
    # maintenance rather than the stop. Total equity remains comfortably solvent.
    db.conn.execute('UPDATE trades SET stop_loss=1. WHERE id=?',(held['id'],))
    quote_cfd=q('CAP:NVDA',40.)
    engine.protect(quote_cfd,portfolio_quotes={'BTCUSDT':q('BTCUSDT',100.),'CAP:NVDA':quote_cfd})
    assert db.position('CAP:NVDA') is not None
    assert len(db.positions())==2


def test_no_overspend_when_multiple_orders_race(portfolio):
    db,engine=portfolio
    def submit(symbol):
        try: open_spot(db,engine,symbol,qty=4.);return True
        except ValueError as exc:
            assert 'Capital insuffisant' in str(exc)
            return False
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(submit,['BTCUSDT','ETHUSDT','SOLUSDT']))
    assert sum(results)==2 and len(db.positions())==2
    assert db.get('cash')>=0
    assert db.get('cash')==pytest.approx(900.-2*400.2*1.001)


def test_migration_backups_v2_and_preserves_trade_rows_and_cash(tmp_path):
    path=tmp_path/'legacy.sqlite3';db=Store(path);engine=PaperExecutionEngine(db)
    old,_=open_spot(db,engine);engine.sell(q('BTCUSDT',101.),'OLD_CLOSED')
    current,_=open_cfd(db,engine)
    before=db.rows('SELECT * FROM trades ORDER BY id');cash=db.get('cash')
    db.conn.execute('DROP INDEX one_position_per_symbol')
    db.conn.execute('CREATE UNIQUE INDEX one_position ON trades((1)) WHERE closed_at IS NULL')
    db.conn.execute('PRAGMA user_version=2');db.close()
    db=Store(path);engine=PaperExecutionEngine(db)
    assert db.rows('SELECT * FROM trades ORDER BY id')==before and db.get('cash')==cash
    backups=list((tmp_path/'backups').glob('avant_multi_positions_*.sqlite3'))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA user_version').fetchone()[0]==2
        assert backup.execute("SELECT name FROM sqlite_master WHERE name='one_position'").fetchone()
    assert db.conn.execute('PRAGMA user_version').fetchone()[0]==3
    assert db.position()['id']==current['id']
    open_spot(db,engine,'ETHUSDT');assert len(db.positions())==2
    db.close()
    # Restart with >1 position must not recreate the old global unique index.
    db=Store(path);assert len(db.positions())==2
    assert len(list((tmp_path/'backups').glob('avant_multi_positions_*.sqlite3')))==1
    db.close()


def test_reset_requires_all_holds_closed_or_explicit_force(portfolio):
    db,engine=portfolio;open_spot(db,engine);open_spot(db,engine,'ETHUSDT')
    with pytest.raises(ValueError,match='toutes les positions'):db.reset()
    assert len(db.positions())==2
    db.reset(force=True)
    assert not db.positions() and db.get('cash')==900.


def test_private_trade_audit_records_target_ids_without_credentials_or_cash(portfolio,caplog):
    db,engine=portfolio
    with caplog.at_level(logging.INFO,logger='paperpilot'):
        first,_=open_spot(db,engine,'BTCUSDT');second,_=open_spot(db,engine,'ETHUSDT')
        engine.sell(q('ETHUSDT',101.),'SIGNAL_IA')
    events=[json.loads(r.message.split('PAPER_TRADE_AUDIT ',1)[1]) for r in caplog.records if r.message.startswith('PAPER_TRADE_AUDIT ')]
    assert [(r['event'],r['symbol']) for r in events]==[('OPEN','BTCUSDT'),('OPEN','ETHUSDT'),('CLOSE','ETHUSDT')]
    assert events[-1]['trade_id']==second['id']
    assert events[0]['structure_audit']['stop_reference']==98.
    for event in events:
        assert not {'cash','balance','api_key','password','api_password'}&set(event)


def test_each_cfd_accrues_financing_once_without_mutating_other_hold(portfolio,monkeypatch):
    db,engine=portfolio
    first,_=open_cfd(db,engine,'CAP:NVDA');second,_=open_cfd(db,engine,'CAP:TSLA',short=True)
    now=int(time.time()*1000)
    monkeypatch.setattr('app.execution.time.time',lambda:now/1000)
    db.conn.execute('UPDATE trades SET financing_at=? WHERE id=?',(now-4*3600000,first['id']))
    db.conn.execute('UPDATE trades SET financing_at=? WHERE id=?',(now-6*3600000,second['id']))
    observations={'CAP:NVDA':q('CAP:NVDA',100.),'CAP:TSLA':q('CAP:TSLA',100.)}
    initial,_=statistics(db,observations);cash=db.get('cash')
    engine.protect(observations['CAP:NVDA'],portfolio_quotes=observations)
    accrued=db.position('CAP:NVDA')['financing_accrued']
    assert accrued>0 and db.position('CAP:TSLA')['financing_accrued']==0.
    assert db.get('cash')==cash # Financing is charged on close, valued while open.
    engine.protect(observations['CAP:NVDA'],portfolio_quotes=observations)
    assert db.position('CAP:NVDA')['financing_accrued']==accrued
    engine.protect(observations['CAP:TSLA'],portfolio_quotes=observations)
    final,_=statistics(db,observations)
    assert final['financing_total']==pytest.approx(initial['financing_total'],abs=1e-10)
    assert final['equity']==pytest.approx(initial['equity'],abs=1e-10)


def test_closing_all_mixed_positions_releases_each_asset_or_margin_exactly_once(portfolio):
    db,engine=portfolio
    open_spot(db,engine,'BTCUSDT',qty=2.)
    open_cfd(db,engine,'CAP:NVDA')
    open_cfd(db,engine,'CAP:TSLA',short=True)
    for symbol,bid in [('CAP:TSLA',99.),('BTCUSDT',102.),('CAP:NVDA',101.)]:
        engine.sell(q(symbol,bid),'SEQUENTIAL_CLOSE')
    assert not db.positions() and len(db.trades())==3
    stats,p=statistics(db,{})
    assert p is None and stats['positions']==[] and stats['open_positions_count']==0
    assert stats['equity']==db.get('cash')==pytest.approx(900.+sum(t['net_pnl'] for t in db.trades()),abs=1e-6)
    assert stats['unrealized_pnl']==0. and stats['margin_used']==0.
    assert stats['portfolio_risk_used']==0. and stats['portfolio_risk_known'] is True
