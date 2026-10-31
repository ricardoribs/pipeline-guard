# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0", "pyyaml>=6.0.3", "requests>=2.31"]
# ///
"""Policy-gated boundary for publishing a Pipeline Guard draft PR."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.draft_pr_publisher import publish_request as publish_draft_pr_request
from scripts.git_executor import (
    isolated_push_environment,
    resolve_push_credential,
    validate_remediation_git_configuration,
)
from scripts.pr_policy_gate import PRPolicyDecision, decide_pr_publication
from scripts.pr_request_builder import PRRequest
from scripts.test_executor import CommandResult, TestExecutor, TestExecutorResult
from scripts.validate_config import validate_config

_GIT_VERIFICATION_TIMEOUT_SECONDS = 30


class PRPublicationError(ValueError):
    """Raised when a host-provided draft-PR publication result is invalid."""


class DraftPRPublisher(Protocol):
    """Narrow adapter for Pipeline Guard draft PR publication.

    A repeated call with the same incident and run identifiers must reuse the
    existing draft PR rather than create another one.
    """

    def publish_or_reuse_draft(
        self,
        request: PRRequest,
        *,
        incident_id: str,
        run_id: str,
    ) -> Mapping[str, object]:
        """Create or reuse one repair draft PR."""
        ...


class _DraftPRRequestPublisher(Protocol):
    """Callable contract for the draft PR provider script."""

    def __call__(self, value: object, /) -> dict[str, object]: ...


class PipelineGuardDraftPRPublisher:
    """Publish only the draft PR produced by a Pipeline Guard execution."""

    def __init__(
        self,
        *,
        provider: object,
        repository: object,
        repository_owner: object,
        request_publisher: _DraftPRRequestPublisher = publish_draft_pr_request,
    ) -> None:
        self._provider = _required_text(provider, "repository.provider").lower()
        if self._provider not in {"bitbucket", "github"}:
            raise PRPublicationError(
                "Pipeline Guard draft PR publication supports only bitbucket and github"
            )
        self._repository = _required_text(repository, "repository")
        self._repository_owner = _required_text(repository_owner, "repository_owner")
        self._request_publisher = request_publisher

    def publish_or_reuse_draft(
        self,
        request: PRRequest,
        *,
        incident_id: str,
        run_id: str,
    ) -> dict[str, str]:
        """Publish one neutral repair request and expose only the PR reference."""
        payload = {
            "provider": self._provider,
            "repository": self._repository,
            "repository_owner": self._repository_owner,
            "title": request.title,
            "body": request.body,
            "branch": request.branch,
            "base": request.base,
            "draft": True,
            "incident_id": incident_id,
            "run_id": run_id,
        }
        try:
            response = self._request_publisher(payload)
        except (OSError, ValueError) as error:
            raise PRPublicationError(
                "Pipeline Guard draft-PR publication failed"
            ) from error
        return _normalized_repair_reference(response)


def pipeline_guard_publisher_from_repository(
    repository_config: object,
    *,
    request_publisher: _DraftPRRequestPublisher = publish_draft_pr_request,
) -> PipelineGuardDraftPRPublisher:
    """Select the draft PR publisher from validated repository config."""
    if not isinstance(repository_config, Mapping):
        raise PRPublicationError("repository configuration must be an object")
    provider = repository_config.get("provider")
    repository_url = _required_text(repository_config.get("url"), "repository.url")
    normalized_provider = _required_text(provider, "repository.provider").lower()
    owner, name = _repository_identity_from_url(repository_url, normalized_provider)
    return PipelineGuardDraftPRPublisher(
        provider=provider,
        repository=name,
        repository_owner=owner,
        request_publisher=request_publisher,
    )


@dataclass(frozen=True)
class DraftPRPublication:
    """A policy decision and, when allowed, the repair draft PR reference."""

    decision: PRPolicyDecision
    draft_pr: dict[str, str] | None
    test_result: TestExecutorResult | None = None


@dataclass(frozen=True)
class PushedIncidentBranch:
    """The exact incident branch and commit verified on the configured origin."""

    branch: str
    commit: str


def publish_draft_pr(
    request: PRRequest,
    *,
    incident_id: object,
    run_id: object,
    test_result: object,
    pushed_branch: object,
    allow_unvalidated_draft: object,
    publisher: DraftPRPublisher | None,
) -> DraftPRPublication:
    """Publish only from executor evidence for the pushed incident branch."""
    normalized_incident_id = _required_text(incident_id, "incident_id")
    normalized_run_id = _required_text(run_id, "run_id")
    if not isinstance(request, PRRequest) or request.draft is not True:
        raise PRPublicationError("request must be a neutral draft PRRequest")
    normalized_test_result = _validated_test_result(
        test_result, run_id=normalized_run_id
    )
    _validated_pushed_branch(pushed_branch, request=request)

    decision = decide_pr_publication(
        normalized_test_result.validation_status,
        allow_unvalidated_draft=allow_unvalidated_draft,
    )
    if decision.status == "BLOCKED":
        return DraftPRPublication(
            decision=decision,
            draft_pr=None,
            test_result=normalized_test_result,
        )
    if publisher is None:
        raise PRPublicationError(
            "a draft-PR publisher is required after policy approval"
        )

    reference = publisher.publish_or_reuse_draft(
        request,
        incident_id=normalized_incident_id,
        run_id=normalized_run_id,
    )
    return DraftPRPublication(
        decision=decision,
        draft_pr=_normalized_draft_pr(reference),
        test_result=normalized_test_result,
    )


def publish_from_config(
    value: object,
    *,
    project_config: object,
    request_publisher: _DraftPRRequestPublisher = publish_draft_pr_request,
) -> DraftPRPublication:
    """Production entry point: load the project policy before any Git-hosting call."""
    if not isinstance(value, Mapping) or set(value) != {
        "request",
        "incident_id",
        "run_id",
        "workspace_path",
    }:
        raise PRPublicationError("publication input has unsupported or missing fields")
    if not isinstance(project_config, Mapping):
        raise PRPublicationError("project configuration must be an object")
    remediation = project_config.get("remediation", {})
    repository = project_config.get("repository")
    if not isinstance(remediation, Mapping):
        raise PRPublicationError("project_config.remediation must be an object")
    try:
        validate_remediation_git_configuration(project_config)
    except ValueError as error:
        raise PRPublicationError(
            "project configuration does not permit remediation Git access"
        ) from error
    request = _request_from_mapping(value["request"])
    normalized_run_id = _required_text(value["run_id"], "run_id")
    workspace_path = _workspace_path(value["workspace_path"])
    verified_branch = _verify_pushed_incident_branch(
        workspace_path,
        request=request,
        repository_config=repository,
    )
    try:
        test_result = TestExecutor(
            project_config,
            run_id=normalized_run_id,
            cwd=workspace_path,
        ).run()
    except (OSError, RuntimeError, ValueError) as error:
        raise PRPublicationError(
            "configured validation could not be executed"
        ) from error
    _verify_pushed_incident_branch(
        workspace_path,
        request=request,
        repository_config=repository,
        expected_commit=verified_branch.commit,
    )
    decision = decide_pr_publication(
        test_result.validation_status,
        allow_unvalidated_draft=remediation.get("allow_unvalidated_draft", False),
    )
    if decision.status == "BLOCKED":
        return DraftPRPublication(
            decision=decision, draft_pr=None, test_result=test_result
        )
    publisher = pipeline_guard_publisher_from_repository(
        repository,
        request_publisher=request_publisher,
    )
    return publish_draft_pr(
        request,
        incident_id=value["incident_id"],
        run_id=normalized_run_id,
        test_result=test_result,
        pushed_branch=verified_branch,
        allow_unvalidated_draft=remediation.get("allow_unvalidated_draft", False),
        publisher=publisher,
    )


def _workspace_path(value: object) -> Path:
    path = Path(_required_text(value, "workspace_path")).expanduser()
    if not path.is_dir():
        raise PRPublicationError(
            "workspace_path must identify an existing workspace directory"
        )
    return path.resolve()


def _verify_pushed_incident_branch(
    workspace_path: Path,
    *,
    request: PRRequest,
    repository_config: object,
    expected_commit: str | None = None,
) -> PushedIncidentBranch:
    """Bind validation to the exact clean incident commit already on origin."""
    if not isinstance(repository_config, Mapping):
        raise PRPublicationError("repository configuration must be an object")
    configured_url = _required_text(repository_config.get("url"), "repository.url")
    provider = _required_text(repository_config.get("provider"), "repository.provider")
    credential_ref = _required_text(
        repository_config.get("push_credential_ref"), "repository.push_credential_ref"
    )
    try:
        credential = resolve_push_credential(credential_ref)
    except (RuntimeError, ValueError) as error:
        raise PRPublicationError(
            "could not resolve the configured push credential"
        ) from error
    environment = isolated_push_environment(credential, provider=provider)

    origin_url = _workspace_git_text(
        workspace_path,
        "remote",
        "get-url",
        "origin",
        environment=environment,
        action="read workspace origin",
    ).strip()
    if _normalized_repository_url(origin_url) != _normalized_repository_url(
        configured_url
    ):
        raise PRPublicationError("workspace origin does not match repository.url")

    branch = _workspace_git_text(
        workspace_path,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        environment=environment,
        action="read workspace branch",
    ).strip()
    if branch != request.branch:
        raise PRPublicationError(
            "request.branch must match the checked-out incident branch"
        )
    if _workspace_git_text(
        workspace_path,
        "status",
        "--porcelain",
        "--untracked-files=all",
        environment=environment,
        action="inspect workspace changes",
    ).strip():
        raise PRPublicationError(
            "workspace must be clean before publication validation"
        )

    head = _workspace_git_text(
        workspace_path,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        environment=environment,
        action="read workspace commit",
    ).strip()
    remote = _remote_branch_commit(
        workspace_path,
        request.branch,
        environment=environment,
    )
    if remote != head:
        raise PRPublicationError(
            "request.branch must already be pushed at the workspace commit"
        )
    if expected_commit is not None and head != expected_commit:
        raise PRPublicationError(
            "incident branch changed during publication validation"
        )
    return PushedIncidentBranch(branch=branch, commit=head)


def _workspace_git_text(
    workspace_path: Path,
    *arguments: str,
    environment: Mapping[str, str],
    action: str,
) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace_path), "-c", "core.longpaths=true", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(environment),
            text=True,
            timeout=_GIT_VERIFICATION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PRPublicationError(f"Git could not {action}") from error
    if completed.returncode != 0:
        raise PRPublicationError(f"Git could not {action}")
    return completed.stdout


def _remote_branch_commit(
    workspace_path: Path,
    branch: str,
    *,
    environment: Mapping[str, str],
) -> str | None:
    reference = f"refs/heads/{branch}"
    output = _workspace_git_text(
        workspace_path,
        "ls-remote",
        "--heads",
        "origin",
        reference,
        environment=environment,
        action="inspect the incident branch on origin",
    ).strip()
    if not output:
        return None
    lines = output.splitlines()
    if len(lines) != 1:
        raise PRPublicationError(
            "Git returned multiple origin references for the incident branch"
        )
    parts = lines[0].split("\t")
    if len(parts) != 2 or parts[1] != reference:
        raise PRPublicationError(
            "Git returned an unexpected origin reference for the incident branch"
        )
    return _required_commit(parts[0], "origin incident branch")


def _normalized_repository_url(value: str) -> str:
    return value.rstrip("/").removesuffix(".git")


def _required_commit(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) not in {40, 64}
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise PRPublicationError(f"{field} must be a lowercase Git commit hash")
    return value


def _validated_test_result(value: object, *, run_id: str) -> TestExecutorResult:
    if not isinstance(value, TestExecutorResult):
        raise PRPublicationError("test_result must be produced by TestExecutor")
    if value.run_id != run_id:
        raise PRPublicationError("test_result.run_id must match run_id")
    if not isinstance(value.results, list) or any(
        not isinstance(result, CommandResult) for result in value.results
    ):
        raise PRPublicationError(
            "test_result.results must contain TestExecutor command results"
        )
    statuses = {result.status for result in value.results}
    if not statuses:
        expected_status = "INCOMPLETE"
    elif statuses.issubset({"PASSED", "SKIPPED"}):
        expected_status = "ALL_PASSED"
    elif statuses.issubset({"PASSED", "SKIPPED", "FAILED", "TIMEOUT"}):
        expected_status = "HAS_FAILURES"
    else:
        raise PRPublicationError("test_result contains an unsupported command status")
    if value.validation_status != expected_status:
        raise PRPublicationError(
            "test_result.validation_status disagrees with command results"
        )
    return value


def _validated_pushed_branch(
    value: object, *, request: PRRequest
) -> PushedIncidentBranch:
    if not isinstance(value, PushedIncidentBranch):
        raise PRPublicationError("pushed_branch must be verified before publication")
    if value.branch != request.branch:
        raise PRPublicationError("request.branch must match the pushed incident branch")
    _required_commit(value.commit, "pushed_branch.commit")
    return value


def _request_from_mapping(value: object) -> PRRequest:
    if not isinstance(value, Mapping) or set(value) != {
        "title",
        "body",
        "branch",
        "base",
        "draft",
    }:
        raise PRPublicationError("request has unsupported or missing fields")
    if value["draft"] is not True:
        raise PRPublicationError("request must set draft=true")
    return PRRequest(
        title=_required_text(value["title"], "request.title"),
        body=_required_text(value["body"], "request.body"),
        branch=_required_text(value["branch"], "request.branch"),
        base=_required_text(value["base"], "request.base"),
        draft=True,
    )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PRPublicationError(f"{field} must be a non-empty string")
    return value


def _normalized_draft_pr(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise PRPublicationError(
            "repair PR publisher must return a draft PR id and/or URL"
        )
    if not set(value).issubset({"id", "url"}):
        raise PRPublicationError("repair PR result contains unsupported fields")

    return {key: _required_text(item, f"draft_pr.{key}") for key, item in value.items()}


def _normalized_repair_reference(value: object) -> dict[str, str]:
    """Accept internal idempotency metadata while keeping the result neutral."""
    if not isinstance(value, Mapping) or set(value) != {"id", "url", "reused"}:
        raise PRPublicationError(
            "repair PR publisher returned an invalid draft-PR reference"
        )
    if not isinstance(value["reused"], bool):
        raise PRPublicationError("repair PR publisher returned an invalid reuse flag")
    return {
        "id": _required_text(value["id"], "draft_pr.id"),
        "url": _required_text(value["url"], "draft_pr.url"),
    }


def _repository_identity_from_url(
    repository_url: str, provider: str
) -> tuple[str, str]:
    """Extract the Bitbucket workspace or GitHub owner and repository name."""
    if provider not in {"bitbucket", "github"}:
        raise PRPublicationError(
            "Pipeline Guard draft PR publication supports only bitbucket and github"
        )
    parsed = urlsplit(repository_url)
    path = parsed.path
    if (
        not parsed.scheme
        and ":" in repository_url
        and not repository_url.startswith("/")
    ):
        path = repository_url.split(":", maxsplit=1)[1]
    parts = [part for part in path.rstrip("/").removesuffix(".git").split("/") if part]
    if len(parts) != 2:
        raise PRPublicationError(
            "repository.url must identify a repository owner and name"
        )
    return parts[0], parts[1]


def main(argv: list[str] | None = None) -> int:
    """Publish a JSON request from stdin through the validated project policy."""
    parser = argparse.ArgumentParser(description="Publish one policy-gated draft PR")
    parser.add_argument("--config", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        value = _load_publication_input(sys.stdin)
        publication = publish_from_config(
            value,
            project_config=validate_config(arguments.config),
        )
    except (OSError, ValueError) as error:
        print(f"Draft PR publication failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": publication.decision.status,
                "reason": publication.decision.reason,
                "draft_pr": publication.draft_pr,
                "test_result": _serialized_test_result(publication.test_result),
            }
        )
    )
    return 0


def _load_publication_input(stream: TextIO) -> object:
    """Load JSON stdin while tolerating a Windows UTF-8 byte-order mark."""
    return json.loads(stream.read().removeprefix("\ufeff"))


def _serialized_test_result(
    value: TestExecutorResult | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "run_id": value.run_id,
        "validation_status": value.validation_status,
        "results": [
            {
                "name": result.name,
                "status": result.status,
                "duration_seconds": result.duration_seconds,
                "log_ref": result.log_ref,
            }
            for result in value.results
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
