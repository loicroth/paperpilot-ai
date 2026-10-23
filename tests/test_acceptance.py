"""Acceptance cross-checks: independently derived accounting and atomic recovery.
All prices below are deterministic test fixtures, not a trading record.
"""
import sqlite3
import time
from datetime import datetime, timezone
import pytest
from app.ai import Decision
from app.config import Settings
from app.execution import PaperExecutionEngine
from app.market import Quote, Rules
from app.metrics import statistics
from app.risk import RiskEngine
from app.storage import Store


def fixture_quote(symbol='BTCUSDT',bid=100.0):
    return Quote(symbol,bid,bid+.01,int(time.time()*1000))


def fixture_buy(db,signal='acceptance-m5-1',symbol='BTCUSDT'):
    q=fixture_quote(symbol)
    d=Decision(symbol=symbol,action='BUY',reason='Fixture acceptance.',entry=100.01,
        stop_loss=99.0,take_profit=103.5,invalidation=99.2,confirmation='Confirmation fixture.',
        risks=['Fixture uniquement'],setup='breakout',confirmation_met=True)
    cfg=Settings.model_validate(db.get('settings'))
    rule=Rules(symbol,.001,100000.0,.001,5.0,1000000.0,.01)
    cash=db.get('cash')
    plan=RiskEngine.plan(d,q,rule,cfg,cash,cash,atr=.8)
    p=PaperExecutionEngine(db).buy(plan,q,signal,d,cfg,{'fixture':True})
    return p


@pytest.fixture
def db(tmp_path):
    store=Store(tmp_path/'acceptance.sqlite3')
    yield store
    store.close()


def test_mark_to_liquidation_accounting_identity_and_fees_at_open_position(db):
    p=fixture_buy(db)
    q=fixture_quote(bid=102)
    stats,pos=statistics(db,{'BTCUSDT':q})
    net_sale=q.bid*(1-p['slippage_bps']/10000)*(1-p['fee_rate'])*p['quantity']
    assert stats['equity']==pytest.approx(stats['cash']+net_sale)
    assert stats['equity']==pytest.approx(900+stats['realized_pnl']+stats['unrealized_pnl'])
    assert stats['cash']==pytest.approx(900-p['quantity']*p['entry']-p['entry_fee'])
    assert stats['realized_pnl']==0
    assert stats['total_fees']==pytest.approx(p['entry_fee'])
    assert stats['closed_trades']==0 and pos['current_price']==102


def test_aggregate_metrics_match_cash_ledger_and_two_trade_results(db):
    first=fixture_buy(db,symbol='BTCUSDT')
    paper=PaperExecutionEngine(db)
    paper.protect(fixture_quote('BTCUSDT',104))
    second=fixture_buy(db,signal='acceptance-m5-2',symbol='ETHUSDT')
    paper.protect(fixture_quote('ETHUSDT',98))
    trades=db.trades()
    winner=next(t for t in trades if t['net_pnl']>0)
    loser=next(t for t in trades if t['net_pnl']<0)
    stats,pos=statistics(db,{})
    total=sum(t['net_pnl'] for t in trades)
    assert stats['cash']==pytest.approx(900+total)
    assert stats['equity']==pytest.approx(900+total)
    assert stats['realized_pnl']==pytest.approx(total)
    assert stats['unrealized_pnl']==0 and pos is None
    assert stats['closed_trades']==2 and stats['winners']==1 and stats['losers']==1
    assert stats['win_rate']==50
    assert stats['avg_win']==pytest.approx(winner['net_pnl'])
    assert stats['avg_loss']==pytest.approx(loser['net_pnl'])
    assert stats['profit_factor']==pytest.approx(winner['net_pnl']/abs(loser['net_pnl']))
    assert stats['total_fees']==pytest.approx(sum(t['entry_fee']+t['exit_fee'] for t in trades))
    assert stats['realized_rr']==pytest.approx(sum(t['net_pnl']/t['risk_usdt'] for t in trades)/2)
    assert stats['by_symbol']['BTCUSDT']['net_pnl']==pytest.approx(winner['net_pnl'])
    assert stats['by_symbol']['ETHUSDT']['net_pnl']==pytest.approx(loser['net_pnl'])
    assert sum(d['net_pnl'] for d in stats['daily'])==pytest.approx(total)


def test_open_trade_uses_frozen_commission_and_slippage_after_settings_change(db):
    p=fixture_buy(db)
    cfg=db.get('settings'); cfg.update(fee_rate=.01,slippage_bps=100)
    db.set('settings',cfg)
    PaperExecutionEngine(db).sell(fixture_quote(bid=101),'Fixture after settings change')
    t=db.trades()[0]
    old_fill=101*(1-p['slippage_bps']/10000)
    assert t['exit']==pytest.approx(old_fill)
    assert t['exit_fee']==pytest.approx(old_fill*p['quantity']*p['fee_rate'])
    assert t['fee_rate']==.001 and t['slippage_bps']==5


@pytest.mark.parametrize('operation',['buy','sell'])
def test_database_failure_rolls_back_entire_order_and_cash_mutation(db,monkeypatch,operation):
    if operation=='sell':
        original=fixture_buy(db)
    before_cash=db.get('cash')
    def disk_failure(*args,**kwargs):
        raise sqlite3.OperationalError('Fixture disk I/O failure while recording order event')
    monkeypatch.setattr(db,'event',disk_failure)
    with pytest.raises(sqlite3.OperationalError):
        if operation=='buy': fixture_buy(db)
        else: PaperExecutionEngine(db).sell(fixture_quote(bid=101),'Fixture database failure')
    assert db.get('cash')==before_cash
    assert db.trades()==[]
    if operation=='buy':
        assert db.position() is None
        assert db.rows('SELECT id FROM trades')==[]
    else:
        assert db.position()['id']==original['id']
        assert db.position()['closed_at'] is None


def test_drawdown_uses_peak_then_trough_in_recorded_equity_and_current_value(db):
    for at,equity in [('2026-10-23T01:00:00+00:00',950),('2026-10-23T02:00:00+00:00',855),('2026-10-23T03:00:00+00:00',940)]:
        db.conn.execute('INSERT INTO equity(at,equity,cash,btc_equity) VALUES(?,?,?,?)',(at,equity,equity,None))
    db.set('cash',930)
    stats,_=statistics(db,{})
    assert stats['max_drawdown_pct']==pytest.approx(10)


def test_btc_comparison_uses_observed_baseline_and_current_bid(db):
    db.set('btc_baseline',100.01)
    stats,_=statistics(db,{'BTCUSDT':fixture_quote(bid=110)})
    assert stats['btc_benchmark_equity']==pytest.approx(900*110/100.01)
    assert stats['equity']==900 and stats['closed_trades']==0

# Reuse the established closed-candle integration fixture rather than duplicating it.
from test_engine import rig

@pytest.mark.asyncio
async def test_next_trade_risk_is_capped_by_remaining_utc_daily_loss_allowance(rig):
    engine,store,market,ai,_=rig
    now=datetime.now(timezone.utc)
    store.set('day_state',{'day':now.strftime('%Y-%m-%d'),'start_equity':900.0,'blocked':False,'baseline_approximation':False})
    store.set('cash',883.0)  # Simulated loss 17 of the 18 USDT daily allowance.
    await engine._analyze()
    position=store.position()
    assert position is not None
    assert 0<position['risk_usdt']<=1.0+1e-10
    assert position['risk_usdt']<883*.01
    assert store.get('cash')>=0


def test_btc_hold_comparison_applies_same_entry_and_exit_cost_conventions(db):
    cfg=Settings(); ask=100.01; bid=110.0
    slipped_entry=ask*(1+cfg.slippage_bps/10000)
    quantity=900/(slipped_entry*(1+cfg.fee_rate))
    db.set('btc_baseline',{'quantity':quantity,'fee_rate':cfg.fee_rate,'slippage_bps':cfg.slippage_bps})
    stats,_=statistics(db,{'BTCUSDT':fixture_quote(bid=bid)})
    expected=quantity*bid*(1-cfg.slippage_bps/10000)*(1-cfg.fee_rate)
    assert stats['btc_benchmark_equity']==pytest.approx(expected)
    assert expected<900*bid/ask
    assert db.position() is None and db.trades()==[]


@pytest.mark.asyncio
async def test_critical_analysis_error_stays_fail_closed_even_when_error_journal_fails(rig,monkeypatch):
    engine,store,market,ai,_=rig
    async def fail_analysis():
        raise sqlite3.OperationalError('Fixture critical read failure')
    def fail_journal(*args,**kwargs):
        raise sqlite3.OperationalError('Fixture error journal unavailable')
    monkeypatch.setattr(engine,'_analyze',fail_analysis)
    monkeypatch.setattr(store,'event',fail_journal)
    await engine.analyze()  # Error reporting itself must not escape as an unhandled task exception.
    assert engine.critical_error and 'OperationalError' in engine.critical_error
    assert engine.health['status']=='ERREUR'
    assert engine.health['ai_ok'] is False and engine.health['market_ok'] is False
    assert store.get('mode')=='paused'
    assert store.position() is None and store.get('cash')==900
    assert not ai.contexts
    engine.last_analysis_poll=0
    await engine.tick()
    assert engine.health['status']=='ERREUR'
    assert engine.analysis_task is None
    assert not ai.contexts and store.position() is None
