"""Tests for the standalone Pipeline Guard draft-PR publication boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from base64 import b64encode
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from scripts import pr_publication
from scripts.pr_publication import (
    PipelineGuardDraftPRPublisher,
    PRPublicationError,
    PushedIncidentBranch,
    _load_publication_input,
    _repository_identity_from_url,
    pipeline_guard_publisher_from_repository,
    publish_draft_pr,
    publish_from_config,
)
from scripts.pr_request_builder import PRRequest
from scripts.test_executor import (
    CommandResult,
)
from scripts.test_executor import (
    TestExecutorResult as ValidationExecutionResult,
)


@dataclass
class RecordingRepairPRPublisher:
    calls: list[tuple[PRRequest, str, str]] = field(default_factory=list)

    def publish_or_reuse_draft(
        self,
        request: PRRequest,
        *,
        incident_id: str,
        run_id: str,
    ) -> dict[str, str]:
        self.calls.append((request, incident_id, run_id))
        return {"id": "42", "url": "https://example.invalid/pull-requests/42"}


def _request(*, draft: bool = True) -> PRRequest:
    return PRRequest(
        title="fix: remediate CODE failure",
        body="A neutral request.",
        branch="pipeline-guard/INC-42",
        base="main",
        draft=draft,
    )


def _test_result(status: str, *, run_id: str = "run-42") -> ValidationExecutionResult:
    results = {
        "ALL_PASSED": [CommandResult("test", "PASSED", 1.0, None)],
        "HAS_FAILURES": [CommandResult("test", "FAILED", 1.0, None)],
        "INCOMPLETE": [],
    }[status]
    return ValidationExecutionResult(
        run_id=run_id, results=results, validation_status=status
    )


def _pushed_branch(*, branch: str = "pipeline-guard/INC-42") -> PushedIncidentBranch:
    return PushedIncidentBranch(branch=branch, commit="a" * 40)


@pytest.mark.parametrize("prefix", ["", "\ufeff"])
def test_publication_input_accepts_json_with_optional_utf8_bom(prefix: str) -> None:
    assert _load_publication_input(StringIO(f'{prefix}{{"run_id": "run-42"}}')) == {
        "run_id": "run-42"
    }


def test_blocked_policy_never_invokes_draft_pr_publisher() -> None:
    publisher = RecordingRepairPRPublisher()

    result = publish_draft_pr(
        _request(),
        incident_id="INC-42",
        run_id="run-42",
        test_result=_test_result("HAS_FAILURES"),
        pushed_branch=_pushed_branch(),
        allow_unvalidated_draft=False,
        publisher=publisher,
    )

    assert result.decision.status == "BLOCKED"
    assert result.draft_pr is None
    assert publisher.calls == []


@pytest.mark.parametrize(
    ("test_status", "allow_unvalidated_draft", "expected_status"),
    [
        ("ALL_PASSED", False, "VALIDATED"),
        ("HAS_FAILURES", True, "PUBLISHED_UNVALIDATED"),
    ],
)
def test_allowed_policy_invokes_draft_pr_publisher_once(
    test_status: str,
    allow_unvalidated_draft: bool,
    expected_status: str,
) -> None:
    publisher = RecordingRepairPRPublisher()

    result = publish_draft_pr(
        _request(),
        incident_id="INC-42",
        run_id="run-42",
        test_result=_test_result(test_status),
        pushed_branch=_pushed_branch(),
        allow_unvalidated_draft=allow_unvalidated_draft,
        publisher=publisher,
    )

    assert result.decision.status == expected_status
    assert result.draft_pr == {
        "id": "42",
        "url": "https://example.invalid/pull-requests/42",
    }
    assert publisher.calls == [(_request(), "INC-42", "run-42")]


def test_only_a_draft_request_can_be_published() -> None:
    with pytest.raises(PRPublicationError, match="draft PRRequest"):
        publish_draft_pr(
            _request(draft=False),
            incident_id="INC-42",
            run_id="run-42",
            test_result=_test_result("ALL_PASSED"),
            pushed_branch=_pushed_branch(),
            allow_unvalidated_draft=False,
            publisher=RecordingRepairPRPublisher(),
        )


def test_publication_rejects_a_test_result_that_disagrees_with_its_commands() -> None:
    with pytest.raises(PRPublicationError, match="disagrees"):
        publish_draft_pr(
            _request(),
            incident_id="INC-42",
            run_id="run-42",
            test_result=ValidationExecutionResult(
                run_id="run-42",
                results=[CommandResult("test", "FAILED", 1.0, None)],
                validation_status="ALL_PASSED",
            ),
            pushed_branch=_pushed_branch(),
            allow_unvalidated_draft=False,
            publisher=RecordingRepairPRPublisher(),
        )


def test_publication_rejects_a_request_for_a_different_pushed_branch() -> None:
    with pytest.raises(PRPublicationError, match="pushed incident branch"):
        publish_draft_pr(
            _request(),
            incident_id="INC-42",
            run_id="run-42",
            test_result=_test_result("ALL_PASSED"),
            pushed_branch=_pushed_branch(branch="pipeline-guard/OTHER"),
            allow_unvalidated_draft=False,
            publisher=RecordingRepairPRPublisher(),
        )


def test_production_entry_derives_a_blocking_status_from_executor_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class FailingExecutor:
        def __init__(self, config, *, run_id, cwd) -> None:
            assert cwd == tmp_path
            assert run_id == "run-42"
            assert config["validation"] == {"commands": []}
            calls.append("executor")

        def run(self) -> ValidationExecutionResult:
            return _test_result("HAS_FAILURES")

    def verified_branch(
        workspace_path, *, request, repository_config, expected_commit=None
    ):
        assert workspace_path == tmp_path
        assert request == _request()
        assert (
            repository_config["url"]
            == "https://bitbucket.org/example-org/delivery-dashboard.git"
        )
        assert expected_commit in (None, "a" * 40)
        calls.append(expected_commit)
        return _pushed_branch()

    def no_call(_value: object) -> dict[str, object]:
        raise AssertionError("BLOCKED must not invoke the repair PR publisher")

    monkeypatch.setattr(pr_publication, "TestExecutor", FailingExecutor)
    monkeypatch.setattr(
        pr_publication, "_verify_pushed_incident_branch", verified_branch
    )

    result = publish_from_config(
        {
            "request": {
                "title": _request().title,
                "body": _request().body,
                "branch": _request().branch,
                "base": _request().base,
                "draft": True,
            },
            "incident_id": "INC-42",
            "run_id": "run-42",
            "workspace_path": str(tmp_path),
        },
        project_config={
            "repository": {
                "provider": "bitbucket",
                "url": "https://bitbucket.org/example-org/delivery-dashboard.git",
                "push_credential_ref": "env:PIPELINE_GUARD_TOKEN",
                "allowed_git_operations": ["push_incident_branch"],
            },
            "validation": {"commands": []},
            "remediation": {"allow_unvalidated_draft": False},
        },
        request_publisher=no_call,
    )

    assert result.decision.status == "BLOCKED"
    assert result.test_result == _test_result("HAS_FAILURES")
    assert calls == [
        None,
        "executor",
        "a" * 40,
    ]


def test_production_entry_publishes_only_after_workspace_verification_and_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verification_calls: list[str | None] = []
    provider_calls: list[dict[str, object]] = []

    class PassingExecutor:
        def __init__(self, _config, *, run_id, cwd) -> None:
            assert run_id == "run-42"
            assert cwd == tmp_path

        def run(self) -> ValidationExecutionResult:
            return _test_result("ALL_PASSED")

    def verified_branch(
        _workspace_path, *, request, repository_config, expected_commit=None
    ):
        assert request == _request()
        assert repository_config["push_credential_ref"] == "env:PIPELINE_GUARD_TOKEN"
        verification_calls.append(expected_commit)
        return _pushed_branch()

    def request_publisher(payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        provider_calls.append(payload)
        return {
            "id": "42",
            "url": "https://example.invalid/pull-requests/42",
            "reused": False,
        }

    monkeypatch.setattr(pr_publication, "TestExecutor", PassingExecutor)
    monkeypatch.setattr(
        pr_publication, "_verify_pushed_incident_branch", verified_branch
    )

    result = publish_from_config(
        {
            "request": {
                "title": _request().title,
                "body": _request().body,
                "branch": _request().branch,
                "base": _request().base,
                "draft": True,
            },
            "incident_id": "INC-42",
            "run_id": "run-42",
            "workspace_path": str(tmp_path),
        },
        project_config={
            "repository": {
                "provider": "bitbucket",
                "url": "https://bitbucket.org/example-org/delivery-dashboard.git",
                "push_credential_ref": "env:PIPELINE_GUARD_TOKEN",
                "allowed_git_operations": ["push_incident_branch"],
            },
            "remediation": {"allow_unvalidated_draft": False},
        },
        request_publisher=request_publisher,
    )

    assert verification_calls == [None, "a" * 40]
    assert result.decision.status == "VALIDATED"
    assert result.test_result == _test_result("ALL_PASSED")
    assert result.draft_pr == {
        "id": "42",
        "url": "https://example.invalid/pull-requests/42",
    }
    assert provider_calls == [
        {
            "provider": "bitbucket",
            "repository": "delivery-dashboard",
            "repository_owner": "example-org",
            "title": _request().title,
            "body": _request().body,
            "branch": _request().branch,
            "base": _request().base,
            "draft": True,
            "incident_id": "INC-42",
            "run_id": "run-42",
        }
    ]


def test_production_entry_revalidates_the_pushed_workspace_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    _git(tmp_path, "init", "--initial-branch=main", str(source))
    _git(source, "config", "user.name", "Pipeline Guard Tests")
    _git(source, "config", "user.email", "pipeline-guard@example.test")
    (source / "model.sql").write_text("select 1\n", encoding="utf-8")
    _git(source, "add", "model.sql")
    _git(source, "commit", "-m", "test base")
    _git(tmp_path, "clone", str(source), str(workspace))
    _git(workspace, "config", "user.name", "Pipeline Guard Tests")
    _git(workspace, "config", "user.email", "pipeline-guard@example.test")
    _git(workspace, "checkout", "-b", "pipeline-guard/INC-42")
    (workspace / "model.sql").write_text("select 2\n", encoding="utf-8")
    _git(workspace, "add", "model.sql")
    _git(workspace, "commit", "-m", "test remediation")
    _git(workspace, "push", "origin", "refs/heads/pipeline-guard/INC-42")
    origin_url = _git(workspace, "remote", "get-url", "origin")
    assert _git(workspace, "status", "--porcelain") == ""
    assert (
        pr_publication._workspace_git_text(
            workspace,
            "status",
            "--porcelain",
            "--untracked-files=all",
            environment=pr_publication.isolated_push_environment(
                "non-secret-test-value"
            ),
            action="inspect workspace changes",
        ).strip()
        == ""
    )
    monkeypatch.setenv("PIPELINE_GUARD_TEST_TOKEN", "non-secret-test-value")
    publisher = RecordingRepairPRPublisher()
    monkeypatch.setattr(
        pr_publication,
        "pipeline_guard_publisher_from_repository",
        lambda *_arguments, **_kwargs: publisher,
    )

    result = publish_from_config(
        {
            "request": {
                "title": _request().title,
                "body": _request().body,
                "branch": _request().branch,
                "base": _request().base,
                "draft": True,
            },
            "incident_id": "INC-42",
            "run_id": "run-42",
            "workspace_path": str(workspace),
        },
        project_config={
            "repository": {
                "provider": "bitbucket",
                "url": origin_url,
                "push_credential_ref": "env:PIPELINE_GUARD_TEST_TOKEN",
                "allowed_git_operations": ["push_incident_branch"],
            },
            "validation": {
                "commands": [
                    {"name": "clean-tree", "command": "git status --porcelain"},
                ]
            },
            "remediation": {"allow_unvalidated_draft": False},
        },
    )

    assert result.decision.status == "VALIDATED"
    assert result.test_result is not None
    assert result.test_result.validation_status == "ALL_PASSED"
    assert result.draft_pr == {
        "id": "42",
        "url": "https://example.invalid/pull-requests/42",
    }
    assert publisher.calls == [(_request(), "INC-42", "run-42")]


def test_workspace_verification_uses_an_isolated_credential_for_ls_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    commit = "a" * 40

    def git_text(_workspace, *arguments, environment, action):
        del action
        calls.append((arguments, dict(environment)))
        if arguments == ("remote", "get-url", "origin"):
            return "https://bitbucket.org/example-org/delivery-dashboard.git\n"
        if arguments == ("symbolic-ref", "--quiet", "--short", "HEAD"):
            return "pipeline-guard/INC-42\n"
        if arguments == ("status", "--porcelain", "--untracked-files=all"):
            return ""
        if arguments == ("rev-parse", "--verify", "HEAD^{commit}"):
            return f"{commit}\n"
        if arguments == (
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/pipeline-guard/INC-42",
        ):
            return f"{commit}\trefs/heads/pipeline-guard/INC-42\n"
        raise AssertionError(f"unexpected Git command: {arguments!r}")

    monkeypatch.setattr(
        pr_publication, "resolve_push_credential", lambda _reference: "token"
    )
    monkeypatch.setattr(pr_publication, "_workspace_git_text", git_text)

    verified = pr_publication._verify_pushed_incident_branch(
        tmp_path,
        request=_request(),
        repository_config={
            "provider": "bitbucket",
            "url": "https://bitbucket.org/example-org/delivery-dashboard.git",
            "push_credential_ref": "env:PIPELINE_GUARD_TOKEN",
        },
    )

    assert verified == _pushed_branch()
    ls_remote_arguments, ls_remote_environment = calls[-1]
    assert ls_remote_arguments[:2] == ("ls-remote", "--heads")
    assert ls_remote_environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert ls_remote_environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert ls_remote_environment["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert ls_remote_environment["GIT_CONFIG_VALUE_0"] == (
        "Authorization: Basic " + b64encode(b"x-token-auth:token").decode("ascii")
    )


def _git(directory: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_factory_selects_standalone_publisher_from_repository_config() -> None:
    publisher = pipeline_guard_publisher_from_repository(
        {
            "provider": "bitbucket",
            "url": "git@bitbucket.org:example-org/delivery-dashboard.git",
        },
    )

    assert isinstance(publisher, PipelineGuardDraftPRPublisher)
    assert publisher._provider == "bitbucket"
    assert publisher._repository == "delivery-dashboard"
    assert publisher._repository_owner == "example-org"


@pytest.mark.parametrize(
    ("provider", "url", "expected"),
    [
        (
            "bitbucket",
            "https://bitbucket.org/example-org/delivery.git",
            ("example-org", "delivery"),
        ),
        (
            "github",
            "https://github.com/example-org/delivery.git",
            ("example-org", "delivery"),
        ),
    ],
)
def test_repository_identity_preserves_provider_owner(
    provider: str, url: str, expected: tuple[str, str]
) -> None:
    assert _repository_identity_from_url(url, provider) == expected


@pytest.mark.parametrize("provider", ["gitlab", "azure-devops"])
def test_repository_identity_rejects_unsupported_provider(provider: str) -> None:
    with pytest.raises(PRPublicationError, match="only bitbucket and github"):
        _repository_identity_from_url(
            "https://example.test/owner/repository.git", provider
        )
