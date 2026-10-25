import json
import httpx
import pytest
from app.ai import AIClient, Decision, MAX_OUTPUT_TOKENS, wait


def proposal(**changes):
    data = dict(symbol="BTCUSDT", action="BUY", reason="Cassure confirmée avec volume.",
                entry=100.0, stop_loss=98.0, take_profit=106.0, invalidation=98.0,
                confirmation="Clôture au-dessus de la résistance.", risks=["Faux breakout"],
                setup="breakout", confirmation_met=True)
    data.update(changes)
    return data


def context(**changes):
    data = dict(symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                candidates={"BTCUSDT": ["breakout"]}, positions=[], rules={})
    data.update(changes)
    return data


def response_body(decision=None, **changes):
    body = dict(status="completed", usage=dict(input_tokens=123, output_tokens=54),
                output=[dict(type="message", content=[dict(type="output_text", text=json.dumps(decision or proposal()))])])
    body.update(changes)
    return body


@pytest.mark.asyncio
async def test_missing_key_blocks_without_network():
    def handler(request):
        raise AssertionError("No paid call allowed")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await AIClient("", client=http).decide(context())
    assert result.decision.action == "WAIT"
    assert "IA non configurée" in result.error
    assert result.input_tokens == result.output_tokens == 0


@pytest.mark.asyncio
async def test_structured_response_payload_and_usage():
    def handler(request):
        assert request.url == "https://api.openai.com/v1/responses"
        payload = json.loads(request.content)
        assert payload["store"] is False
        assert payload["text"]["format"]["strict"] is True
        schema = payload["text"]["format"]["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(Decision.model_fields)
        assert payload["max_output_tokens"] <= MAX_OUTPUT_TOKENS
        return httpx.Response(200, json=response_body())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await AIClient("test-key-never-live", client=http).decide(context())
    assert result.error is None
    assert result.decision.action == "BUY"
    assert (result.input_tokens, result.output_tokens) == (123, 54)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    proposal(stop_loss=105.0), proposal(entry=float("nan")), proposal(entry=float("inf")),
    proposal(confirmation_met=False), proposal(setup="none"), proposal(invalidation=None),
    proposal(reason=" "), proposal(unknown="extra"), proposal(entry="100"),
    proposal(action="WAIT"), {"action": "BUY"}, proposal(symbol="DOGEUSDT"),
    proposal(setup="reversal"), proposal(action="SELL", symbol="ETHUSDT"),
    proposal(action="HOLD", entry=None, stop_loss=None, take_profit=None, invalidation=None),
])
async def test_invalid_decisions_never_trade(data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response_body(data)))) as http:
        result = await AIClient("fixture-key", client=http).decide(context())
    assert result.decision.action == "WAIT"
    assert result.error


@pytest.mark.asyncio
async def test_sell_only_held_asset():
    sell = proposal(action="SELL", entry=None, stop_loss=None, take_profit=None,
                    invalidation=None, setup="none", confirmation_met=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response_body(sell)))) as http:
        result = await AIClient("fixture-key", client=http).decide(context(positions=[{"symbol": "BTCUSDT", "quantity": 0.1}]))
    assert result.decision.action == "SELL" and result.error is None


@pytest.mark.asyncio
async def test_no_averaging_down():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response_body()))) as http:
        result = await AIClient("fixture-key", client=http).decide(context(positions=[{"symbol": "BTCUSDT", "quantity": 0.1}]))
    assert result.decision.action == "WAIT" and result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_errors_wait(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"error": {"message": "do not expose"}}))) as http:
        result = await AIClient("fixture-key", client=http).decide(context())
    assert result.decision.action == "WAIT" and result.error
    assert "do not expose" not in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    response_body(status="incomplete"),
    response_body(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]),
    response_body(output=[]),
    response_body(output=[{"type": "message", "content": [{"type": "output_text", "text": "bad json"}]}]),
    response_body(output=[{"type": "message", "content": [{"type": "output_text", "text": "{}"}, {"type": "output_text", "text": "{}"}]}]),
])
async def test_incomplete_refusal_or_ambiguous_response_wait(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as http:
        result = await AIClient("fixture-key", client=http).decide(context())
    assert result.decision.action == "WAIT" and result.error


@pytest.mark.asyncio
async def test_timeout_wait():
    def handler(request):
        raise httpx.ReadTimeout("fixture timeout", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await AIClient("fixture-key", client=http).decide(context())
    assert result.decision.action == "WAIT"
    assert "délai" in result.error


@pytest.mark.asyncio
async def test_invalid_input_blocked_before_paid_request():
    def handler(request):
        raise AssertionError("No paid call for nonfinite market data")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await AIClient("fixture-key", client=http).decide(context(price=float("nan")))
    assert result.decision.action == "WAIT" and result.error


def test_wait_safety_object_has_no_execution_levels():
    decision = wait("Clé absente")
    assert decision.symbol == "ALL" and decision.action == "WAIT"
    assert all(getattr(decision, key) is None for key in ("entry", "stop_loss", "take_profit", "invalidation"))


@pytest.mark.asyncio
async def test_duplicate_json_keys_are_ambiguous_and_blocked():
    text = json.dumps(proposal())
    text = text.replace('"action": "BUY"', '"action": "WAIT", "action": "BUY"')
    body = response_body(output=[{"type": "message", "content": [{"type": "output_text", "text": text}]}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as http:
        result = await AIClient("fixture-key", client=http).decide(context())
    assert result.decision.action == "WAIT" and result.error


def test_buy_invalidation_at_entry_is_rejected_before_execution():
    from app.ai import Decision
    with pytest.raises(ValueError):
        Decision(symbol='TRXUSDT',action='BUY',reason='fixture',entry=.3302,stop_loss=.3299,take_profit=.3307,invalidation=.3302,confirmation='fixture',risks=[],setup='reversal',confirmation_met=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('data,context_changes,stage,code', [
    (proposal(invalidation=100.), {}, 'decision_schema', 'buy_invalidation'),
    (proposal(stop_loss=105.), {}, 'decision_schema', 'buy_levels_inconsistent'),
    (proposal(action='WAIT'), {}, 'decision_schema', 'wait_hold_levels'),
    (proposal(action='WAIT', entry=None, stop_loss=None, take_profit=None, invalidation=None), {}, 'decision_schema', 'wait_inconsistent'),
    (proposal(action='HOLD', entry=None, stop_loss=None, take_profit=None, invalidation=None), {}, 'decision_context', 'position_not_held'),
    (proposal(action='SELL', entry=None, stop_loss=None, take_profit=None, invalidation=None), {}, 'decision_context', 'position_not_held'),
    (proposal(symbol='DOGEUSDT'), {}, 'decision_context', 'unknown_symbol'),
    (proposal(setup='reversal'), {}, 'decision_context', 'setup_not_admitted'),
    (proposal(), {'directions': {'BTCUSDT': ['SHORT']}}, 'decision_context', 'direction_not_admitted'),
    (proposal(), {'direction_candidates': {'BTCUSDT': {'SHORT': ['breakout']}}}, 'decision_context', 'setup_direction_mismatch'),
    (proposal(action='SHORT', stop_loss=102., take_profit=94., invalidation=101.), {}, 'decision_context', 'short_forbidden'),
    (proposal(), {'positions': [{'symbol': 'BTCUSDT', 'quantity': 1.}]}, 'decision_context', 'position_already_open'),
])
async def test_rejected_proposals_have_precise_controlled_diagnostics(data, context_changes, stage, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response_body(data)))) as http:
        result = await AIClient('fixture-key', client=http).decide(context(**context_changes))
    assert result.error_kind == 'invalid_decision'
    assert result.diagnostic['stage'] == stage and result.diagnostic['code'] == code
    assert result.diagnostic['reason'] in result.error
    assert result.decision.action == 'WAIT' and result.error
    assert all(getattr(result.decision, k) is None for k in ('entry', 'stop_loss', 'take_profit', 'invalidation'))
    assert (result.input_tokens, result.output_tokens) == (123, 54)
    assert len(result.error) <= 400 and len(result.diagnostic['reason']) <= 200


@pytest.mark.asyncio
@pytest.mark.parametrize('body,code', [
    (response_body(status='incomplete'), 'response_incomplete'),
    (response_body(status='incomplete', incomplete_details={'reason': 'max_output_tokens'}), 'response_output_limit'),
    (response_body(status='incomplete', incomplete_details={'reason': 'content_filter'}), 'response_filtered'),
    (response_body(output=[]), 'missing_output'),
    (response_body(output=[{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'private refusal'}]}]), 'response_refused'),
    (response_body(output=[{'type': 'message', 'content': [{'type': 'output_text', 'text': 'private invalid json'}]}]), 'invalid_json'),
    (response_body(output=[{'type': 'message', 'content': [{'type': 'output_text', 'text': '{}'}, {'type': 'output_text', 'text': '{}'}]}]), 'ambiguous_output'),
])
async def test_response_failure_categories_are_distinct(body, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as http:
        result = await AIClient('fixture-key', client=http).decide(context())
    assert result.error_kind == 'invalid_decision' and result.diagnostic['code'] == code
    assert result.decision.action == 'WAIT' and result.error
    assert (result.input_tokens, result.output_tokens) == (123, 54)
    assert 'private' not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('data', [
    proposal(entry='sk-private-payload', reason='sk-private-payload'),
    proposal(invalidation=100., reason='sk-private-payload'),
    proposal(**{'sk-private-payload': 'sk-private-payload'}),
    proposal(risks=['sk-private-payload'] * 11),
    proposal(symbol='sk-private-payload'),
])
async def test_validation_diagnostics_never_include_rejected_values_or_unknown_field_names(data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response_body(data)))) as http:
        result = await AIClient('sk-private-api-key', client=http).decide(context(private_context='sk-private-context'))
    assert result.error_kind == 'invalid_decision' and result.error
    assert 'sk-private' not in repr(result)
    for issue in result.diagnostic.get('issues', []):
        assert set(issue) == {'field', 'code', 'reason'}
        assert issue['field'] in set(Decision.model_fields) | {'$schema', '$model'}


@pytest.mark.asyncio
@pytest.mark.parametrize('status,kind,code', [
    (401, 'authentication', 'authentication_or_access_denied'),
    (403, 'authentication', 'authentication_or_access_denied'),
    (429, 'limits', 'request_limited'),
    (500, 'http', 'http_error'),
])
async def test_http_failures_are_classified_without_exposing_provider_body(status, kind, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={'error': {'message': 'sk-private-provider-detail'}}))) as http:
        result = await AIClient('sk-private-key', client=http).decide(context())
    assert result.error_kind == kind and result.diagnostic['code'] == code
    assert result.decision.action == 'WAIT' and result.error
    assert 'sk-private' not in repr(result)
    if status == 429:
        assert 'quota épuisé' not in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type,code', [(httpx.ReadTimeout, 'timeout'), (httpx.ConnectError, 'network_error')])
async def test_network_errors_do_not_expose_exception_text(error_type, code):
    def handler(request):
        raise error_type('sk-private-network-detail', request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await AIClient('sk-private-key', client=http).decide(context())
    assert result.error_kind == 'network' and result.diagnostic['code'] == code
    assert result.decision.action == 'WAIT' and 'sk-private' not in repr(result)


@pytest.mark.asyncio
async def test_invalid_input_is_distinct_and_never_calls_provider():
    def handler(request):
        pytest.fail('Invalid input must not make a paid call or retry')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await AIClient('sk-private-key', client=http).decide(context(price=float('nan'), private='sk-private-context'))
    assert result.error_kind == 'invalid_input' and result.diagnostic['stage'] == 'input'
    assert result.decision.action == 'WAIT' and result.error
    assert (result.input_tokens, result.output_tokens) == (0, 0)
    assert 'sk-private' not in repr(result)
