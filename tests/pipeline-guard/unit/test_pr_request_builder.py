from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.pr_request_builder import (  # noqa: E402
    PRRequest,
    PRRequestBuilderError,
    build_pr_request,
)
from scripts.test_executor import (  # noqa: E402
    CommandResult,
)
from scripts.test_executor import (
    TestExecutorResult as ValidationExecutionResult,
)

sys.path.remove(str(PLUGIN_ROOT))

INCIDENT_ID = "incident-pr-001"


def _incident(
    *,
    incident_id: str = INCIDENT_ID,
    pipeline_id: str | None = "pipeline-etl",
    failed_step: str | None = "load-step",
    run_id: str | None = "run-123",
    environment: str | None = "production",
    occurred_at: str | None = "2026-10-01T12:00:00Z",
    error_summary: str | None = "Null constraint violation",
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": "1.0",
        "incident_id": incident_id,
        "source": "airflow",
        "environment": environment,
        "pipeline_id": pipeline_id,
        "run_id": run_id,
        "failed_step": failed_step,
        "occurred_at": occurred_at,
        "error_summary": error_summary,
        "project_profile": "default",
    }
    unavailable: dict[str, str] = {}
    for field, val in [
        ("environment", environment),
        ("pipeline_id", pipeline_id),
        ("run_id", run_id),
        ("failed_step", failed_step),
        ("occurred_at", occurred_at),
        ("error_summary", error_summary),
    ]:
        if val is None:
            unavailable[field] = "not available in fixture"
    if unavailable:
        doc["unavailable_fields"] = unavailable
    return doc


def _diagnosis(
    *,
    incident_id: str = INCIDENT_ID,
    failure_family: str = "CODE",
    confidence: str = "HIGH",
    cause_status: str = "CONFIRMED",
    cause: str = "Null constraint in load step.",
    impact_summary: str | None = None,
    limitations: list[str] | None = None,
    evidence_citations: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": "1.0",
        "incident_id": incident_id,
        "failure_family": failure_family,
        "confidence": confidence,
        "cause_status": cause_status,
        "cause": cause,
        "evidence_citations": (
            evidence_citations
            if evidence_citations is not None
            else [{"evidence_id": "ev-001", "claim": "Log shows null value."}]
        ),
    }
    if impact_summary is not None:
        doc["impact_summary"] = impact_summary
    if limitations is not None:
        doc["limitations"] = limitations
    return doc


def _test_result(
    *,
    run_id: str = "run-123",
    results: list[CommandResult] | None = None,
    validation_status: str = "ALL_PASSED",
) -> ValidationExecutionResult:
    if results is None:
        results = [
            CommandResult(
                name="dbt-test", status="PASSED", duration_seconds=1.5, log_ref=None
            )
        ]
    return ValidationExecutionResult(
        run_id=run_id,
        results=results,
        validation_status=validation_status,
    )


def _call(**overrides: Any) -> PRRequest:
    defaults: dict[str, Any] = {
        "incident": _incident(),
        "diagnosis": _diagnosis(),
        "branch": "fix/pipeline-etl-incident-pr-001",
        "base": "main",
        "commit": "abc1234",
        "files_changed": [
            {"path": "dbt/models/foo.sql", "lines_added": 5, "lines_removed": 2}
        ],
        "test_result": _test_result(),
    }
    defaults.update(overrides)
    return build_pr_request(**defaults)


def test_build_pr_request_returns_pr_request_with_draft_true() -> None:
    result = _call()
    assert isinstance(result, PRRequest)
    assert result.draft is True


def test_title_with_pipeline_id_and_failed_step() -> None:
    result = _call(
        incident=_incident(pipeline_id="pipeline-etl", failed_step="load-step")
    )
    assert result.title == "fix(pipeline-etl): remediate CODE failure in load-step"


def test_title_without_pipeline_id() -> None:
    result = _call(incident=_incident(pipeline_id=None, failed_step="load-step"))
    assert result.title == "fix: remediate CODE failure in load-step"


def test_title_without_failed_step() -> None:
    result = _call(incident=_incident(pipeline_id="pipeline-etl", failed_step=None))
    assert result.title == "fix(pipeline-etl): remediate CODE failure"


def test_body_contains_all_six_sections() -> None:
    result = _call()
    for header in (
        "## Incident",
        "## Diagnosis",
        "## Evidence",
        "## Files changed",
        "## Tests",
        "## Limitations",
    ):
        assert header in result.body


def test_unattended_request_exposes_the_failure_signature() -> None:
    signature = "sha256:" + "a" * 64

    result = _call(failure_signature=signature)

    assert result.failure_signature == signature
    assert f"Signature: {signature}" in result.body


def test_body_with_files_changed_shows_table() -> None:
    files = [{"path": "dbt/models/foo.sql", "lines_added": 5, "lines_removed": 2}]
    result = _call(files_changed=files)
    assert "| File | Lines added | Lines removed |" in result.body
    assert "dbt/models/foo.sql" in result.body
    assert "5" in result.body
    assert "2" in result.body


def test_body_with_empty_files_changed_shows_placeholder() -> None:
    result = _call(files_changed=[])
    assert "No files changed." in result.body


def test_body_with_tests_shows_table() -> None:
    tr = _test_result(
        results=[
            CommandResult(
                name="dbt-run", status="PASSED", duration_seconds=2.1, log_ref=None
            )
        ]
    )
    result = _call(test_result=tr)
    assert "| Command | Status | Duration (s) |" in result.body
    assert "dbt-run" in result.body


def test_body_with_empty_test_results_shows_placeholder() -> None:
    tr = _test_result(results=[], validation_status="INCOMPLETE")
    result = _call(test_result=tr)
    assert "No test commands were executed." in result.body


def test_limitations_merged_from_diagnosis_and_argument_without_duplicates() -> None:
    diag = _diagnosis(limitations=["diag-only", "shared"])
    result = _call(diagnosis=diag, limitations=["shared", "arg-only"])
    assert "- diag-only" in result.body
    assert "- shared" in result.body
    assert "- arg-only" in result.body
    assert result.body.count("- shared") == 1
    idx = result.body.index
    assert idx("diag-only") < idx("shared") < idx("arg-only")


def test_mismatched_incident_id_raises_error() -> None:
    with pytest.raises(PRRequestBuilderError, match="incident_id"):
        _call(
            incident=_incident(incident_id="incident-A"),
            diagnosis=_diagnosis(incident_id="incident-B"),
        )


def test_empty_branch_raises_error() -> None:
    with pytest.raises(PRRequestBuilderError, match="branch"):
        _call(branch="")


def test_empty_commit_raises_error() -> None:
    with pytest.raises(PRRequestBuilderError, match="commit"):
        _call(commit="")


def test_invalid_incident_raises_error() -> None:
    with pytest.raises(PRRequestBuilderError, match="incident_input_v1"):
        _call(incident={"schema_version": "1.0"})


def test_invalid_diagnosis_raises_error() -> None:
    with pytest.raises(PRRequestBuilderError, match="pipeline_diagnosis_v1"):
        _call(diagnosis={"schema_version": "1.0"})


def test_test_result_without_validation_status_raises_error() -> None:
    bad_result = SimpleNamespace(run_id="x", results=[])
    with pytest.raises(PRRequestBuilderError, match="validation_status"):
        _call(test_result=bad_result)


def test_commit_appears_in_body() -> None:
    result = _call(commit="deadbeef")
    assert "deadbeef" in result.body


def test_run_id_appears_in_tests_section() -> None:
    result = _call(
        incident=_incident(run_id="run-xyz"),
        test_result=_test_result(run_id="run-xyz"),
    )
    assert "run-xyz" in result.body


def test_body_renders_run_ids_and_durations_as_inline_code() -> None:
    run_id = "674894380237995"
    result = _call(
        incident=_incident(run_id=run_id),
        test_result=_test_result(
            run_id=run_id,
            results=[
                CommandResult(
                    name="pilot-fixture-baseline",
                    status="PASSED",
                    duration_seconds=0.26500000000010004,
                    log_ref=None,
                )
            ],
        ),
    )

    assert result.body.count(f"Run: `{run_id}`") == 2
    assert "| pilot-fixture-baseline | PASSED | `0.265` |" in result.body


def test_mismatched_run_id_raises_error() -> None:
    with pytest.raises(PRRequestBuilderError, match="run_id"):
        _call(
            incident=_incident(run_id="run-A"),
            test_result=_test_result(run_id="run-B"),
        )
