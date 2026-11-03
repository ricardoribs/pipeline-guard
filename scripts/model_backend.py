# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27.0"]
# ///
"""Resolve model backends and call OpenAI-compatible Chat Completions safely."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

_PROVIDER_ALIASES = {
    "anthropic": "anthropic",
    "databricks": "openai-compatible",
    "openai": "openai-compatible",
    "gemini": "openai-compatible",
    "ollama": "openai-compatible",
    "openai-compatible": "openai-compatible",
}
_REFERENCE_PATTERN = re.compile(
    r"^(?:[A-Z][A-Z0-9_]*|env:[A-Z][A-Z0-9_]*|"
    r"\$\{[A-Z][A-Z0-9_]*\}|secret:[A-Za-z0-9][A-Za-z0-9._/-]*)$"
)
_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "ollama": "http://127.0.0.1:11434/v1",
}
_DEFAULT_MODELS = {
    "databricks": "system.ai.meta-llama-3-3-70b-instruct",
}
_DEFAULT_CREDENTIAL_REFERENCES = {
    "openai": "env:OPENAI_API_KEY",
    "gemini": "env:GEMINI_API_KEY",
}
_DATABRICKS_OAUTH_SCOPE = "ai-gateway"
_DATABRICKS_OAUTH_TIMEOUT_SECONDS = 30.0


class ModelBackendConfigurationError(ValueError):
    """Raised before a model request when backend configuration is incomplete."""


class ModelBackendError(RuntimeError):
    """Sanitized model failure with a stable unattended reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        if reason_code not in {
            "CREDENTIAL_UNAVAILABLE",
            "MODEL_UNAVAILABLE",
            "OPERATIONAL_FAILURE",
        }:
            raise ValueError("unsupported model failure reason code")
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True)
class ModelSettings:
    """Resolved, provider-neutral model settings for one process."""

    profile: str
    provider: str
    model: str
    base_url: str | None
    api_key: str | None
    credential_kind: str | None = None

    @property
    def uses_claude_code(self) -> bool:
        return self.provider == "anthropic"

    @property
    def secret_values(self) -> tuple[str, ...]:
        return (self.api_key,) if self.api_key else ()


class HTTPResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> object: ...


class HTTPClient(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
        timeout: float,
    ) -> HTTPResponse: ...


class OAuthHTTPClient(Protocol):
    def post(
        self,
        url: str,
        *,
        auth: tuple[str, str],
        data: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> HTTPResponse: ...


def resolve_model_settings(
    config: Mapping[str, Any] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    environment: Mapping[str, str] | None = None,
    oauth_client: OAuthHTTPClient | None = None,
) -> ModelSettings:
    """Resolve an explicit model profile without exposing credential values."""

    host_environment = os.environ if environment is None else environment
    configured = _configured_model(config)
    selected_profile = _first_text(
        provider,
        configured.get("provider"),
        host_environment.get("PIPELINE_GUARD_MODEL_PROVIDER"),
        "anthropic",
    ).lower()
    canonical_provider = _PROVIDER_ALIASES.get(selected_profile)
    if canonical_provider is None:
        supported = ", ".join(sorted(_PROVIDER_ALIASES))
        raise ModelBackendConfigurationError(
            f"model provider must be one of: {supported}"
        )

    selected_model = _first_text(
        model,
        configured.get("name"),
        host_environment.get("PIPELINE_GUARD_MODEL"),
        *_profile_model_environment(selected_profile, host_environment),
        _DEFAULT_MODELS.get(selected_profile),
    )
    if not selected_model:
        raise ModelBackendConfigurationError(
            "model name is required through --model, runtime.model.name, or environment"
        )

    if canonical_provider == "anthropic":
        base_url = (
            _first_text(
                configured.get("base_url"),
                host_environment.get("PIPELINE_GUARD_MODEL_BASE_URL"),
                host_environment.get("ANTHROPIC_BASE_URL"),
            )
            or None
        )
        credential_reference = configured.get("credential_ref")
        referenced_credential = _resolve_optional_reference(
            credential_reference, host_environment
        )
        generic_credential = host_environment.get("PIPELINE_GUARD_MODEL_API_KEY")
        anthropic_api_key = host_environment.get("ANTHROPIC_API_KEY")
        anthropic_auth_token = host_environment.get("ANTHROPIC_AUTH_TOKEN")
        api_key = (
            _first_text(
                referenced_credential,
                generic_credential,
                anthropic_api_key,
                anthropic_auth_token,
            )
            or None
        )
        if referenced_credential:
            credential_kind = _anthropic_reference_kind(credential_reference)
        elif generic_credential:
            credential_kind = "bearer"
        elif anthropic_api_key:
            credential_kind = "api-key"
        elif anthropic_auth_token:
            credential_kind = "bearer"
        else:
            credential_kind = None
    else:
        base_url = (
            _first_text(
                configured.get("base_url"),
                host_environment.get("PIPELINE_GUARD_MODEL_BASE_URL"),
                *_profile_base_environment(selected_profile, host_environment),
                _DEFAULT_BASE_URLS.get(selected_profile),
            )
            or None
        )
        if base_url is None:
            raise ModelBackendConfigurationError(
                "OpenAI-compatible providers require runtime.model.base_url or "
                "PIPELINE_GUARD_MODEL_BASE_URL"
            )
        credential_reference = configured.get("credential_ref") or (
            _DEFAULT_CREDENTIAL_REFERENCES.get(selected_profile)
        )
        api_key = (
            _first_text(
                _resolve_optional_reference(credential_reference, host_environment),
                host_environment.get("PIPELINE_GUARD_MODEL_API_KEY"),
                *_profile_key_environment(selected_profile, host_environment),
            )
            or None
        )
        if selected_profile == "databricks" and api_key is None:
            api_key = _databricks_oauth_access_token(
                host_environment,
                client=oauth_client,
            )
        if selected_profile in {"databricks", "openai", "gemini"} and api_key is None:
            raise ModelBackendConfigurationError(
                f"{selected_profile} model credentials are unavailable"
            )
        credential_kind = "bearer" if api_key else None

    if base_url is not None:
        _validate_base_url(base_url)
        if selected_profile == "databricks" and urlsplit(base_url).scheme != "https":
            raise ModelBackendConfigurationError(
                "databricks model base URL must use HTTPS"
            )
    return ModelSettings(
        profile=selected_profile,
        provider=canonical_provider,
        model=selected_model,
        base_url=base_url,
        api_key=api_key,
        credential_kind=credential_kind,
    )


def openai_chat_completion(
    settings: ModelSettings,
    payload: Mapping[str, object],
    *,
    timeout: float,
    client: HTTPClient | None = None,
) -> dict[str, Any]:
    """POST one OpenAI-compatible request and return a validated JSON object."""

    if settings.provider != "openai-compatible" or settings.base_url is None:
        raise ModelBackendConfigurationError(
            "OpenAI-compatible settings are required for chat completions"
        )
    if timeout <= 0:
        raise ModelBackendError("MODEL_UNAVAILABLE", "model request timed out")
    headers = {"Content-Type": "application/json"}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    endpoint = settings.base_url.rstrip("/") + "/chat/completions"
    try:
        if client is None:
            with httpx.Client(follow_redirects=False) as owned_client:
                response = owned_client.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
        else:
            response = client.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
    except (httpx.TimeoutException, httpx.NetworkError) as error:
        raise ModelBackendError(
            "MODEL_UNAVAILABLE", "model endpoint is unavailable"
        ) from error
    except httpx.HTTPError as error:
        raise ModelBackendError(
            "OPERATIONAL_FAILURE", "model request could not be completed"
        ) from error

    if response.status_code >= 400:
        raise ModelBackendError(
            _http_reason_code(response.status_code),
            f"model endpoint returned HTTP {response.status_code}",
        )
    try:
        document = response.json()
    except (json.JSONDecodeError, ValueError) as error:
        raise ModelBackendError(
            "OPERATIONAL_FAILURE", "model endpoint returned invalid JSON"
        ) from error
    if not isinstance(document, dict):
        raise ModelBackendError(
            "OPERATIONAL_FAILURE", "model endpoint returned an invalid response"
        )
    return document


def _configured_model(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    runtime = config.get("runtime")
    if not isinstance(runtime, Mapping):
        return {}
    configured = runtime.get("model")
    return configured if isinstance(configured, Mapping) else {}


def _profile_model_environment(
    profile: str, environment: Mapping[str, str]
) -> tuple[str | None, ...]:
    if profile == "anthropic":
        return (environment.get("ANTHROPIC_MODEL"),)
    if profile == "databricks":
        return (environment.get("DATABRICKS_MODEL"),)
    if profile == "gemini":
        return (environment.get("GEMINI_MODEL"),)
    if profile == "ollama":
        return (environment.get("OLLAMA_MODEL"),)
    if profile == "openai":
        return (environment.get("OPENAI_MODEL"),)
    return ()


def _profile_base_environment(
    profile: str, environment: Mapping[str, str]
) -> tuple[str | None, ...]:
    if profile == "databricks":
        host = environment.get("DATABRICKS_HOST")
        serving_base = host.rstrip("/") + "/ai-gateway/mlflow/v1" if host else None
        return (environment.get("DATABRICKS_MODEL_BASE_URL"), serving_base)
    if profile == "gemini":
        return (environment.get("GEMINI_BASE_URL"),)
    if profile == "ollama":
        return (environment.get("OLLAMA_BASE_URL"),)
    if profile == "openai":
        return (environment.get("OPENAI_BASE_URL"),)
    return ()


def _databricks_oauth_access_token(
    environment: Mapping[str, str],
    *,
    client: OAuthHTTPClient | None,
) -> str | None:
    client_id = environment.get("DATABRICKS_MODEL_CLIENT_ID")
    client_secret = environment.get("DATABRICKS_MODEL_CLIENT_SECRET")
    if not client_id and not client_secret:
        return None
    if not client_id or not client_secret:
        raise ModelBackendConfigurationError(
            "databricks OAuth credentials require both "
            "DATABRICKS_MODEL_CLIENT_ID and DATABRICKS_MODEL_CLIENT_SECRET"
        )
    host = environment.get("DATABRICKS_HOST")
    if not host:
        raise ModelBackendConfigurationError(
            "databricks OAuth credentials require DATABRICKS_HOST"
        )
    token_url = _databricks_token_url(host)
    request = {
        "auth": (client_id, client_secret),
        "data": {
            "grant_type": "client_credentials",
            "scope": _DATABRICKS_OAUTH_SCOPE,
        },
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "timeout": _DATABRICKS_OAUTH_TIMEOUT_SECONDS,
    }
    try:
        if client is None:
            with httpx.Client(follow_redirects=False) as owned_client:
                response = owned_client.post(token_url, **request)
        else:
            response = client.post(token_url, **request)
    except (httpx.TimeoutException, httpx.NetworkError) as error:
        raise ModelBackendConfigurationError(
            "databricks OAuth token endpoint is unavailable"
        ) from error
    except httpx.HTTPError as error:
        raise ModelBackendConfigurationError(
            "databricks OAuth token request could not be completed"
        ) from error
    if response.status_code >= 400:
        raise ModelBackendConfigurationError(
            "databricks OAuth credentials could not be validated"
        )
    try:
        document = response.json()
    except (json.JSONDecodeError, ValueError) as error:
        raise ModelBackendConfigurationError(
            "databricks OAuth token endpoint returned invalid JSON"
        ) from error
    if not isinstance(document, Mapping):
        raise ModelBackendConfigurationError(
            "databricks OAuth token endpoint returned an invalid response"
        )
    access_token = document.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise ModelBackendConfigurationError(
            "databricks OAuth token response did not contain an access token"
        )
    return access_token.strip()


def _databricks_token_url(host: str) -> str:
    _validate_base_url(host)
    parsed = urlsplit(host)
    if parsed.path not in {"", "/"}:
        raise ModelBackendConfigurationError(
            "DATABRICKS_HOST must be the HTTPS workspace origin"
        )
    if parsed.scheme != "https":
        raise ModelBackendConfigurationError(
            "DATABRICKS_HOST must be the HTTPS workspace origin"
        )
    return host.rstrip("/") + "/oidc/v1/token"


def _profile_key_environment(
    profile: str, environment: Mapping[str, str]
) -> tuple[str | None, ...]:
    if profile == "databricks":
        return (environment.get("DATABRICKS_TOKEN"),)
    if profile == "gemini":
        return (environment.get("GEMINI_API_KEY"),)
    if profile == "ollama":
        return (environment.get("OLLAMA_API_KEY"),)
    if profile == "openai":
        return (environment.get("OPENAI_API_KEY"),)
    return ()


def _resolve_optional_reference(
    reference: object, environment: Mapping[str, str]
) -> str | None:
    if reference is None:
        return None
    if (
        not isinstance(reference, str)
        or _REFERENCE_PATTERN.fullmatch(reference) is None
    ):
        raise ModelBackendConfigurationError(
            "runtime.model.credential_ref must be an environment or secret reference"
        )
    if reference.startswith("env:"):
        variable = reference[4:]
    elif reference.startswith("${"):
        variable = reference[2:-1]
    elif reference.startswith("secret:"):
        secret_name = reference[7:].replace("/", "_").replace("-", "_").upper()
        variable = f"SECRET_{secret_name}"
        if variable not in environment and reference in environment:
            variable = reference
    else:
        variable = reference
    return environment.get(variable) or None


def _anthropic_reference_kind(reference: object) -> str:
    if isinstance(reference, str) and "API_KEY" in reference.upper():
        return "api-key"
    return "bearer"


def _validate_base_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ModelBackendConfigurationError("model base URL is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ModelBackendConfigurationError(
            "model base URL must be an HTTP(S) URL without credentials, query, or fragment"
        )


def _http_reason_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return "CREDENTIAL_UNAVAILABLE"
    if status_code in {408, 409, 429} or status_code >= 500:
        return "MODEL_UNAVAILABLE"
    return "OPERATIONAL_FAILURE"


def _first_text(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
