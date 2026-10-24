import math
import time
from dataclasses import replace

import httpx
import pytest

from app.market import BinanceClient, Candle, MarketError, _common_step
from app.indicators import candidates, snapshot


def candles(n=150, price=100.):
    return [Candle(i*300000, (i+1)*300000-1, price, price+1, price-1, price, 100.) for i in range(n)]


def client_for(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BinanceClient(client=client), client


def test_flat_indicators_finite_and_known_values():
    result = snapshot(candles())
    assert result['ema9'] == result['ema21'] == result['ema50'] == 100.
    assert result['rsi14'] == 50.
    assert result['macd'] == result['macd_signal'] == result['macd_hist'] == 0.
    assert result['atr14'] == 2.
    assert result['support'] == 99.
    assert result['resistance'] == 101.
    assert result['volume_ratio'] == 1.
    assert len(result['candles']) == 40
    assert candidates(result, result) == []
    assert all(math.isfinite(v) for v in result.values() if isinstance(v, float))


def test_rsi_rising_market_known_100():
    rows = [Candle(i*300000, (i+1)*300000-1, 100+i, 102+i, 99+i, 101+i, 100) for i in range(150)]
    assert snapshot(rows)['rsi14'] == 100


def test_snapshot_rejects_short_nan_and_unordered():
    with pytest.raises(MarketError): snapshot(candles(20))
    rows = candles()
    rows[-1] = replace(rows[-1], close=float('nan'))
    with pytest.raises(MarketError): snapshot(rows)
    rows = candles()
    rows[-1] = rows[-2]
    with pytest.raises(MarketError): snapshot(rows)


def test_common_lot_precision():
    assert _common_step([.001, .005]) == .005
    assert _common_step([.002, .003]) == .006
    assert _common_step([0, .000001]) == .000001


def test_deterministic_breakout_and_extension_rejection():
    snap = snapshot(candles())
    m5 = dict(snap, ema9=100.5, ema21=100, rsi14=60, macd_hist=.2,
              atr14=1, atr_pct=1, resistance=101, volume_ratio=1.4,
              last=dict(snap['last'], open=100.5, close=101.5, low=100.3, high=101.8))
    h1 = dict(snap, ema21=99, ema50=98, ema21_previous=98.8)
    assert 'breakout' in candidates(m5, h1)
    extended = dict(m5, last=dict(m5['last'], high=104))
    assert candidates(extended, h1) == []
    assert candidates(m5, dict(h1, ema21_previous=100)) == []


@pytest.mark.asyncio
async def test_binance_time_quote_and_closed_klines():
    now = 1800000300000
    def handler(request):
        if request.url.path.endswith('/time'):
            return httpx.Response(200, json={'serverTime':now})
        if request.url.path.endswith('bookTicker'):
            return httpx.Response(200, json={'symbol':'BTCUSDT','bidPrice':'100','askPrice':'100.1'})
        opened = now - now % 300000
        return httpx.Response(200, json=[
            [opened-300000,'100','102','99','101','100',opened-1],
            [opened,'101','102','100','101.5','100',opened+299999]])
    api, raw = client_for(handler)
    quote = await api.quote('BTCUSDT')
    assert quote.bid == 100 and quote.ask == 100.1
    assert abs(quote.time_ms-now) < 1000
    rows = await api.candles('BTCUSDT','5m')
    assert len(rows) == 1 and rows[0].close == 101
    await raw.aclose()


@pytest.mark.asyncio
async def test_filter_combination_and_spot_only():
    payload = {'symbols':[{'symbol':'BTCUSDT','status':'TRADING','quoteAsset':'USDT',
        'isSpotTradingAllowed':True,'filters':[
        {'filterType':'LOT_SIZE','minQty':'0.001','maxQty':'1000','stepSize':'0.001'},
        {'filterType':'MARKET_LOT_SIZE','minQty':'0.002','maxQty':'100','stepSize':'0.002'},
        {'filterType':'MIN_NOTIONAL','minNotional':'5','applyToMarket':True},
        {'filterType':'NOTIONAL','minNotional':'10','maxNotional':'100000','applyMinToMarket':True,'applyMaxToMarket':True},
        {'filterType':'PRICE_FILTER','tickSize':'0.01'}]}]}
    api, raw = client_for(lambda r: httpx.Response(200,json=payload))
    rules = (await api.rules(['BTCUSDT']))['BTCUSDT']
    assert rules.min_qty == rules.step_size == .002
    assert rules.max_qty == 100 and rules.min_notional == 10 and rules.max_notional == 100000
    assert rules.tick_size == .01
    api._rules_at = 0
    payload['symbols'][0]['isSpotTradingAllowed'] = False
    with pytest.raises(MarketError): await api.rules(['BTCUSDT'])
    await raw.aclose()


@pytest.mark.asyncio
async def test_rate_limit_blocks_followup_without_more_requests():
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, headers={'Retry-After':'1'},json={'code':-1003})
    api, raw = client_for(handler)
    with pytest.raises(MarketError): await api.server_time()
    with pytest.raises(MarketError): await api.server_time()
    assert len(calls) == 1
    await raw.aclose()


@pytest.mark.asyncio
async def test_stale_inconsistent_and_missing_candles_rejected():
    now = 1800000300000
    opened = now - now % 300000
    rows = [[opened-600000,'100','102','99','101','100',opened-300001],
            [opened,'100','102','99','101','100',opened+299999]]
    def handler(req):
        return httpx.Response(200,json={'serverTime':now} if req.url.path.endswith('/time') else rows)
    api, raw = client_for(handler)
    with pytest.raises(MarketError, match='manquantes'): await api.candles('BTCUSDT','5m')
    rows[:] = [[opened-900000,'100','102','99','101','100',opened-600001]]
    with pytest.raises(MarketError, match='ancienne'): await api.candles('BTCUSDT','5m')
    rows[:] = [[opened-300000,'100','99','98','101','100',opened-1]]
    with pytest.raises(MarketError, match='OHLC'): await api.candles('BTCUSDT','5m')
    await raw.aclose()


@pytest.mark.asyncio
async def test_bad_quote_and_public_endpoint_guard():
    def handler(req):
        return httpx.Response(200,json={'serverTime':1800000300000} if req.url.path.endswith('/time') else
                              {'symbol':'BTCUSDT','bidPrice':'101','askPrice':'100'})
    api, raw = client_for(handler)
    with pytest.raises(MarketError): await api.quote('BTCUSDT')
    with pytest.raises(MarketError): await api.quote('BTCUSDC')
    with pytest.raises(MarketError): await api._get('order')
    with pytest.raises(ValueError): BinanceClient('https://api.binance.com')
    await raw.aclose()


@pytest.mark.asyncio
async def test_live_binance_market_data():
    """Explicit opt-in integration test; never substitutes fixture prices for Binance."""
    import os
    if os.getenv('PAPER_LIVE_TEST') != '1':
        pytest.skip('PAPER_LIVE_TEST=1 requis pour accéder au réseau Binance réel.')
    api = BinanceClient()
    try:
        symbols = ['BTCUSDT','ETHUSDT','SOLUSDT']
        rules = await api.rules(symbols)
        for symbol in symbols:
            for interval in ['5m','1h']:
                rows = await api.candles(symbol, interval, 150)
                assert len(rows) >= 60
                assert rows[-1].close_time < await api.server_time()
                assert math.isfinite(snapshot(rows)['atr14'])
            quote = await api.quote(symbol)
            assert 0 < quote.bid <= quote.ask
            assert rules[symbol].step_size > 0
    finally:
        await api.close()


def test_long_refusal_explains_bearish_h1_without_authorizing_trade():
    from app.indicators import long_refusals,candidates
    m={'last':{'open':100.,'close':100.2,'high':100.4,'low':99.9},'previous':{'close':100.},'atr14':1.,'atr_pct':1.,'ema21':100.,'rsi14':55.,'volume_ratio':1.5}
    h={'last':{'close':99.},'ema21':100.,'ema21_previous':100.2}
    reasons=long_refusals(m,h)
    assert any('H1 sous EMA21' in x for x in reasons)
    assert any('descendante' in x for x in reasons)
    assert candidates(m,h)==[]
