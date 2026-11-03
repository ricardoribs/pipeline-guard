from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.artifact_store import ArtifactStore, LocalArtifactStore  # noqa: E402
from scripts.policy_authorization import (  # noqa: E402
    PolicyAuthorizationError,
    authorization_consumption_reference,
    issue_policy_authorization,
    validate_and_consume_policy_authorization,
    validate_policy_authorization,
)

sys.path.remove(str(PLUGIN_ROOT))

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
SECRET = "policy-secret-value-0123456789abcdef"
INCIDENT_ID = "incident-policy-001"
DIAGNOSIS_HASH = f"sha256:{'a' * 64}"
BASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
EXTERNAL_REFERENCE = "https://host.example.invalid/runs/123"


def _config(**unattended_overrides: object) -> dict[str, object]:
    return {
        "unattended": {
            "remediation_enabled": True,
            "service_identity": "sp:pipeline-guard",
            "authorization_secret_ref": "env:PIPELINE_GUARD_POLICY_SECRET",
            "authorization_max_age_seconds": 900,
            **unattended_overrides,
        }
    }


def _incident(**overrides: object) -> dict[str, object]:
    return {
        "incident_id": INCIDENT_ID,
        "host_run": {
            "run_url": EXTERNAL_REFERENCE,
            "run_reference": "run-123",
        },
        "code_version": {"commit": BASE_COMMIT},
        **overrides,
    }


def _issue(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "config": _config(),
        "incident": _incident(),
        "diagnosis_hash": DIAGNOSIS_HASH,
        "eligibility_decision": "ELIGIBLE_FOR_REMEDIATION",
        "current_time": NOW,
        "host_environment": {"PIPELINE_GUARD_POLICY_SECRET": SECRET},
        **overrides,
    }
    return issue_policy_authorization(**arguments)  # type: ignore[arg-type]


def _validate(authorization: object, **overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "config": _config(),
        "expected_incident_id": INCIDENT_ID,
        "expected_diagnosis_hash": DIAGNOSIS_HASH,
        "expected_base_commit": BASE_COMMIT,
        "expected_external_reference": EXTERNAL_REFERENCE,
        "current_time": NOW,
        "host_environment": {"PIPELINE_GUARD_POLICY_SECRET": SECRET},
        **overrides,
    }
    return validate_policy_authorization(authorization, **arguments)  # type: ignore[arg-type]


def _validate_and_consume(
    authorization: object, *, artifact_store: ArtifactStore, **overrides: object
) -> tuple[dict[str, object], str]:
    arguments: dict[str, object] = {
        "config": _config(),
        "expected_incident_id": INCIDENT_ID,
        "expected_diagnosis_hash": DIAGNOSIS_HASH,
        "expected_base_commit": BASE_COMMIT,
        "expected_external_reference": EXTERNAL_REFERENCE,
        "current_time": NOW,
        "host_environment": {"PIPELINE_GUARD_POLICY_SECRET": SECRET},
        "artifact_store": artifact_store,
        **overrides,
    }
    return validate_and_consume_policy_authorization(authorization, **arguments)  # type: ignore[arg-type]


def _durable_store(root: Path) -> LocalArtifactStore:
    return LocalArtifactStore(plugin_data_dir=root, durable=True)


def test_issued_policy_authorization_is_offline_valid_and_bound_to_incident() -> None:
    authorization = _issue()

    validated = _validate(authorization)

    assert validated == authorization
    assert authorization == {
        "schema_version": "1.0",
        "incident_id": INCIDENT_ID,
        "diagnosis_hash": DIAGNOSIS_HASH,
        "decision": "APPROVED",
        "actor": "sp:pipeline-guard",
        "decided_at": "2026-10-30T12:00:00Z",
        "channel_type": "policy",
        "external_reference": EXTERNAL_REFERENCE,
        "base_commit": BASE_COMMIT,
        "signature": authorization["signature"],
    }
    assert SECRET not in str(authorization)


@pytest.mark.parametrize(
    ("kwargs", "error_match"),
    [
        ({"eligibility_decision": "REPORT_ONLY"}, "ELIGIBLE_FOR_REMEDIATION"),
        ({"config": _config(remediation_enabled=False)}, "explicitly enabled"),
        (
            {"config": _config(service_identity="pipeline-guard.service")},
            "policy authorization profile",
        ),
        ({"incident": _incident(host_run={})}, "run_url or run_reference"),
        ({"incident": _incident(code_version={})}, "commit"),
    ],
    ids=[
        "ineligible",
        "disabled",
        "invalid-policy-actor",
        "missing-host-reference",
        "missing-base-commit",
    ],
)
def test_policy_authorization_requires_approved_unattended_context(
    kwargs: dict[str, object], error_match: str
) -> None:
    with pytest.raises(PolicyAuthorizationError, match=error_match):
        _issue(**kwargs)


def test_forged_policy_authorization_is_rejected_without_secret_leakage() -> None:
    authorization = _issue()
    authorization["actor"] = "sp:forged"

    with pytest.raises(PolicyAuthorizationError) as captured:
        _validate(authorization)

    assert "authentication failed" in str(captured.value)
    assert SECRET not in str(captured.value)


def test_policy_authorization_rejects_another_configured_service_identity() -> None:
    authorization = _issue(config=_config(service_identity="sp:another-service"))

    with pytest.raises(PolicyAuthorizationError, match="actor"):
        _validate(authorization)


def test_policy_authorization_rejects_another_host_run() -> None:
    authorization = _issue(
        incident=_incident(
            host_run={"run_url": "https://host.example.invalid/runs/other"}
        )
    )

    with pytest.raises(PolicyAuthorizationError, match="external_reference"):
        _validate(authorization)


@pytest.mark.parametrize(
    ("kwargs", "error_match"),
    [
        ({"current_time": NOW + timedelta(seconds=901)}, "expired"),
        ({"expected_diagnosis_hash": f"sha256:{'b' * 64}"}, "diagnosis_hash"),
        ({"expected_base_commit": "f" * 40}, "base_commit"),
    ],
    ids=["expired", "different-diagnosis", "different-commit"],
)
def test_policy_authorization_rejects_invalid_use_context(
    kwargs: dict[str, object], error_match: str
) -> None:
    with pytest.raises(PolicyAuthorizationError, match=error_match):
        _validate(_issue(), **kwargs)


def test_policy_authorization_can_be_consumed_only_once(tmp_path: Path) -> None:
    authorization = _issue()
    store = _durable_store(tmp_path)

    validated, reference = _validate_and_consume(authorization, artifact_store=store)

    assert validated == authorization
    assert reference.startswith("https://host.example.invalid/runs/123:")
    with pytest.raises(PolicyAuthorizationError, match="already consumed"):
        _validate_and_consume(authorization, artifact_store=store)


def test_signature_hex_case_cannot_bypass_single_use(tmp_path: Path) -> None:
    authorization = _issue()
    store = _durable_store(tmp_path)
    _, reference = _validate_and_consume(authorization, artifact_store=store)
    case_variant = dict(authorization)
    prefix, digest = str(case_variant["signature"]).split(":", 1)
    case_variant["signature"] = f"{prefix}:{digest.upper()}"

    assert case_variant["signature"] != authorization["signature"]
    assert authorization_consumption_reference(case_variant) == reference
    with pytest.raises(PolicyAuthorizationError, match="already consumed"):
        _validate_and_consume(case_variant, artifact_store=store)


def test_session_scoped_store_is_rejected_before_consumption(tmp_path: Path) -> None:
    authorization = _issue()
    store = _durable_store(tmp_path)
    reference = authorization_consumption_reference(authorization)

    for _ in range(2):
        with store.session() as session, pytest.raises(TypeError, match="durable"):
            _validate_and_consume(authorization, artifact_store=session)

    assert store.exists("authorization-consumption", reference) is False


def test_store_without_durable_contract_is_rejected_before_consumption(
    tmp_path: Path,
) -> None:
    authorization = _issue()
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    reference = authorization_consumption_reference(authorization)

    with pytest.raises(TypeError, match="durable"):
        _validate_and_consume(authorization, artifact_store=store)

    assert store.exists("authorization-consumption", reference) is False


def test_default_plugin_data_store_is_rejected_before_consumption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authorization = _issue()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    store = LocalArtifactStore()
    reference = authorization_consumption_reference(authorization)

    with pytest.raises(TypeError, match="durable"):
        _validate_and_consume(authorization, artifact_store=store)

    assert store.exists("authorization-consumption", reference) is False


def test_concurrent_consumption_allows_only_one_creation(tmp_path: Path) -> None:
    authorization = _issue()
    store = _durable_store(tmp_path)

    def consume() -> bool:
        try:
            _validate_and_consume(authorization, artifact_store=store)
        except PolicyAuthorizationError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: consume(), range(2)))

    assert results.count(True) == 1
    assert results.count(False) == 1


def test_invalid_authorization_is_not_consumed(tmp_path: Path) -> None:
    authorization = _issue()
    authorization["actor"] = "sp:forged"
    store = _durable_store(tmp_path)
    reference = authorization_consumption_reference(authorization)

    with pytest.raises(PolicyAuthorizationError, match="authentication failed"):
        _validate_and_consume(authorization, artifact_store=store)

    assert store.exists("authorization-consumption", reference) is False


def test_consumption_artifact_does_not_contain_authorization_secret(
    tmp_path: Path,
) -> None:
    authorization = _issue()
    store = _durable_store(tmp_path)

    _, reference = _validate_and_consume(authorization, artifact_store=store)

    artifact = store.get("authorization-consumption", reference)
    assert artifact == b"consumed"
    assert SECRET.encode("utf-8") not in artifact


def test_missing_secret_raises_actionable_error() -> None:
    with pytest.raises(PolicyAuthorizationError) as captured:
        _issue(host_environment={})

    assert "not found" in str(captured.value)


def test_short_secret_does_not_appear_in_error() -> None:
    short_secret = "too-short"

    with pytest.raises(PolicyAuthorizationError) as captured:
        _issue(host_environment={"PIPELINE_GUARD_POLICY_SECRET": short_secret})

    assert "at least 32 bytes" in str(captured.value)
    assert short_secret not in str(captured.value)
