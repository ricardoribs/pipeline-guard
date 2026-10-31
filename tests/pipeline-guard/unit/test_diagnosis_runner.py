from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.diagnose_cli import DiagnosisRunner
from scripts.execution_budget import ExecutionBudget, ExecutionBudgetExceeded

sys.path.remove(str(PLUGIN_ROOT))


def _budget(tokens=1000):
    return ExecutionBudget(
        {
            "runtime": {
                "execution_budget": {
                    "max_tool_calls": 10,
                    "max_seconds": 60,
                    "max_tokens": tokens,
                }
            }
        }
    )


def _client(text='{"cause_status": "UNKNOWN"}', stop_reason="end_turn"):
    client = Mock()
    client.messages.count_tokens.return_value = SimpleNamespace(input_tokens=100)
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=10,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    )
    return client


def test_requests_have_timeouts_output_ceiling_and_no_tools():
    client = _client()
    budget = _budget()
    result = DiagnosisRunner("test-model", client=client)({}, budget, "agent rules")
    assert result == {"cause_status": "UNKNOWN"}
    arguments = client.messages.create.call_args.kwargs
    assert 0 < arguments["timeout"] <= 60
    assert arguments["max_tokens"] == 899
    assert "temperature" not in arguments
    assert "tools" not in arguments
    assert budget.remaining_tokens == 890


def test_prompt_exceeding_budget_never_reaches_generation():
    client = _client()
    with pytest.raises(ExecutionBudgetExceeded, match="max_tokens"):
        DiagnosisRunner("test-model", client=client)(
            {}, _budget(tokens=100), "agent rules"
        )
    client.messages.create.assert_not_called()


def _unity_gateway_count_error() -> anthropic.BadRequestError:
    request = httpx.Request("POST", "https://example.invalid/v1/messages/count_tokens")
    response = httpx.Response(400, request=request)
    return anthropic.BadRequestError(
        "Request path '/ai-gateway/anthropic/v1/messages/count_tokens' doesn't match any "
        "known API type and is classified as an unmanaged api request. Set the "
        "Databricks-Model-Provider-Service header.",
        response=response,
        body=None,
    )


def test_unity_gateway_without_token_count_uses_bounded_local_preflight():
    client = _client()
    client.messages.count_tokens.side_effect = _unity_gateway_count_error()
    budget = _budget(tokens=2000)

    result = DiagnosisRunner("test-model", client=client)({}, budget, "rules")

    assert result == {"cause_status": "UNKNOWN"}
    assert 0 < client.messages.create.call_args.kwargs["max_tokens"] < 2000
    assert budget.remaining_tokens < 2000


def test_unity_gateway_local_bound_stops_before_generation_when_too_large():
    client = _client()
    client.messages.count_tokens.side_effect = _unity_gateway_count_error()

    with pytest.raises(ExecutionBudgetExceeded, match="max_tokens"):
        DiagnosisRunner("test-model", client=client)({}, _budget(tokens=100), "rules")

    client.messages.create.assert_not_called()


def test_other_token_count_bad_request_does_not_fallback():
    client = _client()
    request = httpx.Request("POST", "https://example.invalid/v1/messages/count_tokens")
    client.messages.count_tokens.side_effect = anthropic.BadRequestError(
        "Invalid model",
        response=httpx.Response(400, request=request),
        body=None,
    )

    with pytest.raises(anthropic.BadRequestError, match="Invalid model"):
        DiagnosisRunner("test-model", client=client)({}, _budget(), "rules")

    client.messages.create.assert_not_called()


@pytest.mark.parametrize(
    ("text", "stop_reason"), [('{"x":', "end_turn"), ("{}", "max_tokens")]
)
def test_malformed_or_truncated_output_is_not_accepted(text, stop_reason):
    assert (
        DiagnosisRunner("test-model", client=_client(text, stop_reason))(
            {}, _budget(), "agent rules"
        )
        is None
    )


def test_sdk_timeout_stops_investigation():
    client = _client()
    client.messages.create.side_effect = anthropic.APITimeoutError(
        request=httpx.Request("POST", "https://example.invalid/v1/messages")
    )
    with pytest.raises(ExecutionBudgetExceeded, match="max_seconds"):
        DiagnosisRunner("test-model", client=client)({}, _budget(), "agent rules")


def test_actual_usage_over_ceiling_discards_otherwise_valid_json():
    client = _client(json.dumps({"cause_status": "CONFIRMED"}))
    client.messages.create.return_value.usage.output_tokens = 900
    with pytest.raises(ExecutionBudgetExceeded, match="max_tokens"):
        DiagnosisRunner("test-model", client=client)({}, _budget(), "agent rules")
