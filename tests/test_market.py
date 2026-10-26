"""Clock-contract regressions: public bookTicker is stamped at local receipt."""
import asyncio
import time
import httpx
import pytest
from app.market import BinanceClient, MarketError


@pytest.mark.asyncio
async def test_bookticker_local_receipt_timestamp_not_future_when_exchange_clock_ahead(monkeypatch):
    clock={'seconds':1800000000.0}
    monkeypatch.setattr('app.market.time.time',lambda:clock['seconds'])
    monkeypatch.setattr('app.market.time.monotonic',lambda:clock['seconds'])
    client=BinanceClient()
    calls=[]
    async def get(endpoint,params=None):
        calls.append(endpoint)
        if endpoint=='time':
            # Exchange clock is a realistic 500 ms ahead of the application's clock.
            return {'serverTime':int(clock['seconds']*1000)+500}
        assert endpoint=='ticker/bookTicker'
        # Advance response reception without waiting in wall-clock time.
        clock['seconds']+=.020
        return {'symbol':'BTCUSDT','bidPrice':'100.00','askPrice':'100.02'}
    monkeypatch.setattr(client,'_get',get)
    try:
        q=await client.quote('BTCUSDT')
        local_receipt=int(clock['seconds']*1000)
        assert q.time_ms==local_receipt
        assert 0<=int(clock['seconds']*1000)-q.time_ms<=15000
        # Keep the independently estimated exchange clock: do not weaken clock checks.
        assert await client.server_time()>=local_receipt+499
        assert calls==['time','ticker/bookTicker']
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_slow_metadata_http_does_not_block_another_positions_quote(monkeypatch):
    clock = {'now': 1000.}
    real_sleep = asyncio.sleep
    async def paced_sleep(delay):
        clock['now'] += delay
        await real_sleep(0)
    monkeypatch.setattr('app.market.time.monotonic', lambda: clock['now'])
    monkeypatch.setattr('app.market.asyncio.sleep', paced_sleep)
    metadata_started, release_metadata = asyncio.Event(), asyncio.Event()
    starts = []
    async def handler(request):
        starts.append((request.url.path, clock['now']))
        if request.url.path.endswith('exchangeInfo'):
            metadata_started.set()
            await release_metadata.wait()
            return httpx.Response(200, json={'symbols': []})
        assert request.url.path.endswith('bookTicker')
        return httpx.Response(200, json={'symbol': 'ETHUSDT', 'bidPrice': '100', 'askPrice': '100.02'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw:
        api = BinanceClient(client=raw)
        api._clock_ms = int(time.time() * 1000)
        api._clock_monotonic = clock['now']
        metadata = asyncio.create_task(api._get('exchangeInfo'))
        await metadata_started.wait()
        quote = asyncio.create_task(api.quote('ETHUSDT'))
        try:
            for _ in range(10):
                await real_sleep(0)
                if quote.done():
                    break
            assert quote.done(), 'A pending metadata HTTP response must not hold the departure lock'
            assert not metadata.done()
            assert (await quote).symbol == 'ETHUSDT'
            assert starts[1][1] - starts[0][1] >= .06 - 1e-10
        finally:
            release_metadata.set()
            await asyncio.gather(metadata, quote, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_departures_are_spaced_and_never_exceed_sixteen_per_second(monkeypatch):
    clock = {'now': 1000.}
    real_sleep = asyncio.sleep
    async def paced_sleep(delay):
        clock['now'] += delay
        await real_sleep(0)
    monkeypatch.setattr('app.market.time.monotonic', lambda: clock['now'])
    monkeypatch.setattr('app.market.asyncio.sleep', paced_sleep)
    starts = []
    async def handler(request):
        starts.append(clock['now'])
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw:
        api = BinanceClient(client=raw)
        await asyncio.gather(*(api._get('ticker/bookTicker') for _ in range(32)))
    assert all(b - a >= .06 - 1e-10 for a, b in zip(starts, starts[1:]))
    assert all(sum(t <= other < t + 1 for other in starts) <= 16 for t in starts)


@pytest.mark.asyncio
async def test_queued_departure_rechecks_cooldown_received_while_pacing(monkeypatch):
    clock = {'now': 1000.}
    real_sleep = asyncio.sleep
    first_started, release_first = asyncio.Event(), asyncio.Event()
    pacing, release_pacing = asyncio.Event(), asyncio.Event()
    async def paced_sleep(delay):
        pacing.set()
        await release_pacing.wait()
        clock['now'] += delay
        await real_sleep(0)
    monkeypatch.setattr('app.market.time.monotonic', lambda: clock['now'])
    monkeypatch.setattr('app.market.asyncio.sleep', paced_sleep)
    starts = []
    async def handler(request):
        starts.append(request.url.path)
        if request.url.path.endswith('exchangeInfo'):
            first_started.set()
            await release_first.wait()
            return httpx.Response(429, headers={'Retry-After': '1'}, json={'code': -1003})
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw:
        api = BinanceClient(client=raw)
        first = asyncio.create_task(api._get('exchangeInfo'))
        await first_started.wait()
        queued = asyncio.create_task(api._get('ticker/bookTicker'))
        try:
            for _ in range(10):
                await real_sleep(0)
                if pacing.is_set():
                    break
            assert pacing.is_set(), 'Only the departure interval may delay the second request'
            release_first.set()
            with pytest.raises(MarketError, match='HTTP 429'):
                await first
            release_pacing.set()
            with pytest.raises(MarketError, match='pause'):
                await queued
            assert len(starts) == 1
        finally:
            release_first.set()
            release_pacing.set()
            await asyncio.gather(first, queued, return_exceptions=True)
