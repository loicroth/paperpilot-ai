"""Deadline and executable-price invariants, not evidence of strategy returns."""
from datetime import datetime, timezone
import pytest

from app.intraday import cfd_entry_allowed, cfd_exit_due
from app.risk import RiskEngine, RiskRejected
from app.storage import dumps
from test_derivatives import book, open_position, quote, proposal, rules, cfg, SYMBOL
from test_engine import rig


def ms(instant):
    return int(datetime.fromisoformat(instant).timestamp() * 1000)


@pytest.mark.parametrize('instant,allowed',[
    ('2026-11-02T20:29:59.999+00:00', True),
    ('2026-11-02T20:30:00+00:00', False),
    ('2026-11-02T23:59:59+00:00', False),
    ('2026-11-03T00:00:00+00:00', True),
    ('2026-11-07T12:00:00+00:00', False),
    ('2026-11-08T23:00:00+00:00', False),
    ('2026-07-06T20:30:00+00:00', False),  # Rule is UTC, not a DST-dependent hour.
])
def test_entry_window(instant,allowed):
    assert cfd_entry_allowed('expanded_v2',ms(instant)) is allowed
    assert cfd_entry_allowed('expanded_v1',ms(instant)) is True


def test_risk_and_execution_both_block_late_entries(book,monkeypatch):
    now=ms('2026-11-02T20:30:00+00:00')
    monkeypatch.setattr('app.execution.time.time',lambda:now/1000)
    db,paper=book
    with pytest.raises(RiskRejected,match='20:30 UTC'):
        RiskEngine.cfd_plan(proposal(),quote(),rules(),cfg(strategy_profile='expanded_v2'),900.,900.,atr=1.,now_ms=now)
    # A precomputed plan cannot bypass the later executable-price gate.
    from app.risk import OrderPlan
    plan=OrderPlan(SYMBOL,1,100,98,108,0,2,4,kind='cfd',margin=50,leverage=2)
    with pytest.raises(ValueError,match='fenêtre quotidienne'):
        paper.buy(plan,quote(),'late',proposal(),cfg(strategy_profile='expanded_v2'),{})
    assert not db.positions() and db.get('cash')==900.


def mark_v2(db):
    p=db.position();detail=p['detail']
    detail['context']={'rules':{'strategy_profile':'expanded_v2'}}
    db.conn.execute('UPDATE trades SET detail=? WHERE id=?',(dumps(detail),p['id']))


@pytest.mark.parametrize('short',[False,True])
def test_daily_exit_uses_current_side_and_survives_missed_deadline(book,monkeypatch,short):
    now=[ms('2026-11-02T12:00:00+00:00')]
    monkeypatch.setattr('app.execution.time.time',lambda:now[0]/1000)
    db,paper=book;open_position(book,short=short);mark_v2(db)
    now[0]=ms('2026-11-02T20:44:59+00:00')
    assert paper.protect(quote()) is None and db.position()
    now[0]=ms('2026-11-02T20:45:00+00:00')
    assert paper.protect(quote(market_status='CLOSED')) is None and db.position()
    # No fabricated fill at the deadline or on a closed-market quote.
    now[0]=ms('2026-11-03T00:01:00+00:00')
    fresh=quote(100.2,100.3)
    paper.protect(fresh)
    trade=db.trades()[0]
    assert trade['exit_reason']=='FIN_JOURNEE_V2'
    expected=fresh.ask*1.0005 if short else fresh.bid*.9995
    assert trade['exit']==pytest.approx(expected)
    assert db.get('cash')==pytest.approx(900.+trade['net_pnl'])


@pytest.mark.parametrize('short,target',[(False,False),(True,False),(False,True),(True,True)])
def test_hard_stop_or_target_keeps_priority_over_daily_exit(book,monkeypatch,short,target):
    now=[ms('2026-11-02T12:00:00+00:00')]
    monkeypatch.setattr('app.execution.time.time',lambda:now[0]/1000)
    db,paper=book;open_position(book,short=short);mark_v2(db)
    now[0]=ms('2026-11-02T20:45:00+00:00')
    price=(90. if short else 110.) if target else (103. if short else 97.)
    paper.protect(quote(price,price+.02))
    assert db.trades()[0]['exit_reason']==('TAKE_PROFIT' if target else 'STOP_LOSS')


def test_deadline_is_tied_to_opening_cohort_and_utc_date():
    p={'kind':'cfd','opened_at':'2026-11-03T00:10:00+05:00',
       'detail':{'context':{'rules':{'strategy_profile':'expanded_v2'}}}}
    assert not cfd_exit_due(p,ms('2026-11-02T20:44:59+00:00'))
    assert cfd_exit_due(p,ms('2026-11-02T20:45:00+00:00'))
    p['detail']['context']['rules']['strategy_profile']='expanded_v1'
    assert not cfd_exit_due(p,ms('2026-11-03T21:00:00+00:00'))
    p['detail']['context']['rules']['strategy_profile']='expanded_v2';p['kind']='spot'
    assert not cfd_exit_due(p,ms('2026-11-03T21:00:00+00:00'))


def test_cutoff_never_fills_stale_quotes(book,monkeypatch):
    now=[ms('2026-11-02T12:00:00+00:00')]
    monkeypatch.setattr('app.execution.time.time',lambda:now[0]/1000)
    db,paper=book;open_position(book);mark_v2(db)
    stale=quote()
    now[0]=ms('2026-11-02T20:45:00+00:00')
    with pytest.raises(ValueError,match='périmée'):
        paper.protect(stale)
    assert db.position()


@pytest.mark.asyncio
async def test_scanner_does_not_pay_for_an_entry_already_blocked_by_time(rig,monkeypatch):
    from test_multi_engine import configure
    engine,db,market,ai,_=configure(rig,monkeypatch)
    market.now=ms('2026-11-02T20:35:05+00:00');engine.started_ms=market.now-10000
    monkeypatch.setattr('app.engine.time.time',lambda:market.now/1000)
    monkeypatch.setattr('app.engine.pullback_reference',lambda *a,**k:None)
    db.set('settings',engine.settings().model_copy(update={'strategy_profile':'expanded_v2'}).model_dump())
    await engine._analyze()
    assert not ai.contexts and not db.positions() and not db.rows('SELECT * FROM usage')
    row=next(r for r in engine.scan['rows'] if r['symbol']=='CAP:NVDA')
    assert 'fenêtre des entrées CFD fermée' in row['reason']
