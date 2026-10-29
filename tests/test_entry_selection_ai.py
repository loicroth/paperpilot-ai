import copy
import json

import httpx
import pytest

from app.ai import AIClient, decision_schema, ENTRY_SELECTION_PROMPT, SYSTEM_PROMPT, prompt_for


def context(short=False):
    symbol = 'CAP:SILVER' if short else 'CAP:IWM'
    side = 'SHORT' if short else 'BUY'
    plan = dict(id=f'{symbol}:{side}:retest', symbol=symbol, action=side, setup='retest',
                entry=100., stop_loss=102. if short else 98.,
                take_profit=94. if short else 106., invalidation=101. if short else 99.)
    return dict(mode='entry', symbols=[symbol], positions=[],
                portfolio={'open_count': 0, 'max_positions': 3},
                rules={'cfd_enabled': True, 'allow_short': True},
                candidates={symbol: ['retest']}, directions={symbol: [side]},
                direction_candidates={symbol: {side: ['retest']}}, entry_plans=[plan])


def selection(ctx, identifier=None, risk_pct=None):
    return dict(choice=dict(id=identifier or ctx['entry_plans'][0]['id'], risk_pct=risk_pct),
                reason='Retest clôturé et tendance H1 confirmée.',
                confirmation='Reprise déjà observée.', risks=['Fausse reprise possible.'])


async def decide(ctx, selected):
    def handler(request):
        payload = json.loads(request.content)
        assert payload['instructions']==ENTRY_SELECTION_PROMPT
        observed=json.loads(payload['input'][0]['content'][0]['text'])
        assert observed['entry_plans']==ctx['entry_plans']
        schema = payload['text']['format']['schema']
        assert set(schema['required']) == {'choice', 'reason', 'confirmation', 'risks'}
        assert schema['additionalProperties'] is False
        assert all(branch['additionalProperties'] is False
                   for branch in schema['properties']['choice']['anyOf'])
        body = dict(status='completed', usage=dict(input_tokens=100, output_tokens=50),
                    output=[dict(type='message', content=[dict(type='output_text', text=json.dumps(selected))])])
        return httpx.Response(200, json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        return await AIClient('fixture-key', client=http).decide(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize('short', [False, True])
async def test_model_selects_observed_plan_and_cannot_generate_wrong_invalidation(short):
    ctx = context(short)
    saved = copy.deepcopy(ctx)
    result = await decide(ctx, selection(ctx, risk_pct=1.2))
    assert result.error is None
    plan = ctx['entry_plans'][0]
    for field in ('symbol', 'action', 'setup', 'entry', 'stop_loss', 'take_profit', 'invalidation'):
        assert getattr(result.decision, field) == plan[field]
    assert result.decision.confirmation_met and result.decision.risk_pct == 1.2
    assert ctx == saved


@pytest.mark.asyncio
async def test_wait_remains_available_without_opening_any_plan():
    ctx = context()
    result = await decide(ctx, selection(ctx, 'WAIT'))
    assert result.error is None and result.decision.action == 'WAIT'
    assert result.decision.setup == 'none' and not result.decision.confirmation_met
    assert result.decision.invalidation is None and result.decision.risk_pct is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unknown', 'extra_price', 'old_contract', 'wait_risk', 'oversized_risk'])
async def test_provider_cannot_modify_plan_or_bypass_local_selection_validation(change):
    ctx = context()
    data = selection(ctx)
    if change == 'unknown': data['choice']['id'] = 'invented'
    if change == 'extra_price': data['invalidation'] = 100.
    if change == 'old_contract': data = dict(ctx['entry_plans'][0], reason='invented')
    if change == 'wait_risk': data['choice'] = dict(id='WAIT', risk_pct=1.)
    if change == 'oversized_risk': data['choice']['risk_pct'] = 2.
    result = await decide(ctx, data)
    assert result.error_kind == 'invalid_decision' and result.decision.action == 'WAIT'


@pytest.mark.asyncio
async def test_invalid_internal_plan_blocks_before_paid_call():
    ctx = context()
    ctx['entry_plans'][0]['invalidation'] = 100.
    def handler(request):
        raise AssertionError('No request for an incoherent internal plan')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await AIClient('fixture-key', client=http).decide(ctx)
    assert result.error and result.decision.action == 'WAIT'


def test_schema_wait_branch_forbids_risk_and_plan_branch_uses_exact_ids():
    ctx = context()
    schema = decision_schema(ctx)
    branches = schema['properties']['choice']['anyOf']
    assert branches[0]['properties'] == {'id': {'type': 'string', 'enum': ['WAIT']}, 'risk_pct': {'type': 'null'}}
    assert branches[1]['properties']['id']['enum'] == [ctx['entry_plans'][0]['id']]
    assert not {'entry', 'stop_loss', 'take_profit', 'invalidation'} & set(schema['properties'])
    assert '$defs' not in schema


def test_entry_context_without_plans_keeps_legacy_decision_contract():
    ctx = context()
    ctx.pop('entry_plans')
    assert 'action' in decision_schema(ctx)['properties']


@pytest.mark.asyncio
async def test_startup_diagnostic_checks_new_selection_format_with_wait_only():
    ctx = context()
    ctx.update(diagnostic_only=True, symbols=[], candidates={}, directions={},
               direction_candidates={}, entry_plans=[])
    schema = decision_schema(ctx)
    assert len(schema['properties']['choice']['anyOf']) == 1
    result = await decide(ctx, selection(ctx, 'WAIT'))
    assert result.error is None and result.decision.action == 'WAIT'


def test_selection_and_legacy_price_generation_instructions_are_separate():
    assert prompt_for(context())==ENTRY_SELECTION_PROMPT
    assert 'Propose une invalidation structurelle' not in ENTRY_SELECTION_PROMPT
    assert 'entry_plans' in ENTRY_SELECTION_PROMPT
    assert prompt_for({'mode':'management'})==SYSTEM_PROMPT
    assert prompt_for({})==SYSTEM_PROMPT
