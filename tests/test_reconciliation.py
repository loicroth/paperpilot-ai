"""Synthetic interruption fixtures; these are conservative execution tests, not live results."""
from datetime import datetime, timezone
import time
from types import SimpleNamespace

import pytest

from app.ai import Decision
from app.config import Settings
from app.engine import TradingEngine
from app.market import Candle, MarketError, Quote, Rules
from app.risk import RiskEngine
from app.storage import Store
from test_structural_risk import structural_fixture


class RecoveryMarket:
    def __init__(self, clock):
        self.clock = clock
        self.bars = []
        self.bid = 100.
        self.requests = []

    async def close(self):
        pass

    async def server_time(self):
        return self.clock['now']

    async def quote(self, symbol):
        return Quote(symbol, self.bid, self.bid + .02, self.clock['now'])

    async def candles(self, symbol, interval, limit=150, start_time=None, end_time=None):
        assert interval == '1m'
        self.requests.append((start_time, end_time))
        return [bar for bar in self.bars if bar.open_time >= start_time and bar.close_time < self.clock['now']][:limit]


class NoAI:
    test_enabled = True
    async def close(self):
        pass
    async def decide(self, context):
        raise AssertionError('No AI needed for protective execution')


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    base = int(time.time() * 1000) // 60000 * 60000
    clock = {'now': base + 10000}
    monkeypatch.setattr('app.engine.time.time', lambda: clock['now'] / 1000)
    monkeypatch.setattr('app.execution.utcnow', lambda: datetime.fromtimestamp(clock['now']/1000, timezone.utc).isoformat(timespec='milliseconds'))
    path = tmp_path / 'recover.sqlite3'
    store = Store(path)
    env = SimpleNamespace(api_key='', model='gpt-4o-mini', binance_url='https://data-api.binance.vision')
    market = RecoveryMarket(clock)
    engine = TradingEngine(store, env, market=market, ai=NoAI())
    settings = Settings()
    quote = Quote('BTCUSDT', 100., 100.02, clock['now'])
    proposal = Decision(symbol='BTCUSDT', action='BUY', reason='Fixture avant interruption.',
        entry=100.02, stop_loss=98., take_profit=107., invalidation=98.,
        confirmation='Fixture clôturée.', risks=['Interruption'], setup='breakout', confirmation_met=True)
    rules = Rules('BTCUSDT', .001, 1000., .001, 5., 1e9, .01)
    plan = RiskEngine.plan(proposal, quote, rules, settings, 900., 900., atr=1.013,
        now_ms=clock['now'], structure_context=structural_fixture(proposal, 1.013, clock['now']))
    engine.paper.buy(plan, quote, 'before-gap', proposal, settings, {})
    yield SimpleNamespace(base=base, clock=clock, path=path, store=store, env=env,
        market=market, engine=engine, settings=settings)
    # A test may reopen the same database; closing an already-closed sqlite connection is safe.
    store.close()


def candle(open_time, low=99., high=101.):
    return Candle(open_time, open_time+59999, 100., high, low, 100., 100.)


def current(rig, bid=100.):
    return Quote('BTCUSDT', bid, bid+.02, rig.clock['now'])


@pytest.mark.asyncio
async def test_partial_gap_remains_pending_when_no_closed_m1_exists(recovery):
    r = recovery
    r.clock['now'] = r.base + 40000
    await r.engine.reconcile(current(r))
    pending = r.store.get('reconcile_pending')
    assert pending['since'] == r.base+10000 and pending['until'] == r.base+40000
    assert r.engine.reconciled is False
    assert r.store.position()['last_market_at'] == r.base+40000
    assert r.store.trades() == []


@pytest.mark.asyncio
async def test_later_closed_partial_bar_is_not_lost_after_protect_advances_timestamp(recovery):
    r = recovery
    r.clock['now'] = r.base+40000
    await r.engine.reconcile(current(r))
    r.clock['now'] = r.base+45000
    r.engine.paper.protect(current(r))
    assert r.store.position()['last_market_at'] == r.base+45000
    # This minute contains the unresolved outage portion. Both touched means STOP first.
    r.market.bars = [candle(r.base, low=97., high=108.)]
    r.clock['now'] = r.base+65000
    await r.engine.reconcile(current(r))
    assert r.store.position() is None and r.engine.reconciled is True
    assert r.store.get('reconcile_pending') is None
    trade = r.store.trades()[0]
    assert trade['exit_reason'] == 'STOP_GAP_APPROXIMATION'
    assert trade['exit'] == pytest.approx(97.*(1-r.settings.slippage_bps/10000))
    assert trade['detail']['exit']['both_touched'] is True
    assert trade['detail']['exit']['partial_bar'] is True
    assert trade['detail']['exit']['uncertain'] is True
    # Execution is booked at actual recovery time, not backdated to the candle touch.
    recorded = int(datetime.fromisoformat(trade['closed_at']).timestamp()*1000)
    assert recorded == r.clock['now'] and recorded > r.market.bars[0].close_time


@pytest.mark.asyncio
async def test_recovery_uses_lower_current_bid_when_worse_than_historical_stop(recovery):
    r = recovery
    r.market.bars = [candle(r.base, low=97., high=108.)]
    r.market.bid = 95.
    r.clock['now'] = r.base+65000
    await r.engine.reconcile(current(r, bid=95.))
    trade = r.store.trades()[0]
    assert trade['exit_reason'] == 'STOP_GAP_APPROXIMATION'
    assert trade['exit'] == pytest.approx(95.*(1-r.settings.slippage_bps/10000))
    assert trade['net_pnl'] < 0


@pytest.mark.asyncio
async def test_target_gap_never_receives_better_than_current_bid_or_target(recovery):
    r = recovery
    r.market.bars = [candle(r.base, low=99., high=108.)]
    r.market.bid = 103.
    r.clock['now'] = r.base+65000
    await r.engine.reconcile(current(r, bid=103.))
    trade = r.store.trades()[0]
    assert trade['exit_reason'] == 'OBJECTIF_GAP_APPROXIMATION'
    assert trade['exit'] == pytest.approx(103.*(1-r.settings.slippage_bps/10000))
    assert trade['detail']['exit']['uncertain'] is True


@pytest.mark.asyncio
async def test_pending_gap_and_position_survive_store_reopen(recovery):
    r = recovery
    r.clock['now'] = r.base+40000
    await r.engine.reconcile(current(r))
    pending = r.store.get('reconcile_pending')
    cash = r.store.get('cash')
    r.store.close()
    reopened = Store(r.path)
    try:
        assert reopened.get('reconcile_pending') == pending
        assert reopened.get('cash') == cash
        restarted = TradingEngine(reopened, r.env, market=r.market, ai=NoAI())
        assert reopened.get('mode') == 'stopped' and reopened.position() is not None
        r.market.bars = [candle(r.base, low=97., high=108.)]
        r.clock['now'] = r.base+65000
        await restarted.reconcile(current(r))
        assert reopened.position() is None
        assert reopened.trades()[0]['exit_reason'] == 'STOP_GAP_APPROXIMATION'
        assert reopened.get('reconcile_pending') is None
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_incomplete_leading_m1_blocks_reconciliation_and_keeps_pending(recovery):
    r = recovery
    r.market.bars = [candle(r.base+60000)]
    r.clock['now'] = r.base+125000
    with pytest.raises(MarketError, match='premières bougies'):
        await r.engine.reconcile(current(r))
    assert r.engine.reconciled is False
    assert r.store.position() is not None
    assert r.store.get('reconcile_pending')['since'] == r.base+10000
    assert r.store.position()['last_market_at'] == r.base+10000
    assert r.store.trades() == []


@pytest.mark.asyncio
async def test_missing_closed_minutes_never_assumes_successful_backfill(recovery):
    r = recovery
    r.clock['now'] = r.base+125000
    with pytest.raises(MarketError, match='incomplètes'):
        await r.engine.reconcile(current(r))
    assert r.engine.reconciled is False and r.store.position() is not None
    assert r.store.get('reconcile_pending') is not None
    assert r.store.trades() == []


@pytest.mark.asyncio
async def test_current_observed_stop_still_executes_when_history_is_incomplete(recovery):
    r = recovery
    r.clock['now'] = r.base+125000
    r.market.bars = [candle(r.base+60000)]  # Leading minute unavailable.
    r.market.bid = 97.
    await r.engine.tick()
    assert r.engine.health['market_ok'] is False
    assert r.engine.health['status'] == 'DONNÉES INDISPONIBLES'
    assert r.store.position() is None, 'Fresh observed stop must not wait for missing old candles'
    trade = r.store.trades()[0]
    assert trade['exit'] <= 97.
    assert trade['net_pnl'] < 0


@pytest.mark.asyncio
async def test_missing_history_does_not_book_favorable_target_without_stop_order_known(recovery):
    r = recovery
    r.clock['now'] = r.base+125000
    r.market.bars = [candle(r.base+60000)]
    r.market.bid = 108.
    await r.engine.tick()
    assert r.engine.health['market_ok'] is False
    assert r.store.position() is not None
    assert r.store.trades() == []


@pytest.mark.asyncio
async def test_quote_is_refreshed_after_backfill_before_execution(recovery):
    r = recovery
    r.market.bars = [candle(r.base, low=97., high=108.)]
    r.market.bid = 95.  # Market worsened while the initial quote/backfill was being processed.
    r.clock['now'] = r.base+65000
    await r.engine.reconcile(current(r, bid=100.))
    trade = r.store.trades()[0]
    assert trade['exit'] == pytest.approx(95.*(1-r.settings.slippage_bps/10000))
    assert trade['exit_bid'] == 95.
