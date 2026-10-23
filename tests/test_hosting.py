import httpx
import pytest
from app.web import create_app
from app.config import Environment
from test_web import web

@pytest.mark.asyncio
async def test_hosted_origin_and_health(web):
    web.env.password='test-only-password-123456'
    web.env.public_origin='https://paperpilot.example'
    app=create_app(env=web.env,store=web.store,engine=web.bot,background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://internal') as c:
        assert (await c.get('/healthz')).json()=={'ok':True}
        assert (await c.get('/api/state')).status_code==401
        c.auth=('paperpilot',web.env.password)
        token=(await c.get('/api/state')).json()['csrf_token']
        headers={'X-CSRF-Token':token,'Origin':web.env.public_origin}
        assert (await c.post('/api/control',headers=headers,json={'action':'pause'})).status_code==200
        headers['Origin']='https://attacker.example'
        assert (await c.post('/api/control',headers=headers,json={'action':'start'})).status_code==403

def test_render_origin(monkeypatch):
    monkeypatch.delenv('PUBLIC_ORIGIN',raising=False)
    monkeypatch.setenv('RENDER_EXTERNAL_URL','https://example.onrender.com')
    assert Environment().public_origin=='https://example.onrender.com'
    monkeypatch.setenv('PUBLIC_ORIGIN','https://example.com/path')
    with pytest.raises(ValueError): Environment()
