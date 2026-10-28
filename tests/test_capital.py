"""Contract-shaped fixtures, not real broker sessions, trades or profitability."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
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


def nested_market(epic='SILVER',**snapshot_changes):
    row=details();row['instrument']['epic']=epic
    row['snapshot'].update(market(epic=epic))
    row['snapshot'].pop('epic')
    row['snapshot'].update(snapshot_changes)
    return row


@pytest.fixture
def rig(monkeypatch):
    captured=[]; state={'details':details(),'rows':[market()],'auths':0,'fail_auth':False,'fail_once':False}
    async def no_sleep(_):pass
    monkeypatch.setattr('app.capital.asyncio.sleep',no_sleep)
    monkeypatch.setattr('app.capital.time.time',lambda:NOW/1000)
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
            epic=path.rsplit('/',1)[-1]
            body=json.loads(json.dumps(state.get('details_by_symbol',{}).get(epic,state['details'])))
            body['instrument']['epic']=epic
        elif path.endswith('/markets'):
            if 'epics' in request.url.params:
                nested=[]
                for row in state['rows']:
                    item=json.loads(json.dumps(state.get('details_by_symbol',{}).get(row['epic'],state['details'])))
                    item['instrument']['epic']=row['epic']
                    item['snapshot'].update({k:v for k,v in row.items() if k not in {'epic','updateTimeUTC'}})
                    item['snapshot']['updateTime']=row.get('updateTimeUTC')
                    nested.append(item)
                body=state.get('epics_response',{'marketDetails':nested})
            else:body=state.get('search_response',{'markets':state['rows']})
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
async def test_quote_preserves_exchange_event_age_on_local_clock_and_usd(rig):
    c,state,requests,client=rig
    q=await c.quote('CAP:SILVER')
    assert q.time_ms==NOW and q.bid==24 and q.ask==24.02 and q.currency=='USD'
    assert state['auths']==1
    assert requests[0].method=='POST' and json.loads(requests[0].content)['encryptedPassword'] is False
    await client.aclose()


@pytest.mark.asyncio
async def test_official_epics_envelope_uses_fresh_utc_search_not_ambiguous_snapshot_or_cached_price(rig):
    c,state,requests,client=rig
    snapshot=nested_market(updateTimeUTC=None,updateTime='2026-10-24T08:45:00',bid=999.,offer=999.02)
    state['epics_response']={'marketDetails':[snapshot]}
    q=await c.quote('CAP:SILVER')
    assert (q.bid,q.ask,q.time_ms)==(24.,24.02,NOW)
    assert [dict(r.url.params) for r in requests if r.url.path.endswith('/markets')]==[
        {'epics':'SILVER'},{'searchTerm':'SILVER'}]
    assert not any('/markets/' in r.url.path for r in requests)
    # A subsequent price request still fetches observations; the metadata cache
    # must not turn its old snapshot price into a new executable quote.
    state['rows']=[market(bid=25.,offer=25.02)]
    q=await c.quote('CAP:SILVER')
    assert (q.bid,q.ask)==(25.,25.02)
    assert len([r for r in requests if r.url.params.get('searchTerm')=='SILVER'])==2
    await client.aclose()


@pytest.mark.asyncio
async def test_epics_snapshot_with_explicit_utc_is_validated_directly_and_warms_rules_cache(rig):
    c,state,requests,client=rig
    state['epics_response']={'marketDetails':[nested_market(bid=25.,offer=25.02)]}
    q=await c.quote('CAP:SILVER')
    assert (q.bid,q.ask,q.time_ms)==(25.,25.02,NOW)
    assert (await c.rules(['CAP:SILVER']))['CAP:SILVER'].currency=='USD'
    assert [dict(r.url.params) for r in requests if r.url.path.endswith('/markets')]==[{'epics':'SILVER'}]
    assert not any('/markets/' in r.url.path for r in requests)
    await client.aclose()


@pytest.mark.asyncio
async def test_epics_batch_failures_are_independent_and_duplicate_or_missing_contracts_never_quote(rig):
    c,state,_,client=rig
    state['epics_response']={'marketDetails':[nested_market(),nested_market('CLOSED',marketStatus='CLOSED'),
        nested_market('DUPLICATE'),nested_market('DUPLICATE')]}
    rows=await c.quotes(['CAP:SILVER','CAP:CLOSED','CAP:DUPLICATE','CAP:MISSING'])
    assert rows['CAP:SILVER'].bid==24.
    assert isinstance(rows['CAP:CLOSED'],MarketError) and 'fermé' in str(rows['CAP:CLOSED'])
    assert isinstance(rows['CAP:DUPLICATE'],MarketError) and 'dupliqué' in str(rows['CAP:DUPLICATE'])
    assert isinstance(rows['CAP:MISSING'],MarketError) and 'absent' in str(rows['CAP:MISSING'])
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('response',[{}, {'markets':[market()]}, {'marketDetails':{}}, {'marketDetails':None}])
async def test_unknown_epics_envelope_is_refused_without_search_fallback(rig,response):
    c,state,requests,client=rig;state['epics_response']=response
    with pytest.raises(MarketError,match='marketDetails requis'):await c.quote('CAP:SILVER')
    assert not any('searchTerm' in r.url.params for r in requests)
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('response,message',[
    ({'marketDetails':[]},'tableau markets requis'),
    ({'markets':[market(epic='SILVER_OTHER')]},'exacte absente'),
    ({'markets':[market(),market()]},'dupliquée'),
    ({'markets':[market(updateTimeUTC=None,updateTime=TIME)]},'incomplète'),
    ({'markets':[market(updateTimeUTC='2026-10-24T05:40:00')]},'trop ancienne')])
async def test_utc_search_fallback_requires_exact_unique_fresh_explicit_utc_quote(rig,response,message):
    c,state,_,client=rig;state['search_response']=response
    with pytest.raises(MarketError,match=message):await c.quote('CAP:SILVER')
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
async def test_catalog_replaces_unsupported_contracts_until_usable_slots_are_filled(rig):
    c,state,requests,client=rig
    state['rows']=[market(epic=s,instrumentType='INDICES',instrumentName=s)
        for s in ['US500','US100','US30','VALID_NEXT']]
    nonusd=details();nonusd['instrument']['currency']='EUR'
    multiplier=details();multiplier['instrument']['lotSize']=10
    state['details_by_symbol']={'US500':nonusd,'US100':multiplier}
    rows=await c.catalog(2)
    assert [r['symbol'] for r in rows if r['supported']]==['CAP:US30','CAP:VALID_NEXT']
    blocked={r['symbol']:r['reason'] for r in rows if not r['supported']}
    assert 'USD' in blocked['CAP:US500'] and 'Multiplicateur' in blocked['CAP:US100']
    assert len([r for r in requests if '/markets/' in r.url.path])==4
    assert c.metadata['CAP:US500']['supported'] is False
    assert c.metadata['CAP:US30']['supported'] is True
    await client.aclose()


@pytest.mark.asyncio
async def test_catalog_replacements_preserve_family_balance_and_detail_cache(rig):
    c,state,requests,client=rig
    families=[('INDICES','US500','US100'),('SHARES','AAPL','MSFT'),
        ('ETF','SPY','QQQ'),('CURRENCIES','EURUSD','GBPUSD'),('COMMODITIES','GOLD','SILVER')]
    state['rows']=[];state['details_by_symbol']={}
    for kind,bad,good in families:
        for epic in (bad,good):
            state['rows'].append(market(epic=epic,instrumentType=kind,instrumentName=epic))
            row=details();row['instrument']['type']=kind
            if epic==bad:row['instrument']['currency']='EUR'
            state['details_by_symbol'][epic]=row
    rows=await c.catalog(5)
    usable=[r for r in rows if r['supported']]
    assert len(usable)==5 and {r['asset_class'] for r in usable}=={
        'index_cfd','share_cfd','etf_cfd','forex_cfd','commodity_cfd'}
    assert {r['symbol'] for r in usable}=={'CAP:'+good for _,_,good in families}
    assert len([r for r in rows if not r['supported']])==5
    first_details=[r.url.path for r in requests if '/markets/' in r.url.path]
    assert len(first_details)==10
    # Refreshing the catalogue must not invalidate the existing 300s detail cache.
    assert len([r for r in await c.catalog(5) if r['supported']])==5
    assert [r.url.path for r in requests if '/markets/' in r.url.path]==first_details
    await client.aclose()


@pytest.mark.asyncio
async def test_catalog_unsupported_validation_budget_is_bounded(rig):
    c,state,requests,client=rig
    state['details']['instrument']['currency']='EUR'
    state['rows']=[market(epic=f'UNSUPPORTED_{i:03}',instrumentType='INDICES') for i in range(50)]
    rows=await c.catalog(3)
    assert not any(r['supported'] for r in rows)
    assert len(rows)==15
    assert len([r for r in requests if '/markets/' in r.url.path])==15
    assert all('USD' in r['reason'] for r in rows)
    await client.aclose()


@pytest.mark.asyncio
async def test_closed_or_delayed_catalog_rows_are_diagnostic_without_detail_requests(rig):
    c,state,requests,client=rig
    state['rows']=[market(epic='CLOSED',marketStatus='CLOSED'),
        market(epic='DELAYED',delayTime=15),market(epic='SILVER')]
    rows=await c.catalog(3)
    assert [r['symbol'] for r in rows if r['supported']]==['CAP:SILVER']
    blocked={r['symbol']:r['reason'] for r in rows if not r['supported']}
    assert 'fermé' in blocked['CAP:CLOSED'] and 'retardées' in blocked['CAP:DELAYED']
    paths=[r.url.path.rsplit('/',1)[-1] for r in requests if '/markets/' in r.url.path]
    assert paths==['SILVER']
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


@pytest.mark.asyncio
@pytest.mark.parametrize('broker_offset',[30000,-30000])
async def test_quote_normalizes_clock_offset_without_erasing_event_age(rig,monkeypatch,broker_offset):
    from app.capital import _timestamp
    c,state,_,client=rig
    broker_now=NOW+broker_offset
    event_at=broker_now-10000
    state['rows']=[market(updateTimeUTC=datetime.fromtimestamp(event_at/1000,timezone.utc).isoformat())]
    async def clock():return broker_now
    monkeypatch.setattr(c,'server_time',clock)
    quote=await c.quote('CAP:SILVER')
    assert quote.time_ms==NOW-10000
    assert NOW-quote.time_ms==broker_now-_timestamp(state['rows'][0]['updateTimeUTC'])
    await client.aclose()


@pytest.mark.asyncio
async def test_slight_broker_future_tolerance_never_produces_local_future_quote(rig,monkeypatch):
    c,state,_,client=rig
    state['rows']=[market(updateTimeUTC=datetime.fromtimestamp((NOW+800)/1000,timezone.utc).isoformat())]
    async def clock():return NOW
    monkeypatch.setattr(c,'server_time',clock)
    assert (await c.quote('CAP:SILVER')).time_ms==NOW
    await client.aclose()


@pytest.mark.asyncio
async def test_metadata_lookup_cannot_refresh_quote_that_became_stale(rig,monkeypatch):
    c,state,_,client=rig
    clock={'now':NOW}
    state['rows']=[market(updateTimeUTC=datetime.fromtimestamp((NOW-10000)/1000,timezone.utc).isoformat())]
    async def server_time():return clock['now']
    async def delayed_details(symbol):
        clock['now']+=6000
        return details()
    monkeypatch.setattr(c,'server_time',server_time)
    monkeypatch.setattr(c,'_detail',delayed_details)
    with pytest.raises(MarketError,match='trop ancienne'):await c._quote_row('CAP:SILVER',state['rows'][0])
    await client.aclose()


@pytest.mark.asyncio
async def test_slow_metadata_does_not_block_quote_and_request_starts_stay_paced(monkeypatch):
    clock={'now':100.}
    starts=[];delays=[]
    metadata_started=asyncio.Event();release_metadata=asyncio.Event()
    async def paced_sleep(delay):
        delays.append(delay)
        clock['now']+=delay
        await asyncio.sleep(0)
    # Keep the event loop's real clock intact while advancing provider pacing.
    monkeypatch.setattr('app.capital.time',SimpleNamespace(
        monotonic=lambda:clock['now'],time=lambda:NOW/1000))
    monkeypatch.setattr('app.capital.asyncio',SimpleNamespace(Lock=asyncio.Lock,sleep=paced_sleep))
    async def handler(request):
        starts.append((request.url.path,clock['now']))
        if request.url.path.endswith('/markets/NVDA'):
            metadata_started.set()
            await release_metadata.wait()
            body=details();body['instrument']['epic']='NVDA'
        else:
            assert request.url.path.endswith('/markets')
            if 'epics' in request.url.params:
                assert request.url.params['epics']=='SILVER'
                detail=details();detail['snapshot'].update(market())
                body={'marketDetails':[detail]}
            else:
                assert request.url.params['searchTerm']=='SILVER'
                body={'markets':[market()]}
        return httpx.Response(200,json=body,headers={'Date':DATE})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        c=CapitalClient('fixture-key','fixture-user','fixture-password',client=client)
        c._tokens={'CST':'fixture-cst','X-SECURITY-TOKEN':'fixture-security'}
        c._details['CAP:SILVER']=details();c._details_at['CAP:SILVER']=clock['now']
        metadata_task=asyncio.create_task(c.rules(['CAP:NVDA']))
        try:
            await asyncio.wait_for(metadata_started.wait(),timeout=.5)
            quote=await asyncio.wait_for(c.quote('CAP:SILVER'),timeout=.5)
            assert quote.symbol=='CAP:SILVER' and quote.bid==24.
            assert not metadata_task.done() and not release_metadata.is_set()
            assert len(starts)==2 and starts[1][1]-starts[0][1]>=.26-1e-12
            assert delays==pytest.approx([.26])
        finally:
            release_metadata.set()
            await metadata_task

