"""Fictional-price unit scenarios: these tests are not trading performance results."""
from dataclasses import replace
from decimal import Decimal
import math
import time

import pytest

from app.ai import Decision
from app.config import Settings
from app.execution import LiveExecutionEngine, PaperExecutionEngine
from app.market import Candle, Quote, Rules
from app.risk import RiskEngine, RiskRejected, floor_step
from app.storage import Store


def quote(bid=100.0, ask=100.01, symbol='BTCUSDT', age_ms=0):
    return Quote(symbol, bid, ask, int(time.time()*1000)-age_ms)


def decision():
    return Decision(symbol='BTCUSDT', action='BUY', reason='Fixture : cassure confirmée.',
        entry=100.01, stop_loss=99.0, take_profit=103.5, invalidation=99.2,
        confirmation='Clôture au-dessus de la résistance.', risks=['Volatilité'],
        setup='breakout', confirmation_met=True)


@pytest.fixture
def store(tmp_path):
    db=Store(tmp_path/'paper.sqlite3')
    yield db
    db.close()


@pytest.fixture
def settings(): return Settings()


@pytest.fixture
def rules(): return Rules('BTCUSDT', .001, 100000.0, .001, 5.0, 1000000.0, .01)


def make_plan(settings, rules, **overrides):
    args=dict(decision=decision(), quote=quote(), rules=rules, settings=settings,
              cash=900.0,equity=900.0,atr=.8)
    args.update(overrides)
    return RiskEngine.plan(**args)


def open_position(store,settings,rules,signal='fixture-m5-1'):
    plan=make_plan(settings,rules)
    p=PaperExecutionEngine(store).buy(plan,quote(),signal,decision(),settings,
        {'source':'deterministic test fixture','entry':{'setup':'breakout'}})
    return plan,p


def test_sizing_includes_entry_and_stop_exit_costs_and_binance_step(settings,rules):
    p=make_plan(settings,rules)
    assert p.risk_usdt <= 9.0+1e-10
    assert p.rr >= 2
    assert p.quantity >= rules.min_qty
    assert Decimal(str(p.quantity)) % Decimal(str(rules.step_size)) == 0
    assert rules.min_notional <= p.entry*p.quantity <= rules.max_notional
    assert p.entry*p.quantity+p.entry_fee <= 900.0
    slip=settings.slippage_bps/10000
    expected_loss=p.quantity*(p.entry*(1+settings.fee_rate)-p.stop_loss*(1-slip)*(1-settings.fee_rate))
    assert p.risk_usdt == pytest.approx(expected_loss)
    assert p.entry > quote().ask  # Adverse entry slippage, not a mid-price fill.


def test_floor_step_decimal_boundary():
    assert floor_step(.123999999,.001) == .123
    assert floor_step(1.234, .005) == 1.23
    with pytest.raises(RiskRejected): floor_step(1,0)


def test_buy_debits_cash_and_records_virtual_holdings(store,settings,rules):
    plan,p=open_position(store,settings,rules)
    assert p['symbol']=='BTCUSDT'
    assert p['quantity']==plan.quantity
    assert store.get('cash') == pytest.approx(900-plan.entry*plan.quantity-plan.entry_fee)
    assert p['entry_fee']==pytest.approx(plan.entry*plan.quantity*settings.fee_rate)
    assert p['entry_bid']==100.0 and p['entry_ask']==100.01
    assert store.trades()==[]


def test_stop_gap_uses_current_lower_bid_and_both_fees(store,settings,rules):
    plan,_=open_position(store,settings,rules)
    engine=PaperExecutionEngine(store)
    net=engine.protect(quote(98.0,98.01))
    assert net<0
    assert store.position() is None
    t=store.trades()[0]
    fill=98*(1-settings.slippage_bps/10000)
    assert t['exit']==pytest.approx(fill)
    assert t['exit_reason']=='STOP_LOSS'
    assert t['fees']==pytest.approx(plan.entry_fee+fill*plan.quantity*settings.fee_rate)
    assert t['net_pnl']==pytest.approx((fill-plan.entry)*plan.quantity-t['fees'])
    assert store.get('cash')==pytest.approx(900+t['net_pnl'])


def test_take_profit_is_capped_at_target_and_charges_exit_fee(store,settings,rules):
    plan,_=open_position(store,settings,rules)
    PaperExecutionEngine(store).protect(quote(105.0,105.01))
    t=store.trades()[0]
    assert t['exit_reason']=='TAKE_PROFIT'
    assert t['exit']==pytest.approx(plan.take_profit*(1-settings.slippage_bps/10000))
    assert t['exit']<105
    assert t['net_pnl']>0
    assert t['fees']==pytest.approx(t['entry_fee']+t['exit_fee'])
    assert store.get('cash')==pytest.approx(900+t['net_pnl'])


def test_no_short_sale_without_holdings(store):
    with pytest.raises(ValueError,match='Aucune position'):
        PaperExecutionEngine(store).sell(quote(),'Fixture signal IA')
    assert store.get('cash')==900


def test_capital_insufficient_for_min_notional_is_rejected(settings,rules):
    with pytest.raises(RiskRejected,match='Montant|minimum|Capital'):
        make_plan(settings,rules,cash=1)


def test_execution_checks_available_cash_again_atomically(store,settings,rules):
    plan=make_plan(settings,rules)
    store.set('cash',1.0)
    with pytest.raises(ValueError,match='Capital insuffisant'):
        PaperExecutionEngine(store).buy(plan,quote(),'fixture-too-big',decision(),settings,{})
    assert store.position() is None
    assert store.get('cash')==1.0


def test_second_position_and_averaging_down_are_blocked(store,settings,rules):
    plan,_=open_position(store,settings,rules)
    before=store.get('cash')
    with pytest.raises(RiskRejected,match='déjà ouverte'):
        make_plan(settings,rules,position=store.position())
    with pytest.raises(ValueError,match='déjà ouverte'):
        PaperExecutionEngine(store).buy(plan,quote(),'fixture-another',decision(),settings,{})
    assert store.get('cash')==before
    assert len(store.rows('SELECT * FROM trades'))==1


def test_same_signal_cannot_buy_again_even_after_position_closed(store,settings,rules):
    plan,_=open_position(store,settings,rules)
    PaperExecutionEngine(store).sell(quote(101,101.01),'Fixture sortie')
    cash=store.get('cash')
    with pytest.raises(ValueError,match='Signal déjà exécuté'):
        PaperExecutionEngine(store).buy(plan,quote(),'fixture-m5-1',decision(),settings,{})
    assert store.get('cash')==cash
    assert store.position() is None
    assert len(store.trades())==1


def test_signal_claim_is_persistent_across_restart(tmp_path):
    path=tmp_path/'restart.sqlite3'
    db=Store(path)
    assert db.claim_signal('BTCUSDT:closed-5m:fixture')
    db.close()
    db=Store(path)
    try:
        assert not db.claim_signal('BTCUSDT:closed-5m:fixture')
        assert db.claim_signal('BTCUSDT:closed-5m:next')
    finally: db.close()


def test_restart_preserves_holdings_cash_settings_and_trade_history(tmp_path,settings,rules):
    path=tmp_path/'restart.sqlite3'
    db=Store(path)
    custom=settings.model_copy(update={'risk_pct':.5})
    db.set('settings',custom.model_dump())
    _,p=open_position(db,custom,rules)
    saved_cash=db.get('cash')
    db.close()
    db=Store(path)
    try:
        assert db.position()['id']==p['id']
        assert db.position()['quantity']==p['quantity']
        assert db.get('cash')==saved_cash
        assert db.get('settings')['risk_pct']==.5
        PaperExecutionEngine(db).sell(quote(101,101.01),'Fixture fermeture après reprise')
        final_cash=db.get('cash')
    finally: db.close()
    db=Store(path)
    try:
        assert db.position() is None
        assert len(db.trades())==1
        assert db.trades()[0]['exit_reason']=='Fixture fermeture après reprise'
        assert db.get('cash')==final_cash
    finally: db.close()


def test_ambiguous_m1_gap_prioritizes_stop_and_records_uncertainty(store,settings,rules):
    plan,p=open_position(store,settings,rules)
    last=p['last_market_at']
    candle=Candle(last-1000,last+59000,100,104,98,101,10)
    assert PaperExecutionEngine(store).reconcile(quote(102,102.01),[candle])
    t=store.trades()[0]
    assert t['exit_reason']=='STOP_GAP_APPROXIMATION'
    assert t['detail']['exit']['uncertain'] is True
    assert t['detail']['exit']['both_touched'] is True
    assert t['detail']['exit']['partial_bar'] is True
    assert t['exit']<=98
    assert t['closed_at']>=t['opened_at']


def test_gap_take_profit_never_gets_advantageous_historical_price(store,settings,rules):
    _,p=open_position(store,settings,rules)
    candle=Candle(p['last_market_at']+1,p['last_market_at']+60000,100,104,99.5,103,10)
    assert PaperExecutionEngine(store).reconcile(quote(100.5,100.51),[candle])
    t=store.trades()[0]
    assert t['exit_reason']=='OBJECTIF_GAP_APPROXIMATION'
    assert t['exit']==pytest.approx(100.5*(1-settings.slippage_bps/10000))
    assert t['detail']['exit']['uncertain'] is True


def test_stale_quotes_block_buys_and_sells(store,settings,rules):
    with pytest.raises(RiskRejected,match='ancienne|futur'):
        make_plan(settings,rules,quote=quote(age_ms=16000))
    plan=make_plan(settings,rules)
    with pytest.raises(ValueError,match='périmée'):
        PaperExecutionEngine(store).buy(plan,quote(age_ms=16000),'old',decision(),settings,{})
    open_position(store,settings,rules)
    with pytest.raises(ValueError,match='périmée'):
        PaperExecutionEngine(store).sell(quote(98,98.01,age_ms=16000),'old')
    assert store.position() is not None


@pytest.mark.parametrize('changes',[
    {'quote':quote(100.1,100.0)},
    {'quote':quote(100,100.5)},
    {'quote':quote(float('nan'),100.01)},
    {'quote':quote(symbol='ETHUSDT')},
    {'atr':float('nan')},
    {'atr':4.0},
])
def test_incoherent_prices_symbols_and_volatility_are_rejected(settings,rules,changes):
    with pytest.raises(RiskRejected): make_plan(settings,rules,**changes)


@pytest.mark.parametrize('flag',['paused','day_blocked'])
def test_manual_pause_and_daily_loss_pause_refuse_entries(settings,rules,flag):
    with pytest.raises(RiskRejected): make_plan(settings,rules,**{flag:True})


def test_net_ratio_insufficient_after_fees_is_rejected(settings,rules):
    d=decision().model_copy(update={'take_profit':102.10})
    with pytest.raises(RiskRejected,match='Ratio net'):
        make_plan(settings,rules,decision=d)


def test_live_execution_cannot_be_instantiated():
    with pytest.raises(RuntimeError,match='réel verrouillé'):
        LiveExecutionEngine()


def test_reset_preserves_api_budget_and_refuses_open_holdings(store,settings,rules):
    assert store.reserve_usage('2026-10-23',288,10000,500)
    open_position(store,settings,rules)
    with pytest.raises(ValueError,match='Fermez la position'):
        store.reset()
    PaperExecutionEngine(store).sell(quote(101,101.01),'Fixture close')
    store.reset()
    assert store.get('cash')==900
    assert store.trades()==[]
    assert store.rows('SELECT calls,tokens FROM usage')[0]=={'calls':1,'tokens':500}
    assert store.get('mode')=='stopped'


def test_budget_reservation_blocks_overrun_and_uncertain_timeout_keeps_reservation(store):
    day='2026-10-23'
    assert store.reserve_usage(day,2,1000,400)
    store.settle_usage(day,400,0)
    assert store.rows('SELECT tokens FROM usage WHERE day=?',(day,))[0]['tokens']==400
    assert store.reserve_usage(day,2,1000,400)
    assert not store.reserve_usage(day,2,1000,100)
    assert not store.reserve_usage('2026-10-24',2,100,400)


def test_protection_rejects_wrong_symbol_without_advancing_last_market_at(store,settings,rules):
    _,p=open_position(store,settings,rules)
    before=p['last_market_at']
    with pytest.raises(ValueError):
        PaperExecutionEngine(store).protect(quote(100,100.01,symbol='ETHUSDT'))
    assert store.position()['last_market_at']==before


def test_protection_rejects_stale_quote_without_erasing_reconciliation_gap(store,settings,rules):
    _,p=open_position(store,settings,rules)
    with pytest.raises(ValueError):
        PaperExecutionEngine(store).protect(quote(100,100.01,age_ms=16000))
    assert store.position()['last_market_at']==p['last_market_at']


def test_sell_rejects_nonfinite_ask(store,settings,rules):
    open_position(store,settings,rules)
    with pytest.raises(ValueError):
        PaperExecutionEngine(store).sell(quote(100,float('nan')),'Fixture invalid')
    assert store.position() is not None
