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
