from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_CONTRACTS = REPO_ROOT / "contracts" / "schema"
UNATTENDED_GUIDE = REPO_ROOT / "docs" / "unattended" / "README.md"
AUTHORIZATION_ADR = (
    REPO_ROOT / "docs" / "adrs" / "pipeline-guard-policy-authorization.md"
)


def _load_schema(path: Path) -> dict[str, Any]:
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(schema, dict)
    Draft202012Validator.check_schema(schema)
    return schema


def test_unattended_cli_contract_transports_durable_authorization_state() -> None:
    guide = UNATTENDED_GUIDE.read_text(encoding="utf-8")
    authorization_contract = AUTHORIZATION_ADR.read_text(encoding="utf-8")

    assert "--authorization-state-dir" in guide
    assert "`unattended.remediation_enabled` is `false`" in guide
    assert "create-once" in authorization_contract


def test_airflow_run_reference_is_valid_for_incident_and_policy_approval() -> None:
    run_reference = "scheduled__2026-10-07T10:00:00+00:00"
    commit = "a" * 40
    incident = {
        "schema_version": "1.1",
        "incident_id": "incident-airflow-001",
        "source": "airflow",
        "environment": "production",
        "pipeline_id": "daily-orders",
        "run_id": run_reference,
        "failed_step": "load-orders",
        "occurred_at": "2026-10-07T10:05:00Z",
        "error_summary": "Order loading failed",
        "project_profile": "generic",
        "code_version": {"branch": "main", "commit": commit},
        "host_run": {"host": "airflow", "run_reference": run_reference},
    }
    approval = {
        "schema_version": "1.0",
        "incident_id": incident["incident_id"],
        "diagnosis_hash": "sha256:" + "b" * 64,
        "decision": "APPROVED",
        "actor": "sp:pipeline-guard",
        "decided_at": "2026-10-07T10:10:00Z",
        "channel_type": "policy",
        "external_reference": run_reference,
        "signature": "hmac-sha256:" + "c" * 64,
        "base_commit": commit,
    }

    incident_validator = Draft202012Validator(
        _load_schema(SCHEMA_CONTRACTS / "incident_input_v1.schema.json"),
        format_checker=FormatChecker(),
    )
    approval_validator = Draft202012Validator(
        _load_schema(SCHEMA_CONTRACTS / "approval_input_v1.schema.json"),
        format_checker=FormatChecker(),
    )
    policy_profile_validator = Draft202012Validator(
        _load_schema(SCHEMA_CONTRACTS / "approval_input_v1.policy_profile.schema.json")
    )

    incident_validator.validate(incident)
    approval_validator.validate(approval)
    policy_profile_validator.validate(approval)


def test_policy_profile_rejects_actor_without_service_namespace() -> None:
    approval = {
        "schema_version": "1.0",
        "incident_id": "incident-policy-001",
        "diagnosis_hash": "sha256:" + "b" * 64,
        "decision": "APPROVED",
        "actor": "pipeline-guard.service",
        "decided_at": "2026-10-07T10:10:00Z",
        "channel_type": "policy",
        "external_reference": "host-run-001",
        "signature": "hmac-sha256:" + "c" * 64,
        "base_commit": "a" * 40,
    }
    policy_profile_validator = Draft202012Validator(
        _load_schema(SCHEMA_CONTRACTS / "approval_input_v1.policy_profile.schema.json")
    )

    errors = list(policy_profile_validator.iter_errors(approval))

    assert len(errors) == 1
    assert list(errors[0].absolute_path) == ["actor"]
