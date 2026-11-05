"""Authorized virtual-ledger correction invariants, never a market backtest."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from app.config import Settings
from app.crypto_retirement import MARKER, is_crypto_trade
from app.storage import Store, dumps

NOW = datetime(2026, 11, 5, 18, 30, tzinfo=timezone.utc)


def trade(db, symbol, *, kind='spot', pnl=None, opened='2026-11-05T10:00:00+00:00',
          closed=None, fee=.1, margin=50., asset=None):
    if pnl is not None and closed is None:
        closed = '2026-11-05T11:00:00+00:00'
    detail = {'context': {'rules': {'strategy_profile': 'expanded_v2'},
                          'markets': {symbol: {'instrument': {'asset_class': asset or
                              ('share_cfd' if kind == 'cfd' else 'crypto')}}}},
              'plan': {'rr': 2.3}, 'fixture': True}
    db.conn.execute('''INSERT INTO trades(symbol,opened_at,closed_at,entry,exit,quantity,
        stop_loss,take_profit,entry_fee,fees,net_pnl,risk_usdt,entry_reason,exit_reason,
        detail,last_market_at,entry_bid,entry_ask,signal,fee_rate,slippage_bps,
        kind,direction,margin,currency,fx_open,last_bid,last_ask,last_fx_bid,last_fx_ask)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (symbol, opened, closed, 100., 101. if closed else None, 1., 98., 108., fee,
         fee, pnl, 2., 'Fixture', 'FIXTURE' if closed else None, dumps(detail),
         1793890000000, 99.99, 100.01, f'fixture:{symbol}:{opened}:{closed}', .001, 5.,
         kind, 'LONG', margin if kind == 'cfd' else 0., 'USD' if kind == 'cfd' else 'USDT',
         1., 100., 100.02, .999, 1.001))
    return db.rows('SELECT * FROM trades WHERE id=last_insert_rowid()')[0]


@pytest.fixture
def db(tmp_path):
    store=Store(tmp_path/'retirement.sqlite3')
    yield store
    store.close()


def seed_history(db):
    closed_crypto=trade(db, 'XRPUSDT', pnl=-12.6)
    cfd=trade(db, 'CAP:AAPL', kind='cfd', pnl=10.268,
              opened='2026-11-04T10:00:00+00:00', closed='2026-11-04T11:00:00+00:00')
    spot=trade(db, 'LINKUSDT', fee=.1)
    held=trade(db, 'CAP:META', kind='cfd', fee=.2)
    db.set('cash', 900.-12.6+10.268-100.1-50.2)
    db.set('settings', Settings(risk_pct=.7, max_positions=2).model_dump())
    db.set('mode','paused')
    db.set('day_state', {'day':'2026-11-05','start_equity':897.,'blocked':False})
    db.set('btc_baseline', {'quantity':.02})
    db.set('last_equity', 897.)
    db.set('reconcile_pending_by_id', {
        str(spot['id']): {'id':spot['id'],'since':1,'until':2},
        str(held['id']): {'id':held['id'],'since':3,'until':4}})
    db.set('reconcile_pending', {'id':spot['id'],'since':1,'until':2})
    db.conn.execute('INSERT INTO usage VALUES(?,?,?)', ('2026-11-05', 17, 4000))
    db.conn.execute('INSERT INTO signals VALUES(?,?)', ('m5:global-fixture','2026-11-05T10:00:00Z'))
    db.conn.execute('INSERT INTO equity(at,equity,cash,btc_equity) VALUES(?,?,?,?)',
                    ('2026-11-05T10:30:00Z',890.,750.,910.))
    for symbol in ['XRPUSDT','ALL','CAP:AAPL']:
        db.conn.execute('INSERT INTO decisions(at,symbol,action,reason,detail) VALUES(?,?,?,?,?)',
                        ('2026-11-05T10:00:00Z',symbol,'WAIT','Fixture',dumps({'symbol':symbol})))
    db.event('STOP LOSS XRPUSDT');db.event('CLOTURE CFD PAPER CAP:AAPL')
    return closed_crypto, cfd, spot, held


def test_reversible_removal_rebases_cash_preserves_cfd_rows_and_limits(db):
    closed_crypto, cfd, spot, held=seed_history(db)
    before_cfd=db.rows("SELECT * FROM trades WHERE kind='cfd' ORDER BY id")
    before_settings=db.get('settings')
    result=db.retire_crypto(now=NOW)
    assert result['removed_closed']==1 and result['removed_open']==1
    assert result['removed_net_pnl']==-12.6 and result['restored_open_cost']==100.1
    assert result['cash_after']==pytest.approx(900.+10.268-50.2)
    assert db.rows('SELECT * FROM trades ORDER BY id')==before_cfd
    assert db.get('settings')==before_settings and db.get('mode')=='paused'
    assert db.rows('SELECT * FROM usage')==[{'day':'2026-11-05','calls':17,'tokens':4000}]
    assert len(db.rows('SELECT * FROM signals'))==1
    assert db.get('day_state')['start_equity']==pytest.approx(910.268)
    assert not db.get('day_state')['blocked']
    assert db.get('reconcile_pending_by_id')=={str(held['id']):{'id':held['id'],'since':3,'until':4}}
    assert db.get('reconcile_pending')['id']==held['id']
    assert db.get('btc_baseline') is None and db.get('last_equity') is None
    assert not db.rows('SELECT * FROM equity')
    assert [r['symbol'] for r in db.rows('SELECT * FROM decisions')]==['CAP:AAPL']
    assert not any('XRPUSDT' in row['message'] for row in db.rows('SELECT * FROM events'))
    assert db.get('statistics_scope_started_at')==result['at']
    snapshot=Path(db.path).parent/'backups'/result['backup_name']
    assert snapshot.is_file() and snapshot.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(snapshot) as backup:
        assert backup.execute('SELECT count(*) FROM trades').fetchone()[0]==4
        assert backup.execute('SELECT count(*) FROM equity').fetchone()[0]==1
        assert backup.execute('SELECT count(*) FROM decisions').fetchone()[0]==3
    assert result['cfd_closed_count']==1 and result['cfd_open_count']==1
    assert result['cfd_closed_net_pnl']==10.268


@pytest.mark.parametrize('pnl',[-12.616536077279758, 18.75, 0.])
def test_closed_crypto_result_removed_in_both_directions_without_resetting_cfd(db,pnl):
    trade(db,'BTCUSDT',pnl=pnl)
    cfd=trade(db,'CAP:COIN',kind='cfd',pnl=3.7)
    db.set('cash',900.+pnl+3.7)
    result=db.retire_crypto(now=NOW)
    assert db.get('cash')==pytest.approx(903.7)
    assert db.rows('SELECT * FROM trades')==[cfd]
    assert result['removed_net_pnl']==pnl


def test_restart_does_not_repeat_cash_correction_or_backup(db):
    seed_history(db)
    first=db.retire_crypto(now=NOW);before=db.get('cash')
    second=db.retire_crypto(now=NOW)
    assert first==second and db.get('cash')==before
    assert len(list((Path(db.path).parent/'backups').glob('avant_retrait_crypto_*')))==1


def test_backup_failure_leaves_active_ledger_unchanged(db,monkeypatch):
    seed_history(db)
    before=db.rows('SELECT * FROM trades');cash=db.get('cash');equity=db.rows('SELECT * FROM equity')
    def fail(*args):raise OSError('fixture disk unavailable')
    monkeypatch.setattr('app.crypto_retirement._backup',fail)
    with pytest.raises(OSError):db.retire_crypto(now=NOW)
    assert db.rows('SELECT * FROM trades')==before and db.get('cash')==cash
    assert db.rows('SELECT * FROM equity')==equity and db.get(MARKER) is None


def test_transaction_failure_rolls_back_cash_trades_and_counters(db,monkeypatch):
    seed_history(db)
    before=db.rows('SELECT * FROM trades');cash=db.get('cash');equity=db.rows('SELECT * FROM equity')
    original=db.set
    def fail(key,value):
        if key==MARKER:raise sqlite3.OperationalError('fixture transaction error')
        return original(key,value)
    monkeypatch.setattr(db,'set',fail)
    with pytest.raises(sqlite3.OperationalError):db.retire_crypto(now=NOW)
    assert db.rows('SELECT * FROM trades')==before and db.get('cash')==cash
    assert db.rows('SELECT * FROM equity')==equity and db.get(MARKER) is None
    assert len(list((Path(db.path).parent/'backups').glob('avant_retrait_crypto_*')))==1


def test_crypto_only_loss_latch_can_be_removed_without_cfd_activity_today(db):
    trade(db,'CAP:AAPL',kind='cfd',pnl=10.,opened='2026-11-04T10:00:00Z',closed='2026-11-04T11:00:00Z')
    trade(db,'BTCUSDT',pnl=-30.)
    db.set('cash',880.)
    db.set('day_state',{'day':'2026-11-05','start_equity':910.,'blocked':True})
    result=db.retire_crypto(now=NOW)
    assert db.get('cash')==910. and db.get('day_state')['start_equity']==910.
    assert not result['day_blocked']


def test_actual_cfd_daily_losses_remain_blocked(db):
    trade(db,'CAP:AAPL',kind='cfd',pnl=-30.)
    trade(db,'BTCUSDT',pnl=-2.)
    db.set('cash',868.)
    result=db.retire_crypto(now=NOW)
    assert db.get('cash')==870. and result['day_blocked']


def test_existing_daily_latch_cannot_be_attributed_to_crypto_when_cfd_recovered(db):
    # A profitable final fill does not disprove a real intraday CFD drawdown.
    trade(db,'CAP:AAPL',kind='cfd',pnl=5.)
    trade(db,'BTCUSDT',pnl=-30.)
    db.set('cash',875.)
    db.set('day_state',{'day':'2026-11-05','start_equity':900.,'blocked':True})
    result=db.retire_crypto(now=NOW)
    assert db.get('cash')==905. and result['day_blocked']
    assert db.get('day_state')['prior_block_preserved_conservatively']


def test_carried_cfd_preserves_conservative_existing_daily_latch(db):
    held=trade(db,'CAP:META',kind='cfd',opened='2026-11-04T15:00:00Z')
    trade(db,'BTCUSDT',pnl=-5.,opened='2026-11-04T15:00:00Z',closed='2026-11-04T17:00:00Z')
    db.set('cash',844.9)
    db.set('day_state',{'day':'2026-11-05','start_equity':950.,'blocked':True})
    result=db.retire_crypto(now=NOW)
    assert db.rows('SELECT * FROM trades')==[held]
    assert db.get('day_state')['start_equity']==955.
    assert result['day_baseline_approximation'] and result['day_blocked']


def test_share_cfd_crypto_related_company_is_kept_but_crypto_cfd_removed(db):
    coin=trade(db,'CAP:COIN',kind='cfd',pnl=2.)
    mstr=trade(db,'CAP:MSTR',kind='cfd',pnl=3.)
    btc=trade(db,'CAP:BTCUSD',kind='cfd',asset='crypto_cfd',fee=.2,margin=40.)
    db.set('cash',900.+5.-40.2)
    result=db.retire_crypto(now=NOW)
    assert db.rows('SELECT * FROM trades ORDER BY id')==[coin,mstr]
    assert result['removed_open']==1 and db.get('cash')==pytest.approx(905.)
    assert result['restored_open_cost']==40.2


def test_no_fill_is_invented_for_a_retired_open_position(db):
    trade(db,'BTCUSDT');db.set('cash',799.9)
    result=db.retire_crypto(now=NOW)
    assert not db.positions() and not db.trades() and db.get('cash')==900.
    with sqlite3.connect(Path(db.path).parent/'backups'/result['backup_name']) as backup:
        closed,exit_price=backup.execute('SELECT closed_at,exit FROM trades').fetchone()
        assert closed is None and exit_price is None


def test_invalid_closed_ledger_fails_before_mutation(db):
    trade(db,'BTCUSDT',pnl=-2.)
    db.conn.execute('UPDATE trades SET net_pnl=NULL')
    with pytest.raises(ValueError,match='résultat crypto clôturé'):
        db.retire_crypto(now=NOW)
    assert len(db.rows('SELECT * FROM trades'))==1 and db.get(MARKER) is None


def test_classifier_uses_instrument_metadata_and_does_not_guess_from_company_name():
    assert not is_crypto_trade({'symbol':'CAP:COIN','kind':'cfd','detail':{}})
    assert is_crypto_trade({'symbol':'BTCUSDT','kind':'spot','detail':{}})
    assert is_crypto_trade({'symbol':'CAP:BTCUSD','kind':'cfd','detail':{'instrument':{'asset_class':'CRYPTO_CFD'}}})


def test_portfolio_reset_starts_new_curve_without_repeating_crypto_refund(db,monkeypatch):
    seed_history(db)
    original=db.retire_crypto(now=NOW)
    reset_at='2026-11-06T10:00:00.000+00:00'
    monkeypatch.setattr('app.storage.utcnow',lambda: reset_at)
    db.reset(force=True)
    assert db.get('statistics_scope_started_at')==reset_at
    assert db.get(MARKER)==original
    assert db.get('cash')==900. and not db.trades() and not db.positions()
    assert db.retire_crypto(now=NOW)==original and db.get('cash')==900.
    assert len(list((Path(db.path).parent/'backups').glob('avant_retrait_crypto_*')))==1
    assert db.rows('SELECT * FROM usage')==[{'day':'2026-11-05','calls':17,'tokens':4000}]


def test_new_cfd_only_portfolio_reset_dates_its_empty_curve_without_migration(db,monkeypatch):
    db.set('settings', Settings(cfd_only=True).model_dump())
    reset_at='2026-11-06T10:00:00.000+00:00'
    monkeypatch.setattr('app.storage.utcnow',lambda: reset_at)
    db.reset()
    assert db.get('statistics_scope_started_at')==reset_at and db.get(MARKER) is None
