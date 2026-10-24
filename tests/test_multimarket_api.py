"""Credential/router/FX tests. No live broker authentication or trades."""
import json
import os
import stat
import time
from types import SimpleNamespace

import httpx
import pytest

from app.market import MarketError, Quote
from app.multimarket import DollarConversion, MultiMarket
from app.web import create_app
from test_web import web


class BinanceFixture:
    def __init__(self,fail=False):self.fail=fail
    async def server_time(self):
        if self.fail:raise MarketError('Fixture Binance unavailable')
        return 1800000000000
    async def close(self):pass


class FXFixture:
    cached=None;error=None
    async def close(self):pass


class CapitalFixture:
    instances=[]
    def __init__(self,api_key='',identifier='',api_password=''):
        self.api_key=api_key;self.identifier=identifier;self.api_password=api_password
        self.configured=bool(api_key and identifier and api_password);self.closed=False
        self.__class__.instances.append(self)
    async def server_time(self):return 1800000000123
    async def catalog(self,limit=40):
        if self.api_password=='REJECT':raise MarketError('Intentionally rejected fixture')
        return [{'symbol':'CAP:NVDA','label':'NVIDIA CFD','asset_class':'share_cfd',
                 'supported':True,'market_status':'TRADEABLE'}]
    async def close(self):self.closed=True


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    for k in ('CAPITAL_API_KEY','CAPITAL_IDENTIFIER','CAPITAL_API_PASSWORD'):monkeypatch.delenv(k,raising=False)
    monkeypatch.setattr('app.multimarket.CapitalClient',CapitalFixture)
    CapitalFixture.instances=[]
    env=SimpleNamespace(data_dir=tmp_path,binance_url='https://data-api.binance.vision')
    m=MultiMarket(env,binance=BinanceFixture(),fx=FXFixture())
    return env,m


@pytest.mark.asyncio
async def test_confirmed_credentials_saved_0600_reloaded_and_absent_from_status(fixture):
    env,m=fixture
    credentials={'api_key':'test-secret-key','identifier':'fixture@example.invalid','api_password':'test-secret-password'}
    old=m.capital
    await m.setup_capital(credentials)
    assert old.closed
    p=m.credentials_path
    assert p.is_file() and not p.is_symlink()
    assert stat.S_IMODE(p.stat().st_mode)==0o600
    assert json.loads(p.read_text())==credentials
    state=json.dumps(m.status())
    assert all(value not in state for value in credentials.values())
    reloaded=MultiMarket(env,binance=BinanceFixture(),fx=FXFixture())
    assert reloaded.capital.configured and reloaded.capital.api_key==credentials['api_key']
    assert reloaded.capital.identifier==credentials['identifier']
    assert reloaded.capital.api_password==credentials['api_password']
    await reloaded.close();await m.close()


@pytest.mark.asyncio
async def test_rejected_auth_does_not_replace_valid_connection_or_file(fixture):
    _,m=fixture
    original={'api_key':'first','identifier':'fixture@example.invalid','api_password':'valid-fixture'}
    await m.setup_capital(original);saved=m.credentials_path.read_bytes();active=m.capital
    with pytest.raises(MarketError) as error:
        await m.setup_capital({'api_key':'new','identifier':'new@example.invalid','api_password':'REJECT'})
    assert 'REJECT' not in str(error.value)
    assert m.capital is active and not active.closed
    assert m.credentials_path.read_bytes()==saved
    assert CapitalFixture.instances[-1].closed
    await m.close()


def test_symlink_destination_cannot_be_read_or_written(fixture,tmp_path):
    env,m=fixture
    outside=tmp_path/'do-not-overwrite.json';outside.write_text('{"fixture":"unchanged"}')
    m.credentials_path.symlink_to(outside)
    with pytest.raises(ValueError):m._save({'api_key':'forbidden'})
    with pytest.raises(ValueError):MultiMarket(env,binance=BinanceFixture(),fx=FXFixture())
    assert outside.read_text()=='{"fixture":"unchanged"}'


@pytest.mark.asyncio
async def test_capital_clock_independent_during_binance_outage(fixture):
    env,m=fixture;m.binance.fail=True
    with pytest.raises(MarketError):await m.server_time()
    await m.setup_capital({'api_key':'fixture','identifier':'user','api_password':'fixture-password'})
    assert await m.server_time()==1800000000123
    await m.close()


@pytest.mark.asyncio
async def test_fx_uses_inverse_bidask_and_reuses_short_cache():
    requests=[]
    async def handler(request):
        requests.append(request)
        assert str(request.url).startswith('https://api.kraken.com/0/public/Ticker')
        assert request.url.params['pair']=='USDTUSD'
        return httpx.Response(200,json={'error':[],'result':{'USDTZUSD':{'b':['.99'],'a':['1.01']}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fx=DollarConversion(client)
        rates=await fx.rates()
        assert rates['bid']==pytest.approx(1/1.01)
        assert rates['ask']==pytest.approx(1/.99)
        assert rates['bid']<rates['ask']
        assert 'réception REST' in rates['source']
        assert await fx.rates() is rates and len(requests)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[
    {'error':['fixture unavailable'],'result':{}},
    {'error':[],'result':{}},
    {'error':[],'result':{'A':{},'B':{}}},
    {'error':[],'result':{'A':{'b':['1.1'],'a':['1.0']}}},
    {'error':[],'result':{'A':{'b':['0'],'a':['1']}}},
    {'error':[],'result':{'A':{'b':['NaN'],'a':['1']}}},
    {'error':[],'result':{'A':{'b':['.1'],'a':['.2']}}},
])
async def test_fx_invalid_or_unavailable_blocks_without_stale_fallback(payload):
    async def handler(request):return httpx.Response(200,json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fx=DollarConversion(client);fx.cached={'bid':1.,'ask':1.,'at':int(time.time()*1000)-3000}
        with pytest.raises(MarketError,match='Conversion'):await fx.rates()
        assert fx.error


@pytest.mark.asyncio
async def test_fx_network_error_sanitized():
    async def handler(request):raise httpx.ConnectError('secret transport details',request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fx=DollarConversion(client)
        with pytest.raises(MarketError) as error:await fx.rates()
        assert 'secret' not in str(error.value)


@pytest.mark.asyncio
async def test_setup_route_requires_auth_csrf_and_never_echoes_secret(web,monkeypatch):
    web.env.password='fixture-long-password-1234';web.env.public_origin='https://paperpilot.example'
    captured=[]
    async def setup(credentials):captured.append(credentials)
    monkeypatch.setattr(web.market,'setup_capital',setup,raising=False)
    app=create_app(env=web.env,store=web.store,engine=web.bot,background=False)
    credentials={'api_key':'route-secret-key','identifier':'route-user@example.invalid','api_password':'route-secret-password'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://internal') as c:
        assert (await c.post('/api/providers/capital/setup',json=credentials)).status_code==401
        c.auth=('paperpilot',web.env.password)
        assert (await c.post('/api/providers/capital/setup',json=credentials)).status_code==403
        token=(await c.get('/api/state')).json()['csrf_token']
        headers={'X-CSRF-Token':token,'Origin':web.env.public_origin}
        response=await c.post('/api/providers/capital/setup',json=credentials,headers=headers)
        assert response.status_code==200 and response.json()=={'configured':True}
        assert captured==[credentials]
        assert all(value not in response.text for value in credentials.values())
        text=(await c.get('/api/state')).text
        assert all(value not in text for value in credentials.values())
        headers['Origin']='https://attacker.example'
        assert (await c.post('/api/providers/capital/setup',json=credentials,headers=headers)).status_code==403
