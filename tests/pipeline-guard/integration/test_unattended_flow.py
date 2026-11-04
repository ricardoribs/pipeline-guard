"""Offline integration coverage for all unattended terminal outcomes."""

from __future__ import annotations

import os
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
from scripts.authoring_runner import AuthoringLimitReached, AuthoringResult  # noqa: E402
from scripts.execution_budget import ExecutionBudgetExceeded  # noqa: E402
from scripts.git_executor import GitExecutionResult  # noqa: E402
from scripts.patch_validator import PatchValidationError  # noqa: E402
from scripts.pr_policy_gate import PRPolicyDecision  # noqa: E402
from scripts.pr_publication import DraftPRPublication, PRPublicationError  # noqa: E402
from scripts.test_executor import (  # noqa: E402
    CommandResult,
)
from scripts.test_executor import (
    TestExecutorResult as ValidationExecutionResult,
)
from scripts.unattended_cli import RepairServices, run_unattended  # noqa: E402
from scripts.unattended_preflight import UnattendedPreflightResult  # noqa: E402
from scripts.workspace_executor import WorkspaceExecutionError  # noqa: E402

COMMIT = "a" * 40


@dataclass
class FakeWorkspace:
    path: Path
    incident_branch: str | None
    verified_base_commit: str | None = COMMIT
    discarded: bool = False
    closed: bool = False
    confirmed_discard: bool = False

    def close(self) -> None:
        self.closed = True

    def authorship_environment(self) -> dict[str, str]:
        return {"PATH": "safe-path", "HOME": str(self.path.parent / "safe-home")}

    def discard(self, *, confirm_unpushed_commits: bool = False) -> None:
        self.confirmed_discard = confirm_unpushed_commits
        self.discarded = True


def _config(*, remediation_enabled: bool = True) -> dict[str, Any]:
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
                "max_tool_calls": 8,
                "max_seconds": 300,
                "max_tokens": 10000,
            },
        },
        "validation": {
            "commands": [{"name": "dbt-test", "command": "true", "timeout_seconds": 30}]
        },
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
        "remediation": {"allow_unvalidated_draft": False},
        "unattended": {
            "remediation_enabled": remediation_enabled,
            "service_identity": "sp:pipeline-guard",
            "authorization_secret_ref": "env:POLICY_SECRET",
            "authorization_max_age_seconds": 900,
            "target_branch": "dev",
            "max_seconds": 300,
            "max_model_tokens": 10000,
        },
    }


def _incident() -> dict[str, Any]:
    return {
        "schema_version": "1.1",
        "incident_id": "incident-flow-1",
        "source": "ci",
        "environment": "dev",
        "pipeline_id": "orders-daily",
        "run_id": "run-flow-1",
        "failed_step": "dbt-build",
        "occurred_at": "2026-11-02T12:00:00Z",
        "error_summary": "model failed",
        "project_profile": "generic",
        "artifacts": [
            {"type": "source", "uri": "models/orders.sql", "hash": "sha256:x"}
        ],
        "code_version": {
            "repository_url": "https://bitbucket.org/example-org/orders.git",
            "branch": "dev",
            "commit": COMMIT,
        },
        "host_run": {"host": "other", "run_reference": "host-flow-1"},
    }


def _diagnosis(*, eligible: bool = True) -> dict[str, Any]:
    family = "CODE" if eligible else "DATA"
    decision = "ELIGIBLE_FOR_REMEDIATION" if eligible else "REPORT_ONLY"
    return {
        "exit_code": 0,
        "diagnosis": {
            "schema_version": "1.0",
            "incident_id": "incident-flow-1",
            "failure_family": family,
            "confidence": "HIGH",
            "cause_status": "CONFIRMED",
            "cause": "One model contains an inverted condition.",
            "affected_resources": ["models/orders.sql"],
            "evidence_citations": [],
            "limitations": [],
        },
        "eligibility_decision": {
            "schema_version": "1.0",
            "incident_id": "incident-flow-1",
            "diagnosis_hash": "sha256:flow-diagnosis",
            "decision": decision,
            "rule_id": "flow-rule",
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
    project = tmp_path / "source"
    output = tmp_path / "output"
    workspaces = tmp_path / "workspaces"
    state = tmp_path / "state"
    for path in (project, output, workspaces, state):
        path.mkdir(exist_ok=True)
    (project / "checkout-marker.txt").write_text("unchanged", encoding="utf-8")
    if not (project / ".git").is_dir():
        for arguments in (
            ("init",),
            ("config", "user.email", "pipeline-guard@example.invalid"),
            ("config", "user.name", "Pipeline Guard Test"),
            ("add", "checkout-marker.txt"),
            ("commit", "-m", "test: initialize source checkout"),
        ):
            subprocess.run(
                ["git", "-C", str(project), *arguments],
                check=True,
                capture_output=True,
                text=True,
            )
    return UnattendedPreflightResult(
        config=config,
        project_root=project,
        output_dir=output,
        workspace_parent=workspaces,
        authorization_state_dir=state,
        uv_executable="uv",
        git_executable="git",
        claude_executable="claude",
        claude_version=(2, 1, 295),
        model="fake-model",
    )


def _services(
    tmp_path: Path,
    *,
    diagnosis: dict[str, Any] | BaseException | None = None,
    validation_status: str | list[str] = "ALL_PASSED",
    authoring_error: BaseException | None = None,
    publication_error: BaseException | None = None,
    push_error: BaseException | None = None,
    prepare_error: BaseException | None = None,
    patch_validation_error: BaseException | None = None,
    existing_draft: dict[str, object] | None = None,
    committed_patch: bool = False,
    authorization_state_dir: Path | None = None,
) -> tuple[RepairServices, dict[str, int], FakeWorkspace]:
    workspace_path = tmp_path / "isolated-workspace"
    (workspace_path / "models").mkdir(parents=True, exist_ok=True)
    (workspace_path / "models" / "orders.sql").write_text(
        "select 1\n", encoding="utf-8"
    )
    workspace = FakeWorkspace(workspace_path, "")
    calls = {"diagnose": 0, "find_draft": 0, "push": 0, "publish": 0}
    repair_state = {"committed": committed_patch}
    validation_statuses = (
        [validation_status] if isinstance(validation_status, str) else validation_status
    )
    test_attempt = 0

    def diagnose(*_args: object, **_kwargs: object) -> dict[str, Any]:
        calls["diagnose"] += 1
        selected = _diagnosis() if diagnosis is None else diagnosis
        if isinstance(selected, BaseException):
            raise selected
        return selected

    def prepare(
        _config_path: Path, *, incident_id: str, **_kwargs: object
    ) -> FakeWorkspace:
        if authorization_state_dir is not None:
            assert any(path.is_file() for path in authorization_state_dir.rglob("*"))
        remote_environment = _kwargs.get("remote_environment")
        assert isinstance(remote_environment, dict)
        assert remote_environment["GIT_CONFIG_GLOBAL"] == os.devnull
        assert remote_environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert remote_environment["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        assert str(remote_environment["GIT_CONFIG_VALUE_0"]).startswith(
            "Authorization: Basic "
        )
        assert "push-token" not in repr(remote_environment)
        if prepare_error is not None:
            raise prepare_error
        workspace.incident_branch = f"pipeline-guard/{incident_id}"
        return workspace

    def find_draft(**_kwargs: object) -> dict[str, object] | None:
        calls["find_draft"] += 1
        if authorization_state_dir is not None:
            assert any(path.is_file() for path in authorization_state_dir.rglob("*"))
        return existing_draft

    def author(*_args: object, **_kwargs: object) -> AuthoringResult:
        if authoring_error is not None:
            raise authoring_error
        if authorization_state_dir is not None:
            assert str(authorization_state_dir) not in repr(_kwargs)
        return AuthoringResult(
            result={"type": "result", "is_error": False},
            events=(),
            stderr="",
            total_tokens=10,
        )

    def validate(*_args: object, **_kwargs: object):
        if patch_validation_error is not None:
            raise patch_validation_error
        patch_hash = "sha256:" + "b" * 64
        return (
            b"diff --git a/models/orders.sql b/models/orders.sql\n",
            [
                {
                    "path": "models/orders.sql",
                    "lines_changed": 2,
                    "changed_content": "select 2",
                    "lines_added": 1,
                    "lines_removed": 1,
                }
            ],
            {
                "accepted": True,
                "violations": [],
                "base_commit": COMMIT,
                "patch_hash": patch_hash,
            },
        )

    def tests(**_kwargs: object) -> ValidationExecutionResult:
        nonlocal test_attempt
        selected_status = validation_statuses[
            min(test_attempt, len(validation_statuses) - 1)
        ]
        test_attempt += 1
        command_status = "PASSED" if selected_status == "ALL_PASSED" else "FAILED"
        return ValidationExecutionResult(
            run_id="run-flow-1",
            results=[CommandResult("dbt-test", command_status, 1.0, None)],
            validation_status=selected_status,
        )

    def push(*_args: object, **_kwargs: object) -> GitExecutionResult:
        calls["push"] += 1
        assert workspace.incident_branch is not None
        if push_error is not None:
            repair_state["committed"] = True
            raise push_error
        return GitExecutionResult(
            branch=workspace.incident_branch,
            commit="c" * 40,
            base_commit=COMMIT,
            patch_hash="sha256:" + "b" * 64,
        )

    def publish(**_kwargs: object) -> DraftPRPublication:
        calls["publish"] += 1
        if publication_error is not None:
            raise publication_error
        return DraftPRPublication(
            decision=PRPolicyDecision("VALIDATED", "passed"),
            draft_pr={
                "id": "42",
                "url": "https://bitbucket.org/example-org/orders/pull-requests/42",
                "reused": False,
            },
        )

    return (
        RepairServices(
            diagnose=diagnose,
            verify_config=lambda *_args, **_kwargs: None,
            find_draft=find_draft,
            prepare=prepare,
            has_committed_patch=lambda _workspace: repair_state["committed"],
            author=author,
            validate_workspace_patch=validate,
            execute_tests=tests,
            push=push,
            publish=publish,
        ),
        calls,
        workspace,
    )


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: dict[str, Any] | None = None,
    incident: dict[str, Any] | None = None,
    diagnosis: dict[str, Any] | BaseException | None = None,
    validation_status: str | list[str] = "ALL_PASSED",
    authoring_error: BaseException | None = None,
    publication_error: BaseException | None = None,
    push_error: BaseException | None = None,
    prepare_error: BaseException | None = None,
    patch_validation_error: BaseException | None = None,
    existing_draft: dict[str, object] | None = None,
    committed_patch: bool = False,
) -> tuple[dict[str, Any], dict[str, int], FakeWorkspace, UnattendedPreflightResult]:
    active_config = config or _config()
    preflight = _preflight(tmp_path, active_config)
    services, calls, workspace = _services(
        tmp_path,
        authorization_state_dir=preflight.authorization_state_dir,
        diagnosis=diagnosis,
        validation_status=validation_status,
        authoring_error=authoring_error,
        publication_error=publication_error,
        push_error=push_error,
        prepare_error=prepare_error,
        patch_validation_error=patch_validation_error,
        existing_draft=existing_draft,
        committed_patch=committed_patch,
    )
    monkeypatch.setattr(
        unattended_cli,
        "build_remediation_result",
        lambda **_kwargs: {"schema_version": "1.0", "status": "DRAFT_PR_PUBLISHED"},
    )
    result = run_unattended(
        _incident() if incident is None else incident,
        config_path=tmp_path / "config.yaml",
        project_root=preflight.project_root,
        output_dir=preflight.output_dir,
        preflight=preflight,
        services=services,
        environment={
            "POLICY_SECRET": "s" * 32,
            "PUSH_TOKEN": "push-token",
        },
    )
    return result, calls, workspace, preflight


def test_draft_pr_published_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args, **_kwargs: pytest.fail("unattended flow requested input"),
    )
    result, calls, workspace, preflight = _run(tmp_path, monkeypatch)

    assert result["outcome"] == "DRAFT_PR_PUBLISHED"
    assert result["exit_code"] == 0
    assert calls == {"diagnose": 1, "find_draft": 1, "push": 1, "publish": 1}
    assert workspace.discarded is True
    assert workspace.confirmed_discard is True
    assert (preflight.project_root / "checkout-marker.txt").read_text() == "unchanged"
    source_status = subprocess.run(
        ["git", "-C", str(preflight.project_root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert source_status.stdout == ""


def test_workspace_execution_error_preserves_sanitized_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _, workspace, _ = _run(
        tmp_path,
        monkeypatch,
        prepare_error=WorkspaceExecutionError(
            "Git clone failed; check repository access and the configured base branch"
        ),
    )

    assert result["outcome"] == "ERROR"
    assert result["reason_code"] == "OPERATIONAL_FAILURE"
    assert result["limitations"][-1] == (
        "Git clone failed; check repository access and the configured base branch"
    )
    assert workspace.discarded is False


def test_patch_validation_error_preserves_sanitized_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _, workspace, _ = _run(
        tmp_path,
        monkeypatch,
        patch_validation_error=PatchValidationError(
            "applied_patch must be non-empty bytes from the prepared workspace"
        ),
    )

    assert result["outcome"] == "BLOCKED"
    assert result["reason_code"] == "CHANGE_LIMIT_EXCEEDED"
    assert result["limitations"][-1] == (
        "applied_patch must be non-empty bytes from the prepared workspace"
    )
    assert workspace.discarded is True


def test_existing_draft_is_reused_only_after_diagnosis_and_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls, _, _ = _run(
        tmp_path,
        monkeypatch,
        existing_draft={
            "id": "42",
            "url": "https://bitbucket.org/example-org/orders/pull-requests/42",
            "reused": True,
        },
    )

    assert result["outcome"] == "DRAFT_PR_PUBLISHED"
    assert result["draft_pr"]["reused"] is True
    assert result["failure_signature"].startswith("sha256:")
    assert result["code_version"]["commit"] == COMMIT
    assert calls == {"diagnose": 1, "find_draft": 1, "push": 0, "publish": 0}


def test_report_only_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, calls, _, _ = _run(
        tmp_path,
        monkeypatch,
        diagnosis=_diagnosis(eligible=False),
    )

    assert result["outcome"] == "REPORT_ONLY"
    assert result["reason_code"] == "NOT_ELIGIBLE"
    assert calls["push"] == calls["publish"] == 0


def test_related_failed_steps_are_reported_without_extra_publications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident = _incident()
    incident["host_run"]["related_failed_steps"] = ["quality-check", "publish-assets"]

    result, calls, _, _ = _run(tmp_path, monkeypatch, incident=incident)

    assert calls["publish"] == 1
    assert "quality-check" in result["summary"]
    assert "publish-assets" in result["summary"]


def test_blocked_validation_has_no_external_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls, _, _ = _run(
        tmp_path,
        monkeypatch,
        validation_status="HAS_FAILURES",
    )

    assert result["outcome"] == "BLOCKED"
    assert result["reason_code"] == "VALIDATION_FAILED"
    assert calls["push"] == calls["publish"] == 0


def test_failed_validation_is_reauthored_once_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls, _, preflight = _run(
        tmp_path,
        monkeypatch,
        validation_status=["HAS_FAILURES", "ALL_PASSED"],
    )

    assert result["outcome"] == "DRAFT_PR_PUBLISHED"
    assert calls["push"] == calls["publish"] == 1
    artifacts = preflight.output_dir / "artifacts"
    assert (artifacts / "authoring-result-attempt-1.json").is_file()
    assert (artifacts / "patch-validation-attempt-1.json").is_file()
    assert (artifacts / "test-result-attempt-1.json").is_file()


def test_limit_reached_stops_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls, _, _ = _run(
        tmp_path,
        monkeypatch,
        authoring_error=AuthoringLimitReached("TOKEN_LIMIT"),
    )

    assert result["outcome"] == "LIMIT_REACHED"
    assert result["reason_code"] == "TOKEN_LIMIT"
    assert calls["push"] == calls["publish"] == 0


def test_operational_error_after_push_reports_branch_and_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls, workspace, _ = _run(
        tmp_path,
        monkeypatch,
        publication_error=RuntimeError("provider unavailable"),
    )

    assert result["outcome"] == "ERROR"
    assert result["reason_code"] == "OPERATIONAL_FAILURE"
    assert result["pushed_branch"] == workspace.incident_branch
    assert calls["push"] == calls["publish"] == 1
    assert workspace.closed is True
    assert workspace.discarded is False


def test_push_failure_retains_the_committed_workspace_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls, workspace, _ = _run(
        tmp_path,
        monkeypatch,
        push_error=RuntimeError("push unavailable"),
    )

    assert result["outcome"] == "ERROR"
    assert result["reason_code"] == "OPERATIONAL_FAILURE"
    assert calls["push"] == 1
    assert calls["publish"] == 0
    assert workspace.closed is True
    assert workspace.discarded is False


def test_retry_reuses_the_committed_patch_without_authoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls, workspace, _ = _run(
        tmp_path,
        monkeypatch,
        committed_patch=True,
        authoring_error=AssertionError("authoring must not run for a committed retry"),
    )

    assert result["outcome"] == "DRAFT_PR_PUBLISHED"
    assert calls["push"] == calls["publish"] == 1
    assert workspace.discarded is True


@pytest.mark.parametrize("reason_code", ["MODEL_UNAVAILABLE", "CREDENTIAL_UNAVAILABLE"])
def test_external_unavailability_emits_documented_error_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    result, calls, _, _ = _run(
        tmp_path,
        monkeypatch,
        diagnosis=unattended_cli.UnattendedExecutionError(
            reason_code, "external service unavailable"
        ),
    )

    assert result["outcome"] == "ERROR"
    assert result["exit_code"] == 1
    assert result["reason_code"] == reason_code
    assert calls["push"] == calls["publish"] == 0


def test_target_refusal_is_blocked_and_retains_pushed_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls, workspace, _ = _run(
        tmp_path,
        monkeypatch,
        publication_error=PRPublicationError("target refused"),
    )

    assert result["outcome"] == "BLOCKED"
    assert result["reason_code"] == "PR_TARGET_REFUSED"
    assert result["pushed_branch"] == workspace.incident_branch
    assert calls["push"] == calls["publish"] == 1
    assert workspace.closed is True
    assert workspace.discarded is False


def test_pr_credential_failure_has_a_stable_reason_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publication_error = PRPublicationError("draft publication failed")
    publication_error.__cause__ = unattended_cli.RepairPRClientError(
        "Missing required environment variables: BITBUCKET_API_TOKEN"
    )
    result, calls, workspace, _ = _run(
        tmp_path,
        monkeypatch,
        publication_error=publication_error,
    )

    assert result["outcome"] == "ERROR"
    assert result["reason_code"] == "CREDENTIAL_UNAVAILABLE"
    assert result["pushed_branch"] == workspace.incident_branch
    assert calls["push"] == calls["publish"] == 1
    assert workspace.closed is True
    assert workspace.discarded is False


def test_limit_after_push_reports_branch_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExpiresAfterPush:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.check_count = 0

        @property
        def remaining_seconds(self) -> float:
            return 1.0

        @property
        def remaining_tokens(self) -> int:
            return 1000

        def check(self) -> None:
            self.check_count += 1
            if self.check_count == 4:
                raise ExecutionBudgetExceeded("max_seconds")

        def call_tool(self, operation):
            self.check()
            return operation()

        def record_tokens(self, _tokens: int) -> None:
            self.check()

    monkeypatch.setattr(unattended_cli, "UnattendedBudget", ExpiresAfterPush)
    result, calls, workspace, _ = _run(tmp_path, monkeypatch)

    assert result["outcome"] == "LIMIT_REACHED"
    assert result["reason_code"] == "TIME_LIMIT"
    assert result["pushed_branch"] == workspace.incident_branch
    assert calls["push"] == 1
    assert calls["publish"] == 0
    assert workspace.closed is True
    assert workspace.discarded is False


def test_same_policy_authorization_cannot_be_consumed_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    incident = _incident()
    fixed_authorization = unattended_cli.issue_policy_authorization(
        config=config,
        incident=incident,
        diagnosis_hash="sha256:flow-diagnosis",
        eligibility_decision="ELIGIBLE_FOR_REMEDIATION",
        host_environment={"POLICY_SECRET": "s" * 32},
    )
    monkeypatch.setattr(
        unattended_cli,
        "issue_policy_authorization",
        lambda **_kwargs: dict(fixed_authorization),
    )

    first, _, _, _ = _run(tmp_path, monkeypatch, config=config)
    second, calls, _, _ = _run(tmp_path, monkeypatch, config=config)

    assert first["outcome"] == "DRAFT_PR_PUBLISHED"
    assert second["outcome"] == "BLOCKED"
    assert second["reason_code"] == "AUTHORIZATION_INVALID"
    assert calls["push"] == calls["publish"] == 0


def test_policy_authorization_for_another_host_run_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    foreign_incident = _incident()
    foreign_incident["host_run"]["run_reference"] = "another-host-run"
    foreign_authorization = unattended_cli.issue_policy_authorization(
        config=config,
        incident=foreign_incident,
        diagnosis_hash="sha256:flow-diagnosis",
        eligibility_decision="ELIGIBLE_FOR_REMEDIATION",
        host_environment={"POLICY_SECRET": "s" * 32},
    )
    monkeypatch.setattr(
        unattended_cli,
        "issue_policy_authorization",
        lambda **_kwargs: dict(foreign_authorization),
    )

    result, calls, _, _ = _run(tmp_path, monkeypatch, config=config)

    assert result["outcome"] == "BLOCKED"
    assert result["reason_code"] == "AUTHORIZATION_INVALID"
    assert calls["push"] == calls["publish"] == 0
