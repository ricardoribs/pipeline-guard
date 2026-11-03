"""Deterministic unattended failure identity tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.failure_signature import (  # noqa: E402
    FailureSignatureError,
    attach_failure_signature,
    calculate_failure_signature,
    derive_remediation_branch,
)

COMMIT = "a" * 40


def _signature(**overrides: object) -> str:
    values = {
        "repository_url": "https://bitbucket.org/example-org/orders.git",
        "pipeline_id": "orders-daily",
        "failed_step": "dbt-build",
        "commit": COMMIT,
        "target_branch": "dev",
        **overrides,
    }
    return calculate_failure_signature(**values)


def test_identical_failures_keep_the_same_signature_and_branch() -> None:
    first = _signature()
    second = _signature(repository_url="https://BITBUCKET.ORG/example-org/orders/")

    assert first == second
    assert derive_remediation_branch("pipeline-guard", first) == (
        derive_remediation_branch("pipeline-guard", second)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_url", "https://bitbucket.org/example-org/another"),
        ("commit", "b" * 40),
        ("target_branch", "main"),
        ("pipeline_id", "another-pipeline"),
    ],
)
def test_identity_changes_produce_distinct_signatures(field: str, value: str) -> None:
    assert _signature(**{field: value}) != _signature()


def test_parallel_failed_steps_share_one_signature() -> None:
    assert _signature(failed_step="dbt-test") == _signature(failed_step="dbt-build")


def test_signature_is_attached_without_mutating_incident() -> None:
    incident = {"incident_id": "incident-1"}
    signature = _signature()

    attached = attach_failure_signature(incident, signature)

    assert "correlation" not in incident
    assert attached["correlation"]["signature"] == signature


def test_conflicting_supplied_signature_is_rejected() -> None:
    with pytest.raises(FailureSignatureError, match="conflicts"):
        attach_failure_signature(
            {"correlation": {"signature": "sha256:" + "0" * 64}},
            _signature(),
        )
