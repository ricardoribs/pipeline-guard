"""Unit tests for unattended orchestration and safe-stop decisions."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import scripts.unattended_cli as unattended_cli  # noqa: E402
from scripts.execution_budget import ExecutionBudgetExceeded  # noqa: E402
from scripts.unattended_cli import RepairServices, main, run_unattended  # noqa: E402
from scripts.unattended_preflight import UnattendedPreflightResult  # noqa: E402

COMMIT = "a" * 40


@dataclass
class PatchWorkspace:
    path: Path
    verified_base_commit: str | None
    incident_branch: str | None = "pipeline-guard/rename"

    def close(self) -> None:
        pass

    def authorship_environment(self) -> dict[str, str]:
        return {"PATH": "safe-path"}

    def discard(self, *, confirm_unpushed_commits: bool = False) -> None:
        del confirm_unpushed_commits


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _config(
    *, remediation_enabled: bool = True, target_branch: str | None = None
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "pipeline": {"adapter": "mock"},
        "repository": {
            "provider": "bitbucket",
            "url": "https://bitbucket.org/example-org/orders.git",
            "default_branch": "dev",
            "remediation_branch_prefix": "pipeline-guard",
            "push_credential_ref": "env:PUSH_TOKEN",
            "allowed_git_operations": ["push_incident_branch"],
        },
        "runtime": {
            "artifact_store": "local",
            "execution_budget": {
                "max_tool_calls": 5,
                "max_seconds": 300,
                "max_tokens": 10000,
            },
        },
        "validation": {"commands": []},
        "safety": {
            "editable_paths": ["models/**"],
            "editable_extensions": [".sql"],
            "sensitive_paths": ["seeds"],
            "limits": {
                "max_files": 2,
                "max_lines_per_file": 20,
                "max_total_lines": 30,
                "blocked_patterns": [],
            },
        },
        "eligibility": {
            "policy_version": "1.0",
            "minimum_confidence": "MEDIUM",
            "require_confirmed_cause": True,
            "required_signals": [],
        },
        "unattended": {
            "remediation_enabled": remediation_enabled,
            "service_identity": "sp:pipeline-guard",
            "authorization_secret_ref": "env:POLICY_SECRET",
            "target_branch": target_branch,
            "max_seconds": 300,
            "max_model_tokens": 10000,
        },
    }


def _incident(
    *, branch: str | None = "dev", commit: str | None = COMMIT
) -> dict[str, Any]:
    code_version = {"repository_url": "https://bitbucket.org/example-org/orders.git"}
    if branch is not None:
        code_version["branch"] = branch
    if commit is not None:
        code_version["commit"] = commit
    return {
        "schema_version": "1.1",
        "incident_id": "incident-1",
        "source": "ci",
        "environment": "dev",
        "pipeline_id": "orders-daily",
        "run_id": "run-1",
        "failed_step": "dbt-build",
        "occurred_at": "2026-11-02T12:00:00Z",
        "error_summary": "dbt model failed",
        "project_profile": "generic",
        "artifacts": [],
        "code_version": code_version,
        "host_run": {"host": "other", "run_reference": "host-run-1"},
    }


def _diagnosis(family: str, cause_status: str = "CONFIRMED") -> dict[str, Any]:
    decision = (
        "ELIGIBLE_FOR_REMEDIATION"
        if family == "CODE" and cause_status == "CONFIRMED"
        else "ESCALATE"
        if family == "UNKNOWN"
        else "REPORT_ONLY"
    )
    return {
        "exit_code": 0,
        "diagnosis": {
            "schema_version": "1.0",
            "incident_id": "incident-1",
            "failure_family": family,
            "confidence": "HIGH",
            "cause_status": cause_status,
            "cause": "Grounded cause.",
            "evidence_citations": [],
            "affected_resources": [],
            "limitations": [],
        },
        "eligibility_decision": {
            "schema_version": "1.0",
            "incident_id": "incident-1",
            "diagnosis_hash": "sha256:diagnosis",
            "decision": decision,
            "rule_id": "test-rule",
            "policy_version": "1.0",
            "reason": "test",
            "signals_used": [],
            "limitations": [],
        },
        "report": {"schema_version": "1.0"},
        "report_markdown": "# Diagnosis\n",
        "evidences": [],
    }


def _preflight(tmp_path: Path, config: dict[str, Any]) -> UnattendedPreflightResult:
    project = tmp_path / "project"
    output = tmp_path / "output"
    workspace = tmp_path / "workspaces"
    state = tmp_path / "state"
    for path in (project, output, workspace, state):
        path.mkdir(exist_ok=True)
    return UnattendedPreflightResult(
        config=config,
        project_root=project,
        output_dir=output,
        workspace_parent=workspace,
        authorization_state_dir=state,
        uv_executable="uv",
        git_executable="git",
        claude_executable="claude",
        claude_version=(2, 1, 295),
        model="test-model",
    )


def _services(
    diagnosis: dict[str, Any], *, existing: dict[str, object] | None = None
) -> RepairServices:
    def unused(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("remediation boundary must not run on a report-only path")

    return RepairServices(
        diagnose=lambda *_args, **_kwargs: diagnosis,
        verify_config=lambda *_args, **_kwargs: None,
        find_draft=lambda **_kwargs: existing,
        prepare=unused,
        has_committed_patch=lambda _workspace: False,
        author=unused,
        validate_workspace_patch=unused,
        execute_tests=unused,
        push=unused,
        publish=unused,
    )


@pytest.mark.parametrize(
    ("config", "incident", "diagnosis", "reason"),
    [
        (_config(), _incident(), _diagnosis("DATA"), "NOT_ELIGIBLE"),
        (_config(), _incident(), _diagnosis("UNKNOWN", "UNKNOWN"), "UNKNOWN_CAUSE"),
        (
            _config(remediation_enabled=False),
            _incident(),
            _diagnosis("CODE"),
            "REMEDIATION_DISABLED",
        ),
        (_config(), _incident(commit=None), _diagnosis("CODE"), "MISSING_INPUT"),
    ],
)
def test_report_only_paths_do_not_prepare_a_workspace(
    tmp_path: Path,
    config: dict[str, Any],
    incident: dict[str, Any],
    diagnosis: dict[str, Any],
    reason: str,
) -> None:
    preflight = _preflight(tmp_path, config)

    result = run_unattended(
        incident,
        config_path=tmp_path / "config.yaml",
        project_root=preflight.project_root,
        output_dir=preflight.output_dir,
        preflight=preflight,
        services=_services(diagnosis),
        environment={"POLICY_SECRET": "s" * 32, "PUSH_TOKEN": "push"},
    )

    assert result["outcome"] == "REPORT_ONLY"
    assert result["exit_code"] == 3
    assert result["reason_code"] == reason


def test_recursion_guard_stops_before_diagnosis(tmp_path: Path) -> None:
    config = _config()
    preflight = _preflight(tmp_path, config)
    services = _services(_diagnosis("CODE"))

    result = run_unattended(
        _incident(branch="pipeline-guard/unattended-deadbeef"),
        config_path=tmp_path / "config.yaml",
        project_root=preflight.project_root,
        output_dir=preflight.output_dir,
        preflight=preflight,
        services=services,
        environment={"POLICY_SECRET": "s" * 32, "PUSH_TOKEN": "push"},
    )

    assert result["outcome"] == "BLOCKED"
    assert result["reason_code"] == "RECURSION_GUARD"


def test_conflicting_input_signature_is_rejected_as_invalid_input(
    tmp_path: Path,
) -> None:
    config = _config()
    preflight = _preflight(tmp_path, config)
    incident = _incident()
    incident["correlation"] = {"signature": "sha256:" + "0" * 64}

    result = run_unattended(
        incident,
        config_path=tmp_path / "config.yaml",
        project_root=preflight.project_root,
        output_dir=preflight.output_dir,
        preflight=preflight,
        services=_services(_diagnosis("CODE")),
        environment={"POLICY_SECRET": "s" * 32, "PUSH_TOKEN": "push"},
    )

    assert result["outcome"] == "ERROR"
    assert result["exit_code"] == 2
    assert result["reason_code"] == "INVALID_INPUT"


def test_incident_repository_must_match_configured_repository(tmp_path: Path) -> None:
    config = _config()
    preflight = _preflight(tmp_path, config)
    incident = _incident()
    incident["code_version"]["repository_url"] = (
        "https://bitbucket.org/other/orders.git"
    )

    result = run_unattended(
        incident,
        config_path=tmp_path / "config.yaml",
        project_root=preflight.project_root,
        output_dir=preflight.output_dir,
        preflight=preflight,
        services=_services(_diagnosis("CODE")),
        environment={"POLICY_SECRET": "s" * 32, "PUSH_TOKEN": "push"},
    )

    assert result["outcome"] == "ERROR"
    assert result["exit_code"] == 2
    assert result["reason_code"] == "INVALID_INPUT"


def test_remediation_config_must_match_the_incident_commit(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config_path = project / "pipeline-guard.yaml"
    config_path.write_text("policy: committed\n", encoding="utf-8")
    _git(project, "init")
    _git(project, "config", "user.email", "pipeline-guard@example.invalid")
    _git(project, "config", "user.name", "Pipeline Guard Test")
    _git(project, "add", "pipeline-guard.yaml")
    _git(project, "commit", "-m", "test: add remediation config")
    commit = _git(project, "rev-parse", "HEAD")

    unattended_cli._verify_versioned_config(
        config_path, project_root=project, commit=commit
    )

    config_path.write_text("policy: changed\n", encoding="utf-8")
    with pytest.raises(unattended_cli.UnattendedExecutionError) as captured:
        unattended_cli._verify_versioned_config(
            config_path, project_root=project, commit=commit
        )

    assert captured.value.reason_code == "INVALID_INPUT"


def test_approved_read_paths_include_cited_dbt_source_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    model = workspace / "models" / "orders.sql"
    model.parent.mkdir(parents=True)
    model.write_text("select 1\n", encoding="utf-8")

    approved = unattended_cli._approved_read_paths(
        workspace,
        incident={"artifacts": []},
        diagnosis={
            "evidence_citations": [
                {"evidence_id": "incident:dbt:orders", "claim": "syntax error"}
            ]
        },
        evidences=[
            {
                "evidence_id": "incident:dbt:orders",
                "origin": "dbt",
                "content": json.dumps({"original_file_path": "models/orders.sql"}),
            }
        ],
        safety={
            "editable_paths": ["models/**/*.sql"],
            "sensitive_paths": ["config/**"],
        },
    )

    assert approved == ["models/orders.sql"]


def test_approved_read_paths_reject_uncited_and_sensitive_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    for relative in ("models/uncited.sql", "config/private.sql"):
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("select 1\n", encoding="utf-8")

    approved = unattended_cli._approved_read_paths(
        workspace,
        incident={"artifacts": []},
        diagnosis={
            "evidence_citations": [
                {"evidence_id": "incident:dbt:sensitive", "claim": "failure"}
            ]
        },
        evidences=[
            {
                "evidence_id": "incident:dbt:uncited",
                "origin": "dbt",
                "content": json.dumps({"original_file_path": "models/uncited.sql"}),
            },
            {
                "evidence_id": "incident:dbt:sensitive",
                "origin": "dbt",
                "content": json.dumps({"original_file_path": "config/private.sql"}),
            },
        ],
        safety={
            "editable_paths": ["**/*.sql"],
            "sensitive_paths": ["config/**"],
        },
    )

    assert approved == []


@pytest.mark.parametrize(
    ("configured_target", "expected_target"),
    [("main", "main"), (None, "feature/orders")],
)
def test_existing_matching_draft_is_reused_after_authorization_and_resolves_target(
    tmp_path: Path,
    configured_target: str | None,
    expected_target: str,
) -> None:
    config = _config(target_branch=configured_target)
    preflight = _preflight(tmp_path, config)
    services = _services(
        _diagnosis("CODE"),
        existing={"id": "42", "url": "https://bitbucket.org/pr/42", "reused": True},
    )

    result = run_unattended(
        _incident(branch="feature/orders"),
        config_path=tmp_path / "config.yaml",
        project_root=preflight.project_root,
        output_dir=preflight.output_dir,
        preflight=preflight,
        services=services,
        environment={"POLICY_SECRET": "s" * 32, "PUSH_TOKEN": "push"},
    )

    assert result["outcome"] == "DRAFT_PR_PUBLISHED"
    assert result["draft_pr"]["reused"] is True
    assert result["draft_pr"]["target_branch"] == expected_target


def test_mode_is_mandatory_and_only_unattended_is_accepted() -> None:
    with pytest.raises(SystemExit) as missing:
        main(
            [
                "incident.json",
                "--config",
                "config.yaml",
                "--project-root",
                ".",
                "--output-dir",
                ".",
            ]
        )
    assert missing.value.code == 2

    with pytest.raises(SystemExit) as invalid:
        main(
            [
                "incident.json",
                "--mode",
                "interactive",
                "--config",
                "config.yaml",
                "--project-root",
                ".",
                "--output-dir",
                ".",
            ]
        )
    assert invalid.value.code == 2


def test_tool_call_budget_has_a_distinct_terminal_reason() -> None:
    assert (
        unattended_cli._limit_reason(ExecutionBudgetExceeded("max_tool_calls"))
        == "TOOL_CALL_LIMIT"
    )


@pytest.mark.parametrize(
    ("failure_kind", "expected_reason"),
    [("connection", "MODEL_UNAVAILABLE"), ("authentication", "CREDENTIAL_UNAVAILABLE")],
)
def test_diagnosis_external_failures_have_stable_reason_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected_reason: str,
) -> None:
    class FakeAnthropicError(RuntimeError):
        status_code = 401

    if failure_kind == "connection":
        monkeypatch.setattr(
            unattended_cli.anthropic, "APIConnectionError", FakeAnthropicError
        )
    else:
        monkeypatch.setattr(
            unattended_cli.anthropic, "APIStatusError", FakeAnthropicError
        )

    def fail_diagnosis(*_args: object, **_kwargs: object) -> Any:
        raise FakeAnthropicError("external detail must not enter the result")

    monkeypatch.setattr(unattended_cli, "run_diagnosis", fail_diagnosis)

    with pytest.raises(unattended_cli.UnattendedExecutionError) as captured:
        unattended_cli._diagnose(
            {},
            config=_config(),
            project_root=tmp_path,
            model="test-model",
            budget=unattended_cli.UnattendedBudget(_config()),
        )

    assert captured.value.reason_code == expected_reason
    assert "external detail" not in str(captured.value)


def test_workspace_patch_accepts_rename_and_uses_destination_numstat(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    source = repository / "src" / "old_name.py"
    destination = repository / "src" / "new_name.py"
    source.parent.mkdir(parents=True)
    source.write_text("line_1 = 1\nline_2 = 2\nline_3 = 3\n", encoding="utf-8")

    _git(repository, "init")
    _git(repository, "config", "user.email", "pipeline-guard@example.invalid")
    _git(repository, "config", "user.name", "Pipeline Guard Test")
    _git(repository, "add", "src/old_name.py")
    _git(repository, "commit", "-m", "test: initialize rename fixture")
    base_commit = _git(repository, "rev-parse", "HEAD")

    _git(repository, "mv", "src/old_name.py", "src/new_name.py")
    destination.write_text(
        destination.read_text(encoding="utf-8") + "line_4 = 4\n",
        encoding="utf-8",
    )
    config = _config()
    config["safety"] = {
        "editable_paths": ["src/**"],
        "editable_extensions": [".py"],
        "sensitive_paths": [],
        "limits": {
            "max_files": 2,
            "max_lines_per_file": 10,
            "max_total_lines": 10,
            "blocked_patterns": [],
        },
    }

    _patch, patch_files, validation = unattended_cli._validate_workspace_patch(
        PatchWorkspace(repository, base_commit),
        config=config,
    )

    stats = {
        item["path"]: (item["lines_added"], item["lines_removed"])
        for item in patch_files
    }
    assert validation["accepted"] is True
    assert stats == {"src/old_name.py": (0, 0), "src/new_name.py": (1, 0)}


def test_validation_uses_the_workspace_isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    expected = object()

    class CapturingExecutor:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self) -> object:
            return expected

    monkeypatch.setenv("PIPELINE_GUARD_HOST_SECRET", "must-not-leak")
    monkeypatch.setattr(unattended_cli, "TestExecutor", CapturingExecutor)
    workspace = PatchWorkspace(tmp_path, COMMIT)

    result = unattended_cli._execute_tests(
        config={"validation": {"commands": []}},
        run_id="run-isolated-tests",
        workspace=workspace,
    )

    assert result is expected
    assert captured["environment"] == {"PATH": "safe-path"}
