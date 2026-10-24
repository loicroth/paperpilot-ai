"""CFD accounting and risk fixtures. Not a live fill or profitability evidence."""
from dataclasses import replace
from types import SimpleNamespace
import time, sqlite3
import pytest
from app.config import Settings
from app.execution import PaperExecutionEngine, cfd_valuation
from app.market import Candle
from app.metrics import statistics
from app.risk import RiskEngine, RiskRejected
from app.storage import Store

SYMBOL='CAP:TEST'

def cfg(**changes):
    base=Settings().model_dump()
    base.update(symbols=[SYMBOL],cfd_enabled=True,cfd_leverage=2.,cfd_financing_daily=.001,allow_short=True,
        fee_rate=.001,slippage_bps=5.,max_spread_bps=30.,risk_pct=1.,min_rr=2.,max_atr_pct=3.)
    base.update(changes)
    return SimpleNamespace(**base)

def quote(bid=100.,ask=100.02,**changes):
    data=dict(symbol=SYMBOL,bid=bid,ask=ask,time_ms=int(time.time()*1000),currency='USD',market_status='TRADEABLE',
        conversion_rate=1.,conversion_bid=.999,conversion_ask=1.001)
    data.update(changes);return SimpleNamespace(**data)

def rules(**changes):
    data=dict(symbol=SYMBOL,min_qty=.001,max_qty=100000.,step_size=.001,min_notional=0.,max_notional=1e9,
        tick_size=.01,currency='USD',multiplier=1.,allow_short=True,min_margin_pct=20.,financing_daily=0.,maintenance_margin_pct=50.)
    data.update(changes);return SimpleNamespace(**data)

def proposal(short=False):
    return SimpleNamespace(symbol=SYMBOL,action='SHORT' if short else 'BUY',entry=100. if short else 100.02,
        stop_loss=102. if short else 98.,take_profit=92. if short else 108.,invalidation=102. if short else 98.,
        confirmation_met=True,setup='breakout',reason='Fixture structure')

@pytest.fixture
def book(tmp_path):
    db=Store(tmp_path/'cfd.sqlite3');engine=PaperExecutionEngine(db)
    yield db,engine
    db.close()

def open_position(book,short=False,**setting_changes):
    db,engine=book;c=cfg(**setting_changes);q=quote();d=proposal(short)
    plan=RiskEngine.plan(d,q,rules(),c,db.get('cash'),900.,atr=1.)
    engine.buy(plan,q,'fixture',d,c,{'fixture':True})
    return plan,c,q

@pytest.mark.parametrize('short',[False,True])
def test_margin_cash_equity_identity_and_close(book,short):
    db,engine=book;plan,c,q=open_position(book,short)
    assert db.get('cash')==pytest.approx(900-plan.margin-plan.entry_fee)
    p=db.position();v=cfd_valuation(p,q)
    stats,_=statistics(db,{SYMBOL:q})
    assert stats['equity']==pytest.approx(900+v['unrealized_pnl'],abs=1e-6)
    assert stats['margin_used']==pytest.approx(plan.margin)
    exitq=quote(96.,96.02) if short else quote(104.,104.02)
    net=engine.sell(exitq,'SIGNAL_IA')
    assert db.position() is None
    assert db.get('cash')==pytest.approx(900+net)
    t=db.trades()[0]
    assert t['net_pnl']==pytest.approx(net)
    assert t['direction']==('SHORT' if short else 'LONG')
    assert t['fees']==pytest.approx(t['entry_fee']+t['exit_fee'])
    assert t['financing_accrued']>=0

@pytest.mark.parametrize('short,stop_bid,stop_ask,target_bid,target_ask',[
    (False,97.,97.02,110.,110.02),(True,103.,103.02,90.,90.02)])
def test_stop_and_target_execute_adverse_side(book,short,stop_bid,stop_ask,target_bid,target_ask):
    db,engine=book;plan,c,q=open_position(book,short)
    engine.protect(quote(stop_bid,stop_ask))
    t=db.trades()[0];assert t['exit_reason']=='STOP_LOSS' and t['net_pnl']<0
    assert t['exit']==pytest.approx(stop_ask*1.0005 if short else stop_bid*.9995)
    db.reset(force=True);plan,c,q=open_position(book,short)
    engine.protect(quote(target_bid,target_ask));t=db.trades()[0]
    assert t['exit_reason']=='TAKE_PROFIT' and t['net_pnl']>0
    assert t['exit']==pytest.approx(plan.take_profit*(1.0005 if short else .9995))

@pytest.mark.parametrize('kwargs',[{'cfd_enabled':False},{'cfd_leverage':6},{'risk_pct':2}])
def test_disabled_or_unsafe_risk_rejected(kwargs):
    with pytest.raises(RiskRejected): RiskEngine.plan(proposal(),quote(),rules(),cfg(**kwargs),900.,900.,atr=1.)

@pytest.mark.parametrize('bad',[{'currency':'EUR'},{'multiplier':10},{'min_margin_pct':0},{'min_qty':1000}])
def test_unknown_contract_and_invalid_quantity_rejected(bad):
    with pytest.raises(RiskRejected): RiskEngine.plan(proposal(),quote(),rules(**bad),cfg(),900.,900.,atr=1.)


def test_leverage_capped_by_supplier_and_risk():
    p=RiskEngine.plan(proposal(),quote(),rules(min_margin_pct=50),cfg(cfd_leverage=5),900.,900.,atr=1.)
    assert p.leverage==2 and p.risk_usdt<=9.0000001
    assert p.margin==pytest.approx(p.entry*p.quantity*1.001/2)


def test_short_permissions_and_market_closed():
    with pytest.raises(RiskRejected): RiskEngine.plan(proposal(True),quote(),rules(allow_short=False),cfg(),900.,900.,atr=1.)
    with pytest.raises(RiskRejected): RiskEngine.plan(proposal(True),quote(market_status='CLOSED'),rules(),cfg(),900.,900.,atr=1.)


def test_huge_gap_may_exceed_collateral_and_is_not_clamped(book):
    db,engine=book;plan,c,q=open_position(book,True)
    # Very large but observed executable gap: preserve deficit, never clamp away loss.
    engine.protect(quote(5000.,5001.))
    t=db.trades()[0]
    assert t['exit_reason']=='MARGIN_STOP_OUT'
    assert db.get('cash')<0 and db.get('cash')==pytest.approx(900+t['net_pnl'])


def test_pending_financing_survives_restart_and_24h_close(book,monkeypatch):
    db,engine=book;plan,c,q=open_position(book)
    original=time.time();old=int((original-86401)*1000)
    db.conn.execute('UPDATE trades SET opened_at=?,financing_at=?',('2020-01-01T00:00:00+00:00',old))
    before=statistics(db,{SYMBOL:q})[0]
    assert before['financing_total']>=plan.entry*plan.quantity*.001
    engine=PaperExecutionEngine(db);engine.protect(quote())
    t=db.trades()[0]
    assert t['exit_reason']=='DUREE_MAX_24H' and t['financing_accrued']>0
    assert db.get('cash')==pytest.approx(900+t['net_pnl'])


def test_missing_history_never_permits_favorable_take_profit(book):
    db,engine=book;open_position(book)
    engine.protect(quote(110.,110.02),allow_take_profit=False)
    assert db.position() is not None
    engine.protect(quote(97.,97.02),allow_take_profit=False)
    assert db.position() is None

@pytest.mark.parametrize('short',[False,True])
def test_gap_both_touched_stop_wins(book,short):
    db,engine=book;plan,c,q=open_position(book,short)
    bar=Candle(q.time_ms,q.time_ms+59999,100.,110.,90.,100.,100.)
    assert engine.reconcile(quote(),[bar],since_time=q.time_ms)
    t=db.trades()[0]
    assert t['exit_reason']=='STOP_GAP_APPROXIMATION'
    assert t['detail']['exit']['both_touched'] is True
    assert t['exit']>=110. if short else t['exit']<=90.


def test_fx_profit_uses_bid_loss_and_fees_use_ask(book):
    db,engine=book;plan,c,q=open_position(book)
    exitq=quote(104.,104.02,conversion_bid=.9,conversion_ask=1.1)
    net=engine.sell(exitq,'FX_TEST');t=db.trades()[0]
    expected_gross=(t['exit']-t['entry'])*t['quantity']*.9
    expected_fee=t['exit']*t['quantity']*c.fee_rate*1.1
    assert t['exit_fee']==pytest.approx(expected_fee)
    assert net==pytest.approx(expected_gross-t['entry_fee']-expected_fee-t['financing_accrued'])


def test_migration_preserves_wallet_and_history(tmp_path):
    path=tmp_path/'old.sqlite3';db=Store(path)
    db.set('cash',777.);db.set('sentinel','original')
    # Recreate a genuine pre-CFD schema using SQLite's additive-column reversal.
    for name in ['kind','direction','margin','leverage','multiplier','currency','fx_open','financing_daily','financing_accrued','financing_at','maintenance_margin_pct']:
        db.conn.execute('ALTER TABLE trades DROP COLUMN '+name)
    db.conn.execute('PRAGMA user_version=1');db.close()
    db=Store(path)
    assert db.get('cash')==777. and db.get('sentinel')=='original'
    assert {'kind','margin','direction'}<={r[1] for r in db.conn.execute('PRAGMA table_info(trades)')}
    assert db.conn.execute('PRAGMA user_version').fetchone()[0]==2
    db.close()


def test_last_observed_valuation_survives_quote_outage(book):
    db,engine=book;open_position(book)
    # An adverse observation that does not yet cross the structural stop.
    q=quote(99.,99.02,conversion_bid=.99,conversion_ask=1.01)
    engine.protect(q)
    live,p=statistics(db,{SYMBOL:q})
    unavailable,p=statistics(db,{})
    assert unavailable['equity']==pytest.approx(live['equity'],abs=1e-6)
    assert p['current_price']==99. and p['valuation_stale'] is True
    assert p['last_fx_ask']==1.01
