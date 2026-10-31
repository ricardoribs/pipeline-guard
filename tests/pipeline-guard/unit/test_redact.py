from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
HOOKS_ROOT = PLUGIN_ROOT / "hooks"
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.redaction import (  # noqa: E402
    REDACTED_EMAIL,
    REDACTED_PRIVATE_KEY,
    REDACTED_SECRET,
    redact_payload,
    redact_text,
)

sys.path.remove(str(PLUGIN_ROOT))

REDACT_SCRIPT = HOOKS_ROOT / "redact.py"
ATLASSIAN_API_TOKEN = "ATATT3" + ("A1_b-" * 37) + "Z"
DIGITALOCEAN_PAT = "dop_v1_" + ("a1" * 32)
AWS_SECRET_ACCESS_KEY = "Ab3/" * 10


@pytest.mark.parametrize(
    ("source", "secret"),
    [
        (
            "Authorization: Bearer bearer-token-value-123456",
            "bearer-token-value-123456",
        ),
        ("Authorization: Token token-value-1234567890", "token-value-1234567890"),
        ("AccountKey=YWJjZGVmZ2hpamtsbW5vcA==", "YWJjZGVmZ2hpamtsbW5vcA=="),
        ("api_key='stripe-test-placeholder'", "stripe-test-placeholder"),
        ("api_key=stripe-test-placeholder", "stripe-test-placeholder"),
        ("api_key='literal-api-key-123456'", "literal-api-key-123456"),
        ("API key: literal-api-key-123456", "literal-api-key-123456"),
        ("API\t  key: spaced-api-key-123456", "spaced-api-key-123456"),
        (
            "DATABRICKS_TOKEN=placeholder",
            "placeholder",
        ),
        (f"Atlassian API token: {ATLASSIAN_API_TOKEN}", ATLASSIAN_API_TOKEN),
        (f"DigitalOcean PAT: {DIGITALOCEAN_PAT}", DIGITALOCEAN_PAT),
        (f"AWS credential: {AWS_SECRET_ACCESS_KEY}", AWS_SECRET_ACCESS_KEY),
        (
            '"clientSecret": "literal-client-secret-123456"',
            "literal-client-secret-123456",
        ),
        ("password=literal-password", "literal-password"),
        ("token: literal-token-value", "literal-token-value"),
        ("DATABRICKS_TOKEN=literal-databricks-token", "literal-databricks-token"),
        ("AWS_SECRET_ACCESS_KEY=literal-aws-secret-key", "literal-aws-secret-key"),
        (
            "Cookie: sessionid=literal-session-cookie; theme=dark",
            "literal-session-cookie",
        ),
        ("Set-Cookie: auth=literal-auth-cookie; HttpOnly", "literal-auth-cookie"),
        ("senha=literal-senha", "literal-senha"),
        ("segredo: literal-segredo", "literal-segredo"),
        ("chave=literal-chave", "literal-chave"),
        ("credencial: literal-credencial", "literal-credencial"),
        ("credenciais=literal-credenciais", "literal-credenciais"),
        ("chave de API: literal-chave-api", "literal-chave-api"),
        (
            "SharedAccessSignature=sv=2024-01-01&sig=literal-sas-signature&se=2099-01-01",
            "literal-sas-signature",
        ),
        (
            "token=ghp_abcdefghijklmnopqrstuvwxyz123456",
            "ghp_abcdefghijklmnopqrstuvwxyz123456",
        ),
        (
            "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.signature123456",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.signature123456",
        ),
    ],
)
def test_redact_text_masks_known_secret_patterns(source: str, secret: str) -> None:
    result = redact_text(source)

    assert secret not in result
    assert REDACTED_SECRET in result


def test_redact_text_masks_email_and_url_userinfo() -> None:
    source = (
        "contact alice.smith@example.com and use "
        "postgresql://pipeline-user:literal-password@db.example.com/warehouse"
    )

    result = redact_text(source)

    assert "alice.smith@example.com" not in result
    assert "literal-password" not in result
    assert REDACTED_EMAIL in result
    assert f"pipeline-user:{REDACTED_SECRET}@" in result
    assert "db.example.com/warehouse" in result


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "https://user@example.com:literal-password@host.example/repository.git",
            f"https://{REDACTED_EMAIL}:{REDACTED_SECRET}@host.example/repository.git",
        ),
        (
            "https://literal-access-token@host.example/repository.git",
            f"https://{REDACTED_SECRET}@host.example/repository.git",
        ),
        (
            "https://api.example/items?api_key=literal-api-key&limit=10",
            f"https://api.example/items?api_key={REDACTED_SECRET}&limit=10",
        ),
    ],
)
def test_redact_text_masks_url_credentials_without_removing_safe_url_parts(
    source: str, expected: str
) -> None:
    assert redact_text(source) == expected


def test_redact_text_masks_private_key_block() -> None:
    private_key = "\n".join(
        (
            "-----BEGIN PRIVATE" + " KEY-----",
            "c3VwZXItc2VjcmV0LWtleQ==",
            "-----END PRIVATE" + " KEY-----",
        )
    )

    result = redact_text(f"before\n{private_key}\nafter")

    assert private_key not in result
    assert result == f"before\n{REDACTED_PRIVATE_KEY}\nafter"


def test_redact_payload_masks_sensitive_fields_and_nested_content() -> None:
    payload: dict[str, Any] = {
        "incident_id": "incident-001",
        "summary": "Failure reported by owner@example.com",
        "credentials": {
            "username": "pipeline-user",
            "password": "literal-password",
        },
        "provider": {
            "AccountKey": "literal-storage-account-key",
            "clientSecret": "literal-client-secret",
            "access_token": "literal-access-token",
        },
        "safe": {"sha256": "abc123", "count": 3, "enabled": True},
    }

    result = redact_payload(payload)
    serialized = json.dumps(result)

    assert result["incident_id"] == "incident-001"
    assert result["summary"] == f"Failure reported by {REDACTED_EMAIL}"
    assert result["credentials"] == {
        "username": REDACTED_SECRET,
        "password": REDACTED_SECRET,
    }
    assert result["provider"]["clientSecret"] == REDACTED_SECRET
    assert result["provider"]["AccountKey"] == REDACTED_SECRET
    assert result["provider"]["access_token"] == REDACTED_SECRET
    assert result["safe"] == {"sha256": "abc123", "count": 3, "enabled": True}
    assert "literal-password" not in serialized
    assert "literal-client-secret" not in serialized
    assert "literal-access-token" not in serialized
    assert "owner@example.com" not in serialized


@pytest.mark.parametrize(
    "field_name",
    [
        "SECRET_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "DB_PASS",
        "auth_cookie",
        "chave",
        "chave_de_api",
        "cookie",
        "credencial",
        "credenciais",
        "encryption_key",
        "segredo",
        "session_id",
        "senha",
        "SharedAccessSignature",
    ],
)
def test_redact_payload_masks_common_secret_field_shapes(field_name: str) -> None:
    payload = {field_name: "synthetic-secret-value"}

    assert redact_payload(payload) == {field_name: REDACTED_SECRET}


def test_redaction_is_idempotent_and_preserves_safe_text() -> None:
    source = "Model analytics.orders failed after 3 attempts; hash sha256:abc123; MAX_TOKENS=4096."
    payload = {"message": source, "contact": "owner@example.com"}

    first = redact_payload(payload)
    second = redact_payload(first)

    assert first == second
    assert first["message"] == source

    redacted_text = redact_text("token=literal-token-value")
    assert redact_text(redacted_text) == redacted_text


@pytest.mark.parametrize(
    "source",
    [
        "sha1: 0123456789abcdef0123456789abcdef01234567",
        "opaque identifier: abcdefghijklmnopqrstuvwxyzabcdefghijklmn",
        f"embedded-x{AWS_SECRET_ACCESS_KEY}y-identifier",
    ],
)
def test_redact_text_preserves_non_secret_40_character_identifiers(source: str) -> None:
    assert redact_text(source) == source


def test_direct_script_call_sanitizes_json_without_leaking_values() -> None:
    payload = {
        "incident_id": "incident-cli-001",
        "evidence": {
            "content": "Authorization: Bearer live-token-value-123456",
            "owner": "data.owner@example.com",
            "connectionString": "postgresql://user:literal-password@host/db",
        },
    }

    completed = subprocess.run(
        [sys.executable, str(REDACT_SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert "live-token-value-123456" not in completed.stdout
    assert "data.owner@example.com" not in completed.stdout
    assert "literal-password" not in completed.stdout

    result = json.loads(completed.stdout)
    assert result["incident_id"] == "incident-cli-001"
    assert REDACTED_SECRET in result["evidence"]["content"]
    assert result["evidence"]["owner"] == REDACTED_EMAIL
    assert result["evidence"]["connectionString"] == REDACTED_SECRET


def test_direct_script_rejects_invalid_json_without_echoing_input() -> None:
    secret = "literal-password-that-must-not-leak"

    completed = subprocess.run(
        [sys.executable, str(REDACT_SCRIPT)],
        input=f'{{"password": "{secret}"',
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "redaction input must be valid JSON\n"
    assert secret not in completed.stderr


@pytest.mark.parametrize(
    "source",
    [
        "Basic setup complete",
        "Token count: 5",
        "basic configuration loaded",
        "Token validation complete",
        "Basic\nsetup complete",
    ],
)
def test_redact_text_preserves_auth_scheme_prose(source: str) -> None:
    assert redact_text(source) == source
    assert redact_payload({"message": source}) == {"message": source}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "Authorization: Basic dXNlcjpwYXNz",
            f"Authorization: Basic {REDACTED_SECRET}",
        ),
        (
            "Authorization: Token short",
            f"Authorization: Token {REDACTED_SECRET}",
        ),
        (
            "Proxy-Authorization: Basic dXNlcjpwYXNz",
            f"Proxy-Authorization: Basic {REDACTED_SECRET}",
        ),
        (
            "Proxy-Authorization: Token short",
            f"Proxy-Authorization: Token {REDACTED_SECRET}",
        ),
        (
            "authorization:\t token\tshort",
            f"authorization:\t token {REDACTED_SECRET}",
        ),
        (
            '"Authorization": "Basic dXNlcjpwYXNz"',
            f'"Authorization": "Basic {REDACTED_SECRET}"',
        ),
        (
            "Bearer standalone-token-123",
            f"Bearer {REDACTED_SECRET}",
        ),
        (
            "Authorization: ApiKey abc123def456ghi789",
            f"Authorization: ApiKey {REDACTED_SECRET}",
        ),
        (
            'Authorization: Digest username="demo", response="abcdef123456"',
            f"Authorization: Digest {REDACTED_SECRET}",
        ),
        (
            "Authorization: 9f8e7d6c5b4a39281706",
            f"Authorization: {REDACTED_SECRET}",
        ),
    ],
)
def test_redact_text_masks_authentication_context(source: str, expected: str) -> None:
    result = redact_text(source)

    assert result == expected
    assert redact_text(result) == result


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (ATLASSIAN_API_TOKEN, REDACTED_SECRET),
        (DIGITALOCEAN_PAT, REDACTED_SECRET),
        (
            "webhook https://hooks.slack-gov.com/services/test/workspace/fake-webhook",
            f"webhook {REDACTED_SECRET}",
        ),
        (
            "https://hooks.slack-gov.com/services/T01234567/B01234567/abcdefghijklmnopqrstuvwx",
            REDACTED_SECRET,
        ),
        (
            "https://example.blob.core.windows.net/container/file?sv=2023-11-03"
            "&sig=synthetic%2Bsignature%2Fvalue%3D&sp=r",
            "https://example.blob.core.windows.net/container/file?sv=2023-11-03"
            f"&sig={REDACTED_SECRET}&sp=r",
        ),
        (
            "https://storage.example.com/file?sig=synthetic-signature&sp=r#section",
            f"https://storage.example.com/file?sig={REDACTED_SECRET}&sp=r#section",
        ),
        (
            "https://example.blob.core.windows.net/file?SIG=first&sig=second",
            f"https://example.blob.core.windows.net/file?SIG={REDACTED_SECRET}"
            f"&sig={REDACTED_SECRET}",
        ),
        (
            '"https://example.blob.core.windows.net/file?sig=synthetic-signature"',
            f'"https://example.blob.core.windows.net/file?sig={REDACTED_SECRET}"',
        ),
        (
            "https://bucket.s3.amazonaws.com/object?X-Amz-Signature=deadbeef0123456789",
            f"https://bucket.s3.amazonaws.com/object?X-Amz-Signature={REDACTED_SECRET}",
        ),
        (
            "https://storage.googleapis.com/object?X-Goog-Signature=deadbeef0123456789",
            f"https://storage.googleapis.com/object?X-Goog-Signature={REDACTED_SECRET}",
        ),
    ],
)
def test_redact_text_masks_restricted_keys_webhooks_and_url_signatures(
    source: str, expected: str
) -> None:
    result = redact_text(source)

    assert result == expected
    assert redact_payload({"message": source}) == {"message": expected}
    assert redact_text(result) == result


@pytest.mark.parametrize(
    "source",
    [
        "https://example.blob.core.windows.net/container/file?sp=r&sv=2023-11-03",
        "https://storage.example.com/file?design=value#sig=section",
        "https://hooks.slack.com/documentation",
    ],
)
def test_redact_text_preserves_urls_without_credentials(source: str) -> None:
    assert redact_text(source) == source


def test_redact_text_masks_truncated_private_key_to_end_of_input() -> None:
    private_key_type = "BEGIN RSA PRIVATE" + " KEY"
    private_key_header = f"-----{private_key_type}-----"
    source = f"before\n{private_key_header}\ntruncated-key-material"

    assert redact_text(source) == f"before\n{REDACTED_PRIVATE_KEY}"
