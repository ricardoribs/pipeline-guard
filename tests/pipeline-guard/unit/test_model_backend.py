from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.diagnose_cli import DiagnosisRunner  # noqa: E402
from scripts.execution_budget import ExecutionBudget  # noqa: E402
from scripts.model_backend import (  # noqa: E402
    ModelBackendConfigurationError,
    ModelBackendError,
    ModelSettings,
    openai_chat_completion,
    resolve_model_settings,
)

sys.path.remove(str(PLUGIN_ROOT))


class FakeResponse:
    def __init__(self, document: object, *, status_code: int = 200) -> None:
        self.document = document
        self.status_code = status_code
        self.text = "untrusted response detail"

    def json(self) -> object:
        return self.document


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        return self.response


class FakeOAuthClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        return self.response


@pytest.mark.parametrize(
    ("profile", "environment", "expected_url", "expected_key"),
    [
        (
            "databricks",
            {
                "DATABRICKS_HOST": "https://workspace.example",
                "DATABRICKS_MODEL": "system.ai.test-model",
                "DATABRICKS_TOKEN": "databricks-secret",
            },
            "https://workspace.example/ai-gateway/mlflow/v1",
            "databricks-secret",
        ),
        (
            "openai",
            {"OPENAI_MODEL": "gpt-test", "OPENAI_API_KEY": "openai-secret"},
            "https://api.openai.com/v1",
            "openai-secret",
        ),
        (
            "gemini",
            {"GEMINI_MODEL": "gemini-test", "GEMINI_API_KEY": "gemini-secret"},
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-secret",
        ),
        (
            "ollama",
            {"OLLAMA_MODEL": "llama-test"},
            "http://127.0.0.1:11434/v1",
            None,
        ),
    ],
)
def test_resolve_supported_openai_compatible_profiles(
    profile: str,
    environment: dict[str, str],
    expected_url: str,
    expected_key: str | None,
) -> None:
    settings = resolve_model_settings(provider=profile, environment=environment)

    assert settings.profile == profile
    assert settings.provider == "openai-compatible"
    assert settings.base_url == expected_url
    assert settings.api_key == expected_key


def test_databricks_defaults_to_unity_gateway_service_model() -> None:
    settings = resolve_model_settings(
        provider="databricks",
        environment={
            "DATABRICKS_HOST": "https://workspace.example/",
            "DATABRICKS_TOKEN": "databricks-secret",
        },
    )

    assert settings.model == "system.ai.meta-llama-3-3-70b-instruct"
    assert settings.base_url == "https://workspace.example/ai-gateway/mlflow/v1"


def test_databricks_m2m_exchanges_credentials_for_ai_gateway_token() -> None:
    client = FakeOAuthClient(FakeResponse({"access_token": "short-lived-token"}))

    settings = resolve_model_settings(
        provider="databricks",
        environment={
            "DATABRICKS_HOST": "https://workspace.example",
            "DATABRICKS_MODEL_CLIENT_ID": "model-client-id",
            "DATABRICKS_MODEL_CLIENT_SECRET": "model-client-secret",
        },
        oauth_client=client,
    )

    assert settings.api_key == "short-lived-token"
    assert client.requests == [
        {
            "url": "https://workspace.example/oidc/v1/token",
            "auth": ("model-client-id", "model-client-secret"),
            "data": {
                "grant_type": "client_credentials",
                "scope": "ai-gateway",
            },
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "timeout": 30.0,
        }
    ]


def test_databricks_m2m_rejects_partial_credentials_without_leaking_them() -> None:
    secret = "model-client-secret"

    with pytest.raises(ModelBackendConfigurationError) as captured:
        resolve_model_settings(
            provider="databricks",
            environment={
                "DATABRICKS_HOST": "https://workspace.example",
                "DATABRICKS_MODEL_CLIENT_SECRET": secret,
            },
        )

    assert secret not in str(captured.value)


def test_explicit_databricks_base_url_preserves_legacy_endpoint_compatibility() -> None:
    settings = resolve_model_settings(
        provider="databricks",
        environment={
            "DATABRICKS_HOST": "https://workspace.example",
            "DATABRICKS_MODEL_BASE_URL": (
                "https://workspace.example/serving-endpoints"
            ),
            "DATABRICKS_MODEL": "legacy-endpoint-name",
            "DATABRICKS_TOKEN": "databricks-secret",
        },
    )

    assert settings.base_url == "https://workspace.example/serving-endpoints"
    assert settings.model == "legacy-endpoint-name"


def test_databricks_model_base_url_must_use_https() -> None:
    with pytest.raises(ModelBackendConfigurationError, match="HTTPS"):
        resolve_model_settings(
            provider="databricks",
            environment={
                "DATABRICKS_HOST": "http://workspace.example",
                "DATABRICKS_TOKEN": "databricks-secret",
            },
        )


def test_anthropic_remains_the_default_backend() -> None:
    settings = resolve_model_settings(environment={"ANTHROPIC_MODEL": "claude-test"})

    assert settings.provider == "anthropic"
    assert settings.uses_claude_code is True
    assert settings.model == "claude-test"


def test_custom_openai_compatible_profile_requires_a_base_url() -> None:
    with pytest.raises(ModelBackendConfigurationError, match="base_url"):
        resolve_model_settings(
            provider="openai-compatible",
            model="custom-model",
            environment={},
        )


@pytest.mark.parametrize(
    ("profile", "model_variable", "unrelated_environment"),
    [
        (
            "ollama",
            "OLLAMA_MODEL",
            {
                "OPENAI_API_KEY": "unrelated-openai-secret",
                "OPENAI_BASE_URL": "https://unrelated-openai.example/v1",
            },
        ),
        (
            "openai-compatible",
            "PIPELINE_GUARD_MODEL",
            {
                "OPENAI_API_KEY": "unrelated-openai-secret",
                "GEMINI_API_KEY": "unrelated-gemini-secret",
            },
        ),
    ],
)
def test_profiles_do_not_inherit_credentials_or_endpoints_from_other_providers(
    profile: str,
    model_variable: str,
    unrelated_environment: dict[str, str],
) -> None:
    environment = {
        model_variable: "selected-model",
        "PIPELINE_GUARD_MODEL_BASE_URL": "https://selected-model.example/v1",
        **unrelated_environment,
    }

    settings = resolve_model_settings(provider=profile, environment=environment)

    assert settings.model == "selected-model"
    assert settings.base_url == "https://selected-model.example/v1"
    assert settings.api_key is None


def test_custom_profile_uses_only_its_explicit_credential_reference() -> None:
    settings = resolve_model_settings(
        {
            "runtime": {
                "model": {
                    "provider": "openai-compatible",
                    "name": "custom-model",
                    "base_url": "https://model.example/v1",
                    "credential_ref": "env:CUSTOM_MODEL_TOKEN",
                }
            }
        },
        environment={
            "CUSTOM_MODEL_TOKEN": "custom-secret",
            "OPENAI_API_KEY": "unrelated-openai-secret",
        },
    )

    assert settings.api_key == "custom-secret"


def test_model_output_limit_is_resolved_independently_from_execution_budget() -> None:
    settings = resolve_model_settings(
        {
            "runtime": {
                "model": {
                    "provider": "ollama",
                    "name": "llama-test",
                    "max_output_tokens": 8192,
                }
            }
        },
        environment={},
    )

    assert settings.max_output_tokens == 8192
    assert settings.bounded_output_tokens(50_000) == 8192
    assert settings.bounded_output_tokens(4096) == 4096


@pytest.mark.parametrize("value", [0, -1, True, "8192"])
def test_model_output_limit_must_be_a_positive_integer(value: object) -> None:
    with pytest.raises(ModelBackendConfigurationError, match="max_output_tokens"):
        resolve_model_settings(
            {
                "runtime": {
                    "model": {
                        "provider": "ollama",
                        "name": "llama-test",
                        "max_output_tokens": value,
                    }
                }
            },
            environment={},
        )


def test_literal_model_credentials_are_rejected() -> None:
    with pytest.raises(ModelBackendConfigurationError, match="credential_ref"):
        resolve_model_settings(
            {
                "runtime": {
                    "model": {
                        "provider": "openai-compatible",
                        "name": "custom-model",
                        "base_url": "https://model.example/v1",
                        "credential_ref": "literal-secret-value",
                    }
                }
            },
            environment={},
        )


def test_chat_completion_uses_bearer_auth_without_exposing_it_in_result() -> None:
    client = FakeClient(FakeResponse({"choices": [{"message": {"content": "ok"}}]}))
    settings = ModelSettings(
        profile="openai",
        provider="openai-compatible",
        model="gpt-test",
        base_url="https://model.example/v1",
        api_key="secret-value",
    )

    result = openai_chat_completion(
        settings,
        {"model": settings.model, "messages": []},
        timeout=10,
        client=client,
    )

    assert result["choices"]
    assert client.requests[0]["url"] == "https://model.example/v1/chat/completions"
    assert client.requests[0]["headers"]["Authorization"] == "Bearer secret-value"
    assert "secret-value" not in str(result)


def test_diagnosis_runner_uses_openai_compatible_backend() -> None:
    client = FakeClient(
        FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "diagnosis-1",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_diagnosis",
                                        "arguments": '{"cause_status":"UNKNOWN"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"completion_tokens": 7},
            }
        )
    )
    settings = ModelSettings(
        profile="ollama",
        provider="openai-compatible",
        model="llama-test",
        base_url="http://127.0.0.1:11434/v1",
        api_key=None,
        max_output_tokens=512,
    )
    budget = ExecutionBudget(
        {
            "runtime": {
                "execution_budget": {
                    "max_tool_calls": 2,
                    "max_seconds": 60,
                    "max_tokens": 10_000,
                }
            }
        }
    )

    result = DiagnosisRunner(settings=settings, client=client)(
        {}, budget, "Return JSON."
    )

    assert result == {"cause_status": "UNKNOWN"}
    assert client.requests[0]["json"]["model"] == "llama-test"
    assert client.requests[0]["json"]["max_tokens"] == 512
    assert client.requests[0]["json"]["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_diagnosis"},
    }
    parameters = client.requests[0]["json"]["tools"][0]["function"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert "evidence_citations" in parameters["required"]


@pytest.mark.parametrize(
    "arguments",
    ["{", "[]", None],
)
def test_diagnosis_runner_rejects_invalid_structured_arguments(
    arguments: object,
) -> None:
    client = FakeClient(
        FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "diagnosis-1",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_diagnosis",
                                        "arguments": arguments,
                                    },
                                }
                            ]
                        },
                    }
                ],
                "usage": {"completion_tokens": 7},
            }
        )
    )
    settings = ModelSettings(
        profile="ollama",
        provider="openai-compatible",
        model="llama-test",
        base_url="http://127.0.0.1:11434/v1",
        api_key=None,
    )

    assert (
        DiagnosisRunner(settings=settings, client=client)(
            {},
            ExecutionBudget(
                {
                    "runtime": {
                        "execution_budget": {
                            "max_tool_calls": 2,
                            "max_seconds": 60,
                            "max_tokens": 10_000,
                        }
                    }
                }
            ),
            "Return JSON.",
        )
        is None
    )


@pytest.mark.parametrize(
    ("status_code", "reason_code"),
    [
        (401, "CREDENTIAL_UNAVAILABLE"),
        (429, "MODEL_UNAVAILABLE"),
        (500, "MODEL_UNAVAILABLE"),
    ],
)
def test_chat_completion_classifies_sanitized_http_failures(
    status_code: int, reason_code: str
) -> None:
    client = FakeClient(FakeResponse({}, status_code=status_code))
    settings = ModelSettings(
        profile="ollama",
        provider="openai-compatible",
        model="llama-test",
        base_url="http://127.0.0.1:11434/v1",
        api_key=None,
    )

    with pytest.raises(ModelBackendError) as captured:
        openai_chat_completion(settings, {}, timeout=10, client=client)

    assert captured.value.reason_code == reason_code
    assert "untrusted response detail" not in str(captured.value)
