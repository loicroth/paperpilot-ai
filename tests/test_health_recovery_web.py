"""Recovery controls must preserve the independently observed API health."""
import time

import pytest

from test_web import web, client, token


@pytest.mark.asyncio
@pytest.mark.parametrize('verified', [False, True])
async def test_local_analysis_failure_and_start_preserve_known_ai_health(web, monkeypatch, verified):
    web.env.api_key='fixture-not-a-real-key'
    web.bot.health.update(ai_ok=verified,ai_error=None,ai_error_kind=None)
    web.bot.reconciled=True

    async def broken_analysis():
        raise KeyError('symbol')

    monkeypatch.setattr(web.bot,'_analyze',broken_analysis)
    await web.bot.analyze()
    assert web.store.get('mode')=='paused'
    assert "KeyError: 'symbol'" in web.bot.critical_error
    assert web.bot.health['status']=='ERREUR'
    assert web.bot.health['ai_ok'] is verified
    assert web.bot.health.get('ai_error') is None
    assert web.bot.health.get('ai_error_kind') is None

    async with client(web) as c:
        headers=await token(c)
        response=await c.post('/api/control',json={'action':'start'},headers=headers)
        assert response.status_code==200
        assert web.bot.critical_error is None
        # A successful quote tick must not misclassify a resolved local error as
        # lost API verification, or manufacture verification which never ran.
        web.bot.last_analysis_poll=time.monotonic()
        await web.bot.tick()
        state=(await c.get('/api/state')).json()
    assert state['control']['mode']=='running'
    assert state['health']['ai_ok'] is verified
    assert state['health']['status']=='ACTIVE'
    if verified:
        assert 'non encore vérifiée' not in state['health']['message']
    else:
        assert 'non encore vérifiée' in state['health']['message']
    assert not web.store.positions()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['authentication', 'limits', 'invalid_decision'])
async def test_start_does_not_clear_a_real_ai_error(web, kind):
    web.env.api_key='fixture-not-a-real-key'
    message='API failure fixture: '+kind
    web.bot.health.update(ai_ok=False,ai_error=message,ai_error_kind=kind)
    web.bot.critical_error='Independent local engine failure fixture'
    web.bot.reconciled=True
    async with client(web) as c:
        headers=await token(c)
        response=await c.post('/api/control',json={'action':'start'},headers=headers)
        assert response.status_code==200
        web.bot.last_analysis_poll=time.monotonic()
        await web.bot.tick()
        health=(await c.get('/api/state')).json()['health']
    assert web.bot.critical_error is None
    assert health['ai_ok'] is False
    assert health['ai_error']==message
    assert health['ai_error_kind']==kind
    assert message in health['message']
    assert health['status']==('ACTIVE · RÉPONSE IA REJETÉE' if kind=='invalid_decision' else 'ERREUR')
    assert not web.store.positions()
