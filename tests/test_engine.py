"""Worker integration tests: fixtures are NOT live trades or profitability evidence."""
import asyncio
import math
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from app.ai import AIClient, AIResult, Decision, wait
from app.config import Settings
from app.engine import TradingEngine
from app.market import Candle, MarketError, Quote, Rules
from app.risk import RiskEngine
from app.storage import Store


class MarketFixture:
    def __init__(self, now):
        self.now = now
        self.price = 100.0
        self.failed = False
        self.candle_calls = []
        self.stale = False

    async def close(self):
        pass

    async def server_time(self):
        if self.failed:
            raise MarketError('Panne Binance simulée')
        return self.now

    async def quote(self, symbol):
        if self.failed:
            raise MarketError('Panne Binance simulée')
        return Quote(symbol, self.price, self.price + .02, self.now)

    async def rules(self, symbols):
        return {s: Rules(s, .001, 1000., .001, 5., 1e9, .01) for s in symbols}

    async def candles(self, symbol, interval, limit=150, start_time=None, end_time=None):
        if self.failed:
            raise MarketError('Panne Binance simulée')
        self.candle_calls.append((symbol, interval))
        duration = {'5m': 300000, '1h': 3600000, '1m': 60000}[interval]
        end = self.now // duration * duration - 1
        if self.stale:
            end -= duration
        result = []
        for i in range(150):
            close = (97.5 + i * .01 if interval == '5m' else 90 + i * .08) + .4 * math.sin(i * 1.2)
            op = close - .12
            result.append(Candle(end - (149-i) * duration - duration + 1,
                end - (149-i) * duration, op, close + .45, op - .45, close, 100.))
        if interval == '5m':
            c = result[-1]
            result[-1] = Candle(c.open_time, c.close_time, 99.6, 100.3, 99.4, 100., 200.)
        return result


def buy_decision():
    return Decision(symbol='BTCUSDT', action='BUY', reason='Cassure confirmée sur fixture.',
        entry=100.02, stop_loss=98., take_profit=107., invalidation=98.,
        confirmation='Bougie clôturée au-dessus de la résistance.', risks=['Fausse cassure'],
        setup='breakout', confirmation_met=True)


class AIFixture:
    test_enabled = True
    model = 'fixture'

    def __init__(self, decision=None, error=None):
        self.proposal = decision or buy_decision()
        self.error = error
        self.contexts = []
        self.entered = asyncio.Event()
        self.release = None

    async def close(self):
        pass

    async def decide(self, context):
        self.contexts.append(context)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return AIResult(self.proposal, self.error, 400, 100)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    # Freeze at 5 seconds after a close, with startup before that close.
    now = int(time.time() * 1000) // 300000 * 300000 + 5000
    monkeypatch.setattr('app.engine.time.time', lambda: now / 1000)
    store = Store(tmp_path / 'engine.sqlite3')
    env = SimpleNamespace(api_key='', model='gpt-4o-mini', binance_url='https://data-api.binance.vision')
    market = MarketFixture(now)
    ai = AIFixture()
    engine = TradingEngine(store, env, market=market, ai=ai)
    engine.started_ms = now - 10000
    engine.health['market_ok'] = True
    engine.reconciled = True
    engine.quotes = {s: Quote(s, 100., 100.02, now) for s in Settings().symbols}
    engine.last_analysis_poll = time.monotonic()
    store.set('mode', 'running')
    yield engine, store, market, ai, env
    if engine.analysis_task:
        engine.analysis_task.cancel()
    store.close()


@pytest.mark.asyncio
async def test_binance_unavailable_blocks_analysis_and_entries(rig):
    engine, store, market, ai, _ = rig
    market.failed = True
    await engine.tick()
    await engine._analyze()
    assert engine.health['market_ok'] is False
    assert engine.health['status'] == 'DONNÉES INDISPONIBLES'
    assert not ai.contexts and store.position() is None and store.get('cash') == 900.


@pytest.mark.asyncio
async def test_absent_openai_key_records_one_wait_and_never_trades(rig):
    engine, store, market, _, _ = rig
    engine.ai = AIClient('')
    try:
        await engine._analyze()
        await engine._analyze()
    finally:
        await engine.ai.close()
    rows = store.rows('SELECT * FROM decisions')
    assert len(rows) == 1 and rows[0]['action'] == 'WAIT'
    assert 'IA non configurée' in rows[0]['rejection']
    assert store.position() is None and store.get('cash') == 900.
    assert not store.rows('SELECT * FROM usage')


@pytest.mark.asyncio
async def test_candle_closed_before_start_cannot_trigger_trade(rig):
    engine, store, market, ai, _ = rig
    engine.started_ms = market.now
    await engine._analyze()
    assert not ai.contexts and not market.candle_calls
    assert not store.rows('SELECT * FROM decisions')
    assert store.position() is None


@pytest.mark.asyncio
async def test_one_batched_call_per_close_and_duplicate_survives_restart(rig):
    engine, store, market, ai, env = rig
    await engine._analyze()
    await engine._analyze()
    assert len(ai.contexts) == 1
    context = ai.contexts[0]
    assert set(context['markets']) == {'BTCUSDT', 'ETHUSDT', 'SOLUSDT'}
    assert all(context['markets'][s]['m5']['last']['close_time'] == context['closed_candle_ms'] for s in context['symbols'])
    # Actual indicators must expose schema-compatible setup identifiers.
    assert 'breakout' in context['candidates']['BTCUSDT']
    assert store.position() is not None
    assert len(store.rows('SELECT * FROM trades')) == 1
    assert store.get('cash') < 900.
    new_ai = AIFixture()
    restarted = TradingEngine(store, env, market=market, ai=new_ai)
    assert store.get('mode') == 'stopped'
    restarted.started_ms = engine.started_ms
    restarted.health['market_ok'] = True
    restarted.quotes = engine.quotes
    restarted.reconciled = True
    store.set('mode', 'running')
    await restarted._analyze()
    assert not new_ai.contexts and len(store.rows('SELECT * FROM trades')) == 1
    assert store.position()['symbol'] == 'BTCUSDT'


@pytest.mark.asyncio
async def test_stale_closed_candle_blocks_before_ai(rig):
    engine, store, market, ai, _ = rig
    market.stale = True
    await engine._analyze()
    assert not ai.contexts and store.position() is None
    assert engine.health['market_ok'] is False
    assert 'trop ancienne' in engine.health['message']


@pytest.mark.asyncio
async def test_openai_outage_preserves_portfolio(rig):
    engine, store, _, ai, _ = rig
    ai.error = 'OpenAI indisponible : fixture'
    ai.proposal = wait(ai.error)
    await engine._analyze()
    assert store.position() is None and store.get('cash') == 900.
    rows = store.rows('SELECT * FROM decisions')
    assert len(rows) == 1 and rows[0]['action'] == 'WAIT' and rows[0]['rejection']
    assert engine.health['ai_ok'] is False


@pytest.mark.asyncio
async def test_budget_exhaustion_is_persisted_wait_without_ai_call(rig):
    engine, store, _, ai, _ = rig
    cfg = engine.settings().model_dump()
    cfg['max_daily_calls'] = 0
    store.set('settings', cfg)
    await engine._analyze()
    rows = store.rows('SELECT * FROM decisions')
    assert len(rows) == 1 and rows[0]['action'] == 'WAIT'
    assert 'Budget' in rows[0]['rejection']
    assert not ai.contexts and store.position() is None


@pytest.mark.asyncio
async def test_slow_ai_entry_revalidates_fresh_quote_before_buy(rig):
    engine, store, market, ai, _ = rig
    ai.release = asyncio.Event()
    task = asyncio.create_task(engine._analyze())
    await ai.entered.wait()
    market.price = 101.
    ai.release.set()
    await task
    assert store.position() is None and store.get('cash') == 900.
    row = store.rows('SELECT * FROM decisions')[0]
    assert 'déplacé' in row['rejection']


@pytest.mark.asyncio
async def test_protection_runs_while_ai_waits_and_stale_hold_is_rejected(rig):
    engine, store, market, ai, _ = rig
    cfg = engine.settings()
    q = engine.quotes['BTCUSDT']
    rules = (await market.rules(['BTCUSDT']))['BTCUSDT']
    decision = buy_decision()
    plan = RiskEngine.plan(decision, q, rules, cfg, 900., 900., atr=1.013, now_ms=market.now)
    engine.paper.buy(plan, q, 'fixture-position-before-call', decision, cfg, {})
    ai.proposal = Decision(symbol='BTCUSDT', action='HOLD', reason='Conserver fixture.',
        entry=None, stop_loss=None, take_profit=None, invalidation=None,
        confirmation='Position suivie.', risks=[], setup='none', confirmation_met=False)
    ai.release = asyncio.Event()
    task = asyncio.create_task(engine._analyze())
    await ai.entered.wait()
    market.price = 97.5
    await engine.tick()
    assert not task.done(), 'AI is still pending but stops must run'
    assert store.position() is None
    assert store.trades()[0]['exit_reason'] == 'STOP_LOSS'
    ai.release.set()
    await task
    assert store.position() is None
    assert 'HOLD sans position' in store.rows('SELECT * FROM decisions')[0]['rejection']


@pytest.mark.asyncio
async def test_pause_during_ai_call_prevents_entry(rig):
    engine, store, _, ai, _ = rig
    ai.release = asyncio.Event()
    task = asyncio.create_task(engine._analyze())
    await ai.entered.wait()
    store.set('mode', 'paused')
    ai.release.set()
    await task
    assert store.position() is None
    assert 'pause' in store.rows('SELECT * FROM decisions')[0]['rejection']


@pytest.mark.asyncio
async def test_settings_changed_while_ai_waits_cancel_decision(rig):
    engine, store, _, ai, _ = rig
    ai.release = asyncio.Event()
    task = asyncio.create_task(engine._analyze())
    await ai.entered.wait()
    engine._settings_revision += 1
    ai.release.set()
    await task
    assert store.position() is None
    assert 'Paramètres modifiés' in store.rows('SELECT * FROM decisions')[0]['rejection']


def test_daily_loss_latch_does_not_clear_after_recovery_until_next_utc_day(rig):
    engine, store, _, _, _ = rig
    day = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    assert engine.day_state(900., day)['blocked'] is False
    assert engine.day_state(881.99, day)['blocked'] is True
    assert engine.day_state(910., day)['blocked'] is True
    tomorrow = engine.day_state(910., day + timedelta(days=1))
    assert tomorrow['blocked'] is False and tomorrow['day'] == '2026-10-24'


@pytest.mark.asyncio
async def test_decision_delayed_more_than_65_seconds_cannot_open(rig):
    engine, store, market, ai, _ = rig
    ai.release = asyncio.Event()
    task = asyncio.create_task(engine._analyze())
    await ai.entered.wait()
    market.now += 70000
    ai.release.set()
    await task
    assert store.position() is None and store.get('cash') == 900.
    assert 'trop tardive' in store.rows('SELECT * FROM decisions')[0]['rejection']


@pytest.mark.asyncio
async def test_database_failure_before_signal_claim_blocks_ai_and_orders(rig, monkeypatch):
    import sqlite3
    engine, store, _, ai, _ = rig
    def broken_claim(signal):
        raise sqlite3.OperationalError('fixture disk I/O failure')
    monkeypatch.setattr(store, 'claim_signal', broken_claim)
    await engine.analyze()
    assert not ai.contexts and store.position() is None and store.get('cash') == 900.
    assert engine.health['status'] == 'ERREUR'
    assert 'OperationalError' in engine.health['message']


@pytest.mark.asyncio
async def test_structured_ai_adapter_integrates_with_real_indicator_context(rig):
    import json
    import httpx
    engine, store, _, _, _ = rig
    payloads = []
    def handler(request):
        payload = json.loads(request.content)
        observed = json.loads(payload['input'][0]['content'][0]['text'])
        payloads.append(observed)
        return httpx.Response(200, json={
            'status': 'completed', 'usage': {'input_tokens': 800, 'output_tokens': 150},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': buy_decision().model_dump_json()}]}],
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ai = AIClient('fixture-key-never-live', client=http)
        ai.test_enabled = True
        engine.ai = ai
        await engine._analyze()
    assert len(payloads) == 1 and len(payloads[0]['markets']) == 3
    assert store.position() is not None
    assert store.position()['symbol'] == 'BTCUSDT'
    decision = store.rows('SELECT * FROM decisions')[0]
    assert decision['action'] == 'BUY' and decision['rejection'] is None
