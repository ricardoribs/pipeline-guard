"""Terminal unattended result persistence tests."""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.unattended_result import write_unattended_result  # noqa: E402


def _result() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "mode": "unattended",
        "outcome": "REPORT_ONLY",
        "reason_code": "NOT_ELIGIBLE",
        "exit_code": 3,
        "incident_id": "incident-1",
        "service_identity": "sp:pipeline-guard",
        "host_run": {
            "host": "other",
            "run_reference": "run-1",
            "related_failed_steps": ["downstream-model"],
        },
        "started_at": "2026-11-02T12:00:00Z",
        "finished_at": "2026-11-02T12:00:01Z",
        "decisions": [
            {
                "id": "remediation_gate",
                "resolution": "DOCUMENTED_DEFAULT",
                "value": False,
            }
        ],
        "diagnosis": {
            "failure_family": "DATA",
            "confidence": "HIGH",
            "cause_status": "CONFIRMED",
            "cause": "Source data was unavailable.",
            "diagnosis_hash": "sha256:diagnosis",
        },
        "eligibility": {
            "decision": "REPORT_ONLY",
            "rule_id": "eligibility.non-code-family",
            "policy_version": "1.0",
        },
        "limitations": [],
        "summary": "pending",
    }


def test_result_summary_and_artifacts_are_sanitized_and_hashed(tmp_path: Path) -> None:
    secret = "arbitrary-test-secret-value"
    stream = io.StringIO()

    result = write_unattended_result(
        _result(),
        output_dir=tmp_path,
        artifacts={"diagnosis.json": {"message": f"do not leak {secret}"}},
        secret_values=[secret],
        stdout=stream,
    )

    retained = result["artifacts"][0]
    artifact_path = tmp_path / retained["path"]
    artifact_bytes = artifact_path.read_bytes()
    assert retained["sha256"] == hashlib.sha256(artifact_bytes).hexdigest()
    assert secret not in artifact_bytes.decode()
    assert secret not in stream.getvalue()
    assert secret not in (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert secret not in (tmp_path / "result.json").read_text(encoding="utf-8")


def test_result_json_matches_the_returned_document(tmp_path: Path) -> None:
    returned = write_unattended_result(
        _result(),
        output_dir=tmp_path,
        stdout=io.StringIO(),
    )

    persisted = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert persisted == returned


def test_early_error_preserves_actionable_sanitized_detail(tmp_path: Path) -> None:
    secret = "arbitrary-test-secret-value"
    result = {
        "schema_version": "1.0",
        "mode": "unattended",
        "outcome": "ERROR",
        "exit_code": 2,
        "reason_code": "PREREQUISITE_MISSING",
        "started_at": "2026-11-02T12:00:00Z",
        "finished_at": "2026-11-02T12:00:01Z",
        "decisions": [],
        "summary": (
            "Unattended prerequisite missing: --authorization-state-dir: "
            f"filesystem rejected token={secret}"
        ),
    }

    returned = write_unattended_result(
        result,
        output_dir=tmp_path,
        secret_values=[secret],
        stdout=io.StringIO(),
    )

    summary = returned["summary"]
    assert "## Error details" in summary
    assert "--authorization-state-dir" in summary
    assert secret not in summary
    assert "token=[REDACTED_SECRET]" in summary
