"""Unit tests for local commit and push execution after patch validation."""

from __future__ import annotations

import os
import subprocess
import sys
from base64 import b64encode
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from typing import Any  # noqa: E402

import scripts.git_executor as git_executor  # noqa: E402
from scripts.git_executor import (  # noqa: E402
    GitExecutorError,
    GitExecutorValidationError,
    RemediationResultValidationError,
    build_remediation_result,
    commit_and_push,
    resolve_push_credential,
    validate_remediation_git_configuration,
)
from scripts.patch_validator import validate_patch  # noqa: E402
from scripts.pr_policy_gate import decide_pr_publication  # noqa: E402
from scripts.test_executor import (  # noqa: E402
    CommandResult,
    TestExecutorResult as ValidationExecutionResult,
)
from scripts.workspace_executor import (  # noqa: E402
    ClonedWorkspace,
    WorkspaceInput,
    prepare_workspace,
)


def test_commit_and_push_uses_only_the_accepted_patch(tmp_path: Path) -> None:
    repository, config_path, base_commit = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-001",
        parent_dir=tmp_path,
    ) as workspace:
        _configure_author(workspace.path)
        changed_file = workspace.path / "models" / "orders.sql"
        changed_file.write_text("select 2 as order_id\n", encoding="utf-8")
        validation = _validated_patch(workspace, config_path)

        result = commit_and_push(workspace, validation, provider="bitbucket")

        assert result.base_commit == base_commit
        assert result.branch == "pipeline-guard/INC-GIT-001"
        assert result.patch_hash == validation["patch_hash"]
        assert _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == result.commit
        assert _git(
            repository, "rev-parse", f"refs/heads/{result.branch}"
        ).stdout.strip() == (result.commit)
        assert _git(repository, "rev-parse", "main").stdout.strip() == base_commit


def test_rejects_an_unaccepted_patch_before_staging_or_committing(
    tmp_path: Path,
) -> None:
    repository, config_path, base_commit = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-REJECTED",
        parent_dir=tmp_path,
    ) as workspace:
        changed_file = workspace.path / "models" / "orders.sql"
        changed_file.write_text("select 2 as order_id\n", encoding="utf-8")
        validation = _validated_patch(workspace, config_path)
        validation["accepted"] = False

        with pytest.raises(
            GitExecutorValidationError, match="accepted patch validation"
        ):
            commit_and_push(workspace, validation, provider="bitbucket")

        assert _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == base_commit
        assert _git(workspace.path, "diff", "--cached", "--quiet").returncode == 0
        assert (
            _git(
                repository,
                "show-ref",
                "--verify",
                "--quiet",
                "refs/heads/pipeline-guard/INC-GIT-REJECTED",
            ).returncode
            == 1
        )


def test_commit_and_push_requires_a_non_empty_provider(tmp_path: Path) -> None:
    _, config_path, base_commit = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-PROVIDER",
        parent_dir=tmp_path,
    ) as workspace:
        (workspace.path / "models" / "orders.sql").write_text(
            "select 2 as order_id\n", encoding="utf-8"
        )
        validation = _validated_patch(workspace, config_path)

        with pytest.raises(GitExecutorValidationError, match=r"repository\.provider"):
            commit_and_push(workspace, validation, provider="")

        assert _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == base_commit


def test_rejects_a_workspace_that_changed_after_validation(tmp_path: Path) -> None:
    _, config_path, base_commit = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-CHANGED",
        parent_dir=tmp_path,
    ) as workspace:
        changed_file = workspace.path / "models" / "orders.sql"
        changed_file.write_text("select 2 as order_id\n", encoding="utf-8")
        validation = _validated_patch(workspace, config_path)
        changed_file.write_text("select 3 as order_id\n", encoding="utf-8")

        with pytest.raises(GitExecutorValidationError, match="hash does not match"):
            commit_and_push(workspace, validation, provider="bitbucket")

        assert _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == base_commit
        assert _git(workspace.path, "diff", "--cached", "--quiet").returncode == 0


def test_rejects_staged_or_untracked_content_not_in_the_validated_patch(
    tmp_path: Path,
) -> None:
    _, config_path, base_commit = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-UNTRACKED",
        parent_dir=tmp_path,
    ) as workspace:
        changed_file = workspace.path / "models" / "orders.sql"
        changed_file.write_text("select 2 as order_id\n", encoding="utf-8")
        validation = _validated_patch(workspace, config_path)
        (workspace.path / "unexpected.txt").write_text(
            "not validated\n", encoding="utf-8"
        )

        with pytest.raises(GitExecutorValidationError, match="only tracked changes"):
            commit_and_push(workspace, validation, provider="bitbucket")

        assert _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == base_commit


def test_retry_after_failure_before_commit_reuses_the_staged_validated_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, config_path, base_commit = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-BEFORE-COMMIT",
        parent_dir=tmp_path,
    ) as workspace:
        _configure_author(workspace.path)
        (workspace.path / "models" / "orders.sql").write_text(
            "select 2 as order_id\n", encoding="utf-8"
        )
        validation = _validated_patch(workspace, config_path)
        original_commit = git_executor._commit_staged_patch

        def fail_before_commit(*_args: object) -> None:
            raise GitExecutorError("simulated interruption before commit")

        monkeypatch.setattr(git_executor, "_commit_staged_patch", fail_before_commit)
        with pytest.raises(GitExecutorError, match="before commit"):
            commit_and_push(workspace, validation, provider="bitbucket")

        assert _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == base_commit
        assert _git(workspace.path, "diff", "--cached", "--quiet").returncode == 1

        monkeypatch.setattr(git_executor, "_commit_staged_patch", original_commit)
        result = commit_and_push(workspace, validation, provider="bitbucket")

        assert _commit_count(workspace.path, base_commit, result.commit) == "1"
        assert _remote_commit(repository, result.branch) == result.commit


def test_retry_after_failure_between_commit_and_push_reuses_the_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, config_path, base_commit = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-BETWEEN-COMMIT-PUSH",
        parent_dir=tmp_path,
    ) as workspace:
        _configure_author(workspace.path)
        (workspace.path / "models" / "orders.sql").write_text(
            "select 2 as order_id\n", encoding="utf-8"
        )
        validation = _validated_patch(workspace, config_path)
        original_push = git_executor._push_incident_branch

        def fail_before_push(*_args: object, **_kwargs: object) -> None:
            raise GitExecutorError("simulated interruption between commit and push")

        monkeypatch.setattr(git_executor, "_push_incident_branch", fail_before_push)
        with pytest.raises(GitExecutorError, match="between commit and push"):
            commit_and_push(workspace, validation, provider="bitbucket")

        commit = _git(workspace.path, "rev-parse", "HEAD").stdout.strip()
        assert commit != base_commit
        assert _remote_commit(repository, workspace.incident_branch) is None

        monkeypatch.setattr(git_executor, "_push_incident_branch", original_push)
        result = commit_and_push(workspace, validation, provider="bitbucket")

        assert result.commit == commit
        assert _commit_count(workspace.path, base_commit, result.commit) == "1"
        assert _remote_commit(repository, result.branch) == result.commit


def test_retry_after_push_skips_a_duplicate_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config_path, base_commit = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-AFTER-PUSH",
        parent_dir=tmp_path,
    ) as workspace:
        _configure_author(workspace.path)
        (workspace.path / "models" / "orders.sql").write_text(
            "select 2 as order_id\n", encoding="utf-8"
        )
        validation = _validated_patch(workspace, config_path)
        first = commit_and_push(workspace, validation, provider="bitbucket")

        def unexpected_duplicate_push(*_args: object) -> None:
            pytest.fail("a successful push must not be attempted again")

        monkeypatch.setattr(
            git_executor, "_push_incident_branch", unexpected_duplicate_push
        )
        retried = commit_and_push(workspace, validation, provider="bitbucket")

        assert retried == first
        assert _commit_count(workspace.path, base_commit, retried.commit) == "1"


def test_build_remediation_result_returns_the_schema_conformant_final_result(
    tmp_path: Path,
) -> None:
    _, config_path, base_commit = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-RESULT",
        parent_dir=tmp_path,
    ) as workspace:
        _configure_author(workspace.path)
        (workspace.path / "models" / "orders.sql").write_text(
            "select 2 as order_id\n", encoding="utf-8"
        )
        git_result = commit_and_push(
            workspace,
            _validated_patch(workspace, config_path),
            provider="bitbucket",
        )
        result = build_remediation_result(
            incident_id="INC-GIT-RESULT",
            workspace=workspace,
            git_result=git_result,
            test_result=ValidationExecutionResult(
                run_id="run-result",
                validation_status="ALL_PASSED",
                results=[
                    CommandResult(
                        name="dbt-build",
                        status="PASSED",
                        duration_seconds=1.5,
                        log_ref="dbt-build-log",
                    )
                ],
            ),
            draft_pr={"id": "42", "url": "https://example.invalid/pull-requests/42"},
            pr_validation_status="VALIDATED",
            limitations=["The source snapshot is bounded to the approved run."],
            warnings=[],
            log_references=["remediation-log"],
        )

    assert result["status"] == "DRAFT_PR_PUBLISHED"
    assert result["branch"] == "pipeline-guard/INC-GIT-RESULT"
    assert result["files_changed"] == [
        {"path": "models/orders.sql", "lines_added": 1, "lines_removed": 1}
    ]
    assert result["draft_pr"]["id"] == "42"
    assert result["tests"][0]["command"] == "dbt-build"
    assert result["tests"][0]["log_reference"] == "dbt-build-log"


def test_build_remediation_result_rejects_inconsistent_policy_status(
    tmp_path: Path,
) -> None:
    _, config_path, base_commit = _repository_with_config(tmp_path)
    test_result = ValidationExecutionResult(
        run_id="run-failed",
        validation_status="HAS_FAILURES",
        results=[
            CommandResult(
                name="dbt-build",
                status="FAILED",
                duration_seconds=1.5,
                log_ref=None,
            )
        ],
    )

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-RESULT-INVALID",
        parent_dir=tmp_path,
    ) as workspace:
        _configure_author(workspace.path)
        (workspace.path / "models" / "orders.sql").write_text(
            "select 2 as order_id\n", encoding="utf-8"
        )
        git_result = commit_and_push(
            workspace,
            _validated_patch(workspace, config_path),
            provider="bitbucket",
        )

        with pytest.raises(
            RemediationResultValidationError, match="VALIDATED requires"
        ):
            build_remediation_result(
                incident_id="INC-GIT-RESULT-INVALID",
                workspace=workspace,
                git_result=git_result,
                test_result=test_result,
                draft_pr={"id": "43"},
                pr_validation_status="VALIDATED",
            )


def test_build_remediation_result_records_a_blocked_validation_without_a_draft_pr(
    tmp_path: Path,
) -> None:
    _, config_path, base_commit = _repository_with_config(tmp_path)
    test_result = ValidationExecutionResult(
        run_id="run-blocked",
        validation_status="HAS_FAILURES",
        results=[
            CommandResult(
                name="dbt-build",
                status="FAILED",
                duration_seconds=1.5,
                log_ref="dbt-build-log",
            )
        ],
    )
    policy = decide_pr_publication(
        test_result.validation_status,
        allow_unvalidated_draft=False,
    )

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-RESULT-BLOCKED",
        parent_dir=tmp_path,
    ) as workspace:
        _configure_author(workspace.path)
        (workspace.path / "models" / "orders.sql").write_text(
            "select 2 as order_id\n", encoding="utf-8"
        )
        git_result = commit_and_push(
            workspace,
            _validated_patch(workspace, config_path),
            provider="bitbucket",
        )
        result = build_remediation_result(
            incident_id="INC-GIT-RESULT-BLOCKED",
            workspace=workspace,
            git_result=git_result,
            test_result=test_result,
            draft_pr=None,
            pr_validation_status=policy.status,
            reason=policy.reason,
        )

    assert result["status"] == "BLOCKED_VALIDATION_FAILED"
    assert result["reason"] == policy.reason
    assert result["tests"][0]["status"] == "FAILED"
    assert "draft_pr" not in result
    assert "pr_validation_status" not in result


def test_build_remediation_result_rejects_an_inconsistent_test_aggregate(
    tmp_path: Path,
) -> None:
    _, config_path, base_commit = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-GIT-RESULT-INCONSISTENT",
        parent_dir=tmp_path,
    ) as workspace:
        _configure_author(workspace.path)
        (workspace.path / "models" / "orders.sql").write_text(
            "select 2 as order_id\n", encoding="utf-8"
        )
        git_result = commit_and_push(
            workspace,
            _validated_patch(workspace, config_path),
            provider="bitbucket",
        )

        with pytest.raises(
            RemediationResultValidationError,
            match="validation_status does not match its command results",
        ):
            build_remediation_result(
                incident_id="INC-GIT-RESULT-INCONSISTENT",
                workspace=workspace,
                git_result=git_result,
                test_result=ValidationExecutionResult(
                    run_id="run-inconsistent",
                    validation_status="ALL_PASSED",
                    results=[
                        CommandResult(
                            name="dbt-build",
                            status="FAILED",
                            duration_seconds=1.5,
                            log_ref=None,
                        )
                    ],
                ),
                draft_pr={"id": "44"},
                pr_validation_status="VALIDATED",
            )


def _configure_author(repository: Path) -> None:
    _git(repository, "config", "user.email", "pipeline-guard@example.com")
    _git(repository, "config", "user.name", "Pipeline Guard Tests")


def _commit_count(repository: Path, base_commit: str, commit: str) -> str:
    return _git(
        repository, "rev-list", "--count", f"{base_commit}..{commit}"
    ).stdout.strip()


def _remote_commit(repository: Path, branch: str | None) -> str | None:
    assert branch is not None
    result = _git(
        repository, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}"
    )
    assert result.returncode == 0
    return result.stdout.strip() or None


def _validated_patch(workspace, config_path: Path) -> dict:
    config = _load_config(config_path)
    patch = _git_bytes(
        workspace.path, "diff", "--binary", "--no-ext-diff", "HEAD", "--"
    )
    return validate_patch(
        [
            {
                "path": "models/orders.sql",
                "lines_changed": 2,
                "changed_content": "select 2 as order_id",
            }
        ],
        config,
        verified_base_commit=workspace.verified_base_commit,
        applied_patch=patch,
    )


def _repository_with_config(tmp_path: Path) -> tuple[Path, Path, str]:
    repository = tmp_path / "source-repository"
    _git(tmp_path, "init", "--initial-branch=main", str(repository))
    _git(repository, "config", "user.email", "pipeline-guard@example.com")
    _git(repository, "config", "user.name", "Pipeline Guard Tests")
    models = repository / "models"
    models.mkdir()
    (models / "orders.sql").write_text("select 1 as order_id\n", encoding="utf-8")
    _git(repository, "add", "models/orders.sql")
    _git(repository, "commit", "-m", "approved base")
    base_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()

    config_path = tmp_path / "project.yaml"
    config_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "pipeline:",
                '  adapter: "mock"',
                "repository:",
                f'  url: "{repository.as_posix()}"',
                '  default_branch: "main"',
                '  remediation_branch_prefix: "pipeline-guard"',
                "safety:",
                "  editable_paths:",
                '    - "models/**/*.sql"',
                "  editable_extensions:",
                '    - ".sql"',
                "  sensitive_paths: []",
                "  limits:",
                "    max_files: 5",
                "    max_lines_per_file: 20",
                "    max_total_lines: 20",
                "    blocked_patterns: []",
                "runtime:",
                "  execution_budget:",
                "    max_tool_calls: 1",
                "    max_seconds: 1",
                "    max_tokens: 1",
                "",
            )
        ),
        encoding="utf-8",
    )
    return repository, config_path, base_commit


def _load_config(config_path: Path) -> dict:
    import yaml

    return yaml.safe_load(config_path.read_text(encoding="utf-8"))


def _git(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _git_bytes(directory: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=True,
        capture_output=True,
    ).stdout


def test_validate_remediation_git_configuration_accepts_valid_references() -> None:
    for ref in ["env:MY_TOKEN", "${MY_TOKEN}", "secret:pipeline/deploy-token"]:
        config = {
            "repository": {
                "url": "https://github.com/org/repo.git",
                "push_credential_ref": ref,
                "allowed_git_operations": ["push_incident_branch"],
            }
        }
        validated = validate_remediation_git_configuration(config)
        assert validated["push_credential_ref"] == ref
        assert validated["allowed_git_operations"] == ["push_incident_branch"]


@pytest.mark.parametrize(
    "invalid_ref",
    [
        "ghp_super_secret_literal_token_12345",
        "BARE_ENV_VARIABLE",
        "",
        123,
    ],
)
def test_validate_remediation_git_configuration_rejects_literal_or_bare_reference(
    invalid_ref: object,
) -> None:
    config = {
        "repository": {
            "url": "https://github.com/org/repo.git",
            "push_credential_ref": invalid_ref,
            "allowed_git_operations": ["push_incident_branch"],
        }
    }
    with pytest.raises(GitExecutorValidationError, match="push_credential_ref"):
        validate_remediation_git_configuration(config)


def test_validate_remediation_git_configuration_rejects_missing_reference() -> None:
    config = {
        "repository": {
            "url": "https://github.com/org/repo.git",
            "allowed_git_operations": ["push_incident_branch"],
        }
    }
    with pytest.raises(
        GitExecutorValidationError,
        match=r"remediation requires repository\.push_credential_ref",
    ):
        validate_remediation_git_configuration(config)


def test_validate_remediation_git_configuration_rejects_url_credentials() -> None:
    config = {
        "repository": {
            "url": "https://user:token@github.com/org/repo.git",
            "push_credential_ref": "env:MY_TOKEN",
            "allowed_git_operations": ["push_incident_branch"],
        }
    }
    with pytest.raises(
        GitExecutorValidationError,
        match=r"repository\.url must not contain embedded credentials",
    ):
        validate_remediation_git_configuration(config)


@pytest.mark.parametrize(
    "url", ["http://example.com/org/repo.git", "git@example.com:org/repo.git"]
)
def test_validate_remediation_git_configuration_rejects_non_https_push_url(
    url: str,
) -> None:
    config = {
        "repository": {
            "url": url,
            "push_credential_ref": "env:MY_TOKEN",
            "allowed_git_operations": ["push_incident_branch"],
        }
    }
    with pytest.raises(GitExecutorValidationError, match="must use HTTPS"):
        validate_remediation_git_configuration(config)


@pytest.mark.parametrize(
    "invalid_ops",
    [
        None,
        [],
        ["push_incident_branch", "force_push"],
        ["merge"],
        ["delete_branch"],
    ],
)
def test_validate_remediation_git_configuration_rejects_unauthorized_operations(
    invalid_ops: object,
) -> None:
    repository: dict[str, Any] = {
        "url": "https://github.com/org/repo.git",
        "push_credential_ref": "env:MY_TOKEN",
    }
    if invalid_ops is not None:
        repository["allowed_git_operations"] = invalid_ops
    config = {"repository": repository}
    with pytest.raises(GitExecutorValidationError, match="allowed_git_operations"):
        validate_remediation_git_configuration(config)


def test_resolve_push_credential_resolves_env_and_secret() -> None:
    env = {
        "MY_TOKEN": "token-12345",
        "SECRET_PIPELINE_TOKEN": "secret-abcde",
    }
    assert (
        resolve_push_credential("env:MY_TOKEN", host_environment=env) == "token-12345"
    )
    assert resolve_push_credential("${MY_TOKEN}", host_environment=env) == "token-12345"
    assert (
        resolve_push_credential("secret:pipeline/token", host_environment=env)
        == "secret-abcde"
    )

    with pytest.raises(GitExecutorError, match="not found"):
        resolve_push_credential("env:MISSING_TOKEN", host_environment=env)

    with pytest.raises(
        GitExecutorValidationError, match="literal tokens are forbidden"
    ):
        resolve_push_credential("ghp_literal_token", host_environment=env)


def test_push_incident_branch_uses_strict_refspec_and_isolated_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config_path, base_commit = _repository_with_config(tmp_path)
    recorded_calls: list[dict[str, Any]] = []

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-REFSPEC-CHECK",
        parent_dir=tmp_path,
    ) as workspace:
        _configure_author(workspace.path)
        pre_push_hook = workspace.path / ".git" / "hooks" / "pre-push"
        pre_push_hook.write_text("#!/bin/sh\nexit 77\n", encoding="utf-8")
        pre_push_hook.chmod(0o755)
        (workspace.path / "models" / "orders.sql").write_text(
            "select 2 as order_id\n", encoding="utf-8"
        )
        validation = _validated_patch(workspace, config_path)

        original_git_text = git_executor._git_text

        def intercept_git_text(ws, *args, action="", environment=None):
            if "push" in args:
                recorded_calls.append({"args": list(args), "environment": environment})
            return original_git_text(ws, *args, action=action, environment=environment)

        monkeypatch.setattr(git_executor, "_git_text", intercept_git_text)
        monkeypatch.setenv("GITHUB_TOKEN", "leaked-personal-token")

        result = commit_and_push(
            workspace,
            validation,
            push_credential="resolved-remediation-token",
            provider="bitbucket",
        )
        assert result.branch == "pipeline-guard/INC-REFSPEC-CHECK"

    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    expected_refspec = f"refs/heads/{result.branch}:refs/heads/{result.branch}"
    assert expected_refspec in call["args"]
    assert "push" in call["args"]
    assert "--porcelain" in call["args"]
    assert "--no-verify" in call["args"]
    assert any(argument.startswith("core.hooksPath=") for argument in call["args"])
    assert "--force" not in call["args"]
    assert "--tags" not in call["args"]
    assert all("resolved-remediation-token" not in arg for arg in call["args"])

    # The resolved token is never passed on the command line. Git reads it from
    # its configuration environment, while ambient credentials stay excluded.
    assert call["environment"] is not None
    assert not git_executor._LEAKING_GIT_ENV_VARS.intersection(call["environment"])
    assert call["environment"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert call["environment"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert call["environment"]["GIT_CONFIG_COUNT"] == "1"
    assert call["environment"]["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert call["environment"][
        "GIT_CONFIG_VALUE_0"
    ] == "Authorization: Basic " + b64encode(
        b"x-token-auth:resolved-remediation-token"
    ).decode("ascii")


def test_remote_branch_check_uses_isolated_credential_for_https_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = WorkspaceInput(
        repository_url="https://example.com/org/repository.git",
        base_branch="main",
        base_commit="a" * 40,
        incident_branch="pipeline-guard/INC-REMOTE-CREDENTIAL",
    )
    recorded_calls: list[dict[str, Any]] = []

    def intercept_git_text(_ws, *args, action="", environment=None):
        del action
        recorded_calls.append({"args": list(args), "environment": environment})
        return ""

    monkeypatch.setattr(git_executor, "_git_text", intercept_git_text)
    with ClonedWorkspace(inputs, parent_dir=tmp_path) as workspace:
        git_executor._push_incident_branch_if_needed(
            workspace,
            inputs.incident_branch,
            "b" * 40,
            push_credential="resolved-remediation-token",
            provider="bitbucket",
        )

    assert len(recorded_calls) == 2
    for call in recorded_calls:
        assert all("resolved-remediation-token" not in arg for arg in call["args"])
        assert call["environment"] is not None
        assert call["environment"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert call["environment"]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert call["environment"]["GIT_CONFIG_COUNT"] == "1"
        assert call["environment"]["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        assert call["environment"][
            "GIT_CONFIG_VALUE_0"
        ] == "Authorization: Basic " + b64encode(
            b"x-token-auth:resolved-remediation-token"
        ).decode("ascii")

    assert recorded_calls[0]["args"][:2] == ["ls-remote", "--heads"]
    assert "push" in recorded_calls[1]["args"]


def test_push_rechecks_url_before_exposing_credential_to_git(tmp_path: Path) -> None:
    inputs = WorkspaceInput(
        repository_url="http://example.com/repo.git",
        base_branch="main",
        base_commit="a" * 40,
        incident_branch="pipeline-guard/INC-42",
    )
    with (
        ClonedWorkspace(inputs, parent_dir=tmp_path) as workspace,
        pytest.raises(GitExecutorValidationError, match="must use HTTPS"),
    ):
        git_executor._push_incident_branch(
            workspace, "pipeline-guard/INC-42", "secret-token"
        )


def test_isolated_push_environment_removes_every_listed_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in git_executor._LEAKING_GIT_ENV_VARS:
        monkeypatch.setenv(variable, f"leaked-{variable.lower()}")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "C:/Users/example/.gitconfig")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")

    environment = git_executor.isolated_push_environment(
        "resolved-remediation-token",
        provider="bitbucket",
    )

    assert not git_executor._LEAKING_GIT_ENV_VARS.intersection(environment)
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_COUNT"] == "1"
    assert environment["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert environment["GIT_CONFIG_VALUE_0"] == (
        "Authorization: Basic "
        + b64encode(b"x-token-auth:resolved-remediation-token").decode("ascii")
    )


def test_isolated_push_environment_uses_bearer_for_other_providers() -> None:
    environment = git_executor.isolated_push_environment(
        "resolved-remediation-token",
        provider="github",
    )

    assert (
        environment["GIT_CONFIG_VALUE_0"]
        == "Authorization: Bearer resolved-remediation-token"
    )


def test_empty_hooks_directory_is_private_per_commit_and_ignores_preplanted_path(
    tmp_path: Path,
) -> None:
    _, config_path, base_commit = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=base_commit,
        incident_id="INC-HOOKS-ISOLATION",
        parent_dir=tmp_path,
    ) as workspace:
        preplanted = workspace.path.parent / "git-executor-hooks"
        preplanted.mkdir()
        first = git_executor._empty_hooks_directory(workspace)
        second = git_executor._empty_hooks_directory(workspace)

        assert first != second
        assert first.parent == workspace.path.parent
        assert second.parent == workspace.path.parent
        assert first.name.startswith("git-executor-hooks-")
        assert second.name.startswith("git-executor-hooks-")
        assert first != preplanted
        assert second != preplanted
        assert first.is_dir()
        assert not first.is_symlink()
        assert second.is_dir()
        assert not second.is_symlink()

        first.rmdir()
        second.rmdir()
