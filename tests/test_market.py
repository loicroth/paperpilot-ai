"""Clock-contract regressions: public bookTicker is stamped at local receipt."""
import pytest
from app.market import BinanceClient


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
