"""Contract-shaped fixtures, not real broker sessions, trades or profitability."""
import json
from datetime import datetime, timezone
import httpx
import pytest
from app.capital import CapitalClient, DEMO_BASE
from app.market import MarketError

NOW=1792820700000
TIME='2026-10-24T05:45:00'
DATE='Sat, 24 Oct 2026 05:45:00 GMT'


def details():
    # Shape from official API docs/Postman collection; extended precision keys
    # documented at open-api.capital.com. Values are explicitly test fixtures.
    return {'instrument':{'epic':'SILVER','name':'Silver','type':'COMMODITIES',
        'currency':'USD','lotSize':1,'expiry':'-','marginFactor':10,'marginFactorUnit':'PERCENTAGE'},
        'dealingRules':{k:{'unit':'POINTS','value':v} for k,v in {
            'minDealSize':1,'maxDealSize':100000,'minSizeIncrement':.1,'minStepDistance':.001}.items()},
        'snapshot':{'marketStatus':'TRADEABLE','delayTime':0,'scalingFactor':1,'decimalPlacesFactor':3}}


def market(**extra):
    return {'epic':'SILVER','instrumentType':'COMMODITIES','instrumentName':'Silver','expiry':'-',
        'marketStatus':'TRADEABLE','delayTime':0,'updateTimeUTC':TIME,'bid':24.,'offer':24.02,**extra}


@pytest.fixture
def rig(monkeypatch):
    captured=[]; state={'details':details(),'rows':[market()],'auths':0,'fail_auth':False,'fail_once':False}
    async def no_sleep(_):pass
    monkeypatch.setattr('app.capital.asyncio.sleep',no_sleep)
    async def handler(request):
        captured.append(request)
        assert str(request.url).startswith(DEMO_BASE+'/')
        path=request.url.path
        if path.endswith('/session'):
            state['auths']+=1
            if state['fail_auth']:return httpx.Response(401,json={'message':'secret-key and secret-password'},headers={'Date':DATE})
            return httpx.Response(200,json={},headers={'CST':'fixture-cst','X-SECURITY-TOKEN':'fixture-security','Date':DATE})
        if state['fail_once']:
            state['fail_once']=False;return httpx.Response(401,json={},headers={'Date':DATE})
        if path.endswith('/time'):body={'serverTime':NOW}
        elif '/markets/' in path:
            body=json.loads(json.dumps(state['details']));body['instrument']['epic']=path.rsplit('/',1)[-1]
        elif path.endswith('/markets'):body={'markets':state['rows']}
        elif '/prices/' in path:body=state.get('prices',{'prices':[]})
        else:body={}
        return httpx.Response(200,json=body,headers={'Date':DATE})
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider=CapitalClient('secret-key','fixture-user','secret-password',client=client)
    return provider,state,captured,client


@pytest.mark.asyncio
async def test_only_demo_and_no_order_paths(rig):
    c,_,requests,client=rig
    with pytest.raises(MarketError):await c._get('/positions')
    with pytest.raises(MarketError):await c._send('POST','/workingorders',body={})
    assert not requests
    await client.aclose()


@pytest.mark.asyncio
async def test_no_credentials_blocks_login_without_request():
    c=CapitalClient()
    with pytest.raises(MarketError,match='non configuré'):await c.quote('CAP:SILVER')
    assert await c.catalog()==[]
    await c.close()


@pytest.mark.asyncio
async def test_quote_uses_exchange_utc_timestamp_and_usd(rig):
    c,state,requests,client=rig
    q=await c.quote('CAP:SILVER')
    assert q.time_ms==NOW and q.bid==24 and q.ask==24.02 and q.currency=='USD'
    assert state['auths']==1
    assert requests[0].method=='POST' and json.loads(requests[0].content)['encryptedPassword'] is False
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[
    {'marketStatus':'CLOSED'}, {'delayTime':15},{'delayTime':None},
    {'updateTimeUTC':None},{'updateTimeUTC':'2026-10-24T05:40:00'},
    {'updateTimeUTC':'2026-10-24T05:46:00'}, {'bid':25,'offer':24}, {'bid':float('nan')}])
async def test_bad_quotes_rejected(rig,change):
    c,state,_,client=rig
    state['rows']=[market(**change)]
    with pytest.raises(MarketError):await c.quote('CAP:SILVER')
    await client.aclose()


@pytest.mark.asyncio
async def test_login_error_does_not_expose_secrets(rig):
    c,state,_,client=rig;state['fail_auth']=True
    with pytest.raises(MarketError) as error:await c.quote('CAP:SILVER')
    assert 'secret-' not in str(error.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_expired_session_reauthenticated_once(rig):
    c,state,_,client=rig
    await c.quote('CAP:SILVER');state['fail_once']=True
    await c.quote('CAP:SILVER')
    assert state['auths']==2
    await client.aclose()


@pytest.mark.asyncio
async def test_strict_rules(rig):
    c,state,_,client=rig
    rule=(await c.rules(['CAP:SILVER']))['CAP:SILVER']
    assert rule.step_size==.1 and rule.min_margin_pct==10 and rule.multiplier==1
    assert rule.asset_class=='commodity_cfd' and rule.allow_short is True
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['missing_precision','nonusd','multiplier','unit','expiry'])
async def test_unverifiable_rules_rejected(rig,change):
    c,state,_,client=rig
    if change=='missing_precision':state['details']['dealingRules'].pop('minSizeIncrement')
    if change=='nonusd':state['details']['instrument']['currency']='GBP'
    if change=='multiplier':state['details']['instrument']['lotSize']=50
    if change=='unit':state['details']['instrument']['marginFactorUnit']='POINTS'
    if change=='expiry':state['details']['instrument']['expiry']='DEC-26'
    with pytest.raises(MarketError):await c.rules(['CAP:SILVER'])
    await client.aclose()


@pytest.mark.asyncio
async def test_balanced_catalog_and_closed_labels(rig):
    c,state,_,client=rig
    state['rows']=[market(epic='SILVER'),market(epic='NVDA',instrumentName='NVIDIA',instrumentType='SHARES'),
        market(epic='SPY',instrumentName='SPDR S&P ETF',instrumentType='SHARES'),
        market(epic='US500',instrumentType='INDICES'),market(epic='EURUSD',instrumentType='CURRENCIES'),
        market(epic='CLOSED',marketStatus='CLOSED'),market(epic='FUTURE',expiry='DEC-26')]
    rows=await c.catalog(10)
    assert {r['asset_class'] for r in rows}=={'share_cfd','etf_cfd','index_cfd','forex_cfd','commodity_cfd'}
    assert 'CAP:FUTURE' not in [r['symbol'] for r in rows]
    assert next(r for r in rows if r['symbol']=='CAP:CLOSED')['reason']
    await client.aclose()


@pytest.mark.asyncio
async def test_closed_candles_and_conservative_m1_envelope(rig):
    c,state,_,client=rig
    def bar(t):
        return {'snapshotTimeUTC':t,'openPrice':{'bid':24,'ask':24.02},'closePrice':{'bid':24.01,'ask':24.03},
            'highPrice':{'bid':24.02,'ask':24.04},'lowPrice':{'bid':23.98,'ask':24},'lastTradedVolume':100}
    state['prices']={'prices':[bar('2026-10-24T05:43:00'),bar('2026-10-24T05:44:00'),bar(TIME)]}
    bars=await c.candles('CAP:SILVER','1m')
    assert len(bars)==2
    assert bars[0].low==23.98 and bars[0].high==24.04
    assert bars[-1].close_time==NOW-1
    await client.aclose()


@pytest.mark.asyncio
async def test_no_utc_time_no_freshness_substitution(rig):
    c,state,_,client=rig
    state['rows']=[market(updateTimeUTC=None,updateTime=TIME)]
    with pytest.raises(MarketError):await c.quote('CAP:SILVER')
    await client.aclose()
