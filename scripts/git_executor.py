# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Commit and push a previously validated remediation patch from an isolated workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from base64 import b64encode
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

try:
    from .workspace_executor import (
        ClonedWorkspace,
        _git_environment,
        _run_git_command,
    )
except ImportError:
    from workspace_executor import ClonedWorkspace, _git_environment, _run_git_command

_GIT_TIMEOUT_SECONDS = 30
_PATCH_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_PUSH_CREDENTIAL_REF_PATTERN = re.compile(
    r"^(?:env:[A-Z][A-Z0-9_]*|\$\{[A-Z][A-Z0-9_]*\}|secret:[A-Za-z0-9][A-Za-z0-9._/-]*)$"
)
_ALLOWED_GIT_OPERATIONS = frozenset({"push_incident_branch"})
_LEAKING_GIT_ENV_VARS = frozenset(
    {
        "AZURE_DEVOPS_API_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_API_TOKEN",
        "GH_TOKEN",
        "GITLAB_API_TOKEN",
        "GITLAB_TOKEN",
        "BITBUCKET_API_TOKEN",
        "BITBUCKET_EMAIL",
        "BITBUCKET_TOKEN",
        "AZURE_DEVOPS_EXT_PAT",
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
        "GIT_SSH_COMMAND",
    }
)
_GIT_CONFIG_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_REMEDIATION_RESULT_SCHEMA_PATH = (
    _PLUGIN_ROOT / "contracts" / "schema" / "remediation_result_v1.schema.json"
)
_PR_VALIDATION_STATUSES = frozenset({"VALIDATED", "PUBLISHED_UNVALIDATED"})
_REMEDIATION_RESULT_STATUSES = frozenset(
    {
        "DRAFT_PR_PUBLISHED",
        "BLOCKED_VALIDATION_FAILED",
        "BLOCKED_APPROVAL_INVALID",
        "BLOCKED_LIMIT_EXCEEDED",
        "ERROR",
    }
)
_TEST_VALIDATION_STATUSES = frozenset({"ALL_PASSED", "HAS_FAILURES", "INCOMPLETE"})
_TEST_RESULT_STATUSES = frozenset({"PASSED", "FAILED", "TIMEOUT", "SKIPPED"})
_UNMERGED_STATUS_CODES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


class GitExecutorError(RuntimeError):
    """Raised when a local commit or push cannot be completed safely."""


class GitExecutorValidationError(ValueError):
    """Raised when the workspace or patch provenance is not safe to commit."""


class RemediationResultValidationError(ValueError):
    """Raised when a final remediation result cannot satisfy its public contract."""


class _TestResultView(Protocol):
    """Minimum test-execution fields consumed by the final result builder."""

    results: object
    validation_status: object


class _CommandResultView(Protocol):
    """Minimum per-command fields consumed by the final result builder."""

    name: object
    status: object
    duration_seconds: object
    log_ref: object


@dataclass(frozen=True)
class GitExecutionResult:
    """Local Git state produced by committing and pushing one remediation patch."""

    branch: str
    commit: str
    base_commit: str
    patch_hash: str


def build_remediation_result(
    *,
    incident_id: object,
    workspace: ClonedWorkspace,
    git_result: GitExecutionResult,
    test_result: object,
    draft_pr: object | None,
    pr_validation_status: object | None,
    status: object | None = None,
    reason: object | None = None,
    limitations: object = (),
    warnings: object = (),
    log_references: object = (),
) -> dict[str, Any]:
    """Build and validate the terminal ``remediation_result_v1`` contract.

    The Git executor derives the committed files from its isolated workspace.
    The caller supplies the draft-PR reference returned by the draft PR
    publisher and the publication decision returned by the
    deterministic policy gate. If ``status`` is omitted, a ``BLOCKED`` policy
    decision with incomplete or failed validation becomes
    ``BLOCKED_VALIDATION_FAILED``. This module never creates, queries, approves,
    or merges pull requests.
    """
    if not isinstance(git_result, GitExecutionResult):
        raise RemediationResultValidationError(
            "git_result must be a GitExecutionResult"
        )

    normalized_incident_id = _required_nonempty_text(incident_id, "incident_id")
    files_changed = _committed_files_changed(workspace, git_result)
    normalized_tests, validation_status = _normalized_test_results(test_result)
    normalized_status = _normalized_remediation_status(
        status,
        pr_validation_status=pr_validation_status,
        test_validation_status=validation_status,
    )

    result: dict[str, Any] = {
        "schema_version": "1.0",
        "incident_id": normalized_incident_id,
        "status": normalized_status,
        "reason": _normalized_remediation_reason(reason, normalized_status),
        "base_commit": git_result.base_commit,
        "branch": git_result.branch,
        "commit": git_result.commit,
        "files_changed": files_changed,
        "patch_hash": git_result.patch_hash,
        "tests": normalized_tests,
        "limitations": _normalized_text_list(limitations, "limitations"),
        "warnings": _normalized_text_list(warnings, "warnings"),
        "log_references": _normalized_text_list(log_references, "log_references"),
    }
    if normalized_status == "DRAFT_PR_PUBLISHED":
        result["draft_pr"] = _normalized_draft_pr(draft_pr)
        result["pr_validation_status"] = _normalized_pr_validation_status(
            pr_validation_status,
            validation_status,
        )
    else:
        _require_no_published_pr(
            draft_pr,
            pr_validation_status=pr_validation_status,
            status=normalized_status,
        )
    _validate_remediation_result(result)
    return result


def commit_and_push(
    workspace: ClonedWorkspace,
    patch_validation: Mapping[str, Any],
    *,
    push_credential: str | None = None,
    provider: str | None,
) -> GitExecutionResult:
    """Commit and push one accepted patch on the workspace's incident branch.

    The workspace must have been prepared from an approval-bound base commit and
    the patch result must come from :func:`patch_validator.validate_patch`. The
    executor recalculates the current Git diff before staging and after staging,
    so it never commits bytes that differ from the validated patch. It pushes
    exactly one incident branch to ``origin`` and has no pull-request behavior.
    """
    base_commit, branch, current_commit = _require_prepared_incident_workspace(
        workspace
    )
    expected_hash = _require_accepted_patch_validation(patch_validation, base_commit)
    normalized_provider = _required_provider(provider)

    if current_commit == base_commit:
        _require_only_validated_tracked_changes(workspace)
        working_patch = _current_patch(workspace, cached=False)
        _require_patch_hash(working_patch, expected_hash, stage="working tree")

        _stage_validated_patch(workspace)
        staged_patch = _current_patch(workspace, cached=True)
        _require_patch_hash(staged_patch, expected_hash, stage="staging area")

        _commit_staged_patch(workspace, branch)
        commit = _current_commit(workspace)
    else:
        _require_retriable_remediation_commit(
            workspace,
            base_commit=base_commit,
            commit=current_commit,
            expected_hash=expected_hash,
        )
        commit = current_commit

    _push_incident_branch_if_needed(
        workspace,
        branch,
        commit,
        push_credential=push_credential,
        provider=normalized_provider,
    )

    return GitExecutionResult(
        branch=branch,
        commit=commit,
        base_commit=base_commit,
        patch_hash=expected_hash,
    )


def _required_provider(value: object) -> str:
    """Require the repository provider before selecting Git authentication semantics."""
    if not isinstance(value, str) or not value.strip():
        raise GitExecutorValidationError(
            "repository.provider must be a non-empty string"
        )
    return value.strip().casefold()


def _require_prepared_incident_workspace(
    workspace: ClonedWorkspace,
) -> tuple[str, str, str]:
    base_commit = workspace.verified_base_commit
    branch = workspace.incident_branch
    if base_commit is None or branch is None:
        raise GitExecutorValidationError(
            "Git execution requires an open workspace prepared with an approved base and "
            "incident branch"
        )
    normalized_base = _normalize_commit(base_commit, "workspace.verified_base_commit")

    current_branch = _git_text(
        workspace,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        action="verify the incident branch",
    ).strip()
    current_commit = _current_commit(workspace)
    merge_base = _git_text(
        workspace,
        "merge-base",
        normalized_base,
        current_commit,
        action="verify the remediation commit ancestry",
    ).strip()
    if current_branch != branch or merge_base != normalized_base:
        raise GitExecutorValidationError(
            "workspace must be based on the approved commit on the incident branch"
        )
    return normalized_base, branch, current_commit


def _require_accepted_patch_validation(
    patch_validation: Mapping[str, Any],
    base_commit: str,
) -> str:
    if not isinstance(patch_validation, Mapping):
        raise GitExecutorValidationError("patch validation result must be an object")
    if patch_validation.get("accepted") is not True:
        raise GitExecutorValidationError(
            "Git execution requires an accepted patch validation result"
        )

    validated_base = patch_validation.get("base_commit")
    if _normalize_commit(validated_base, "patch_validation.base_commit") != base_commit:
        raise GitExecutorValidationError(
            "patch validation base_commit does not match the workspace's approved base"
        )

    patch_hash = patch_validation.get("patch_hash")
    if (
        not isinstance(patch_hash, str)
        or _PATCH_HASH_PATTERN.fullmatch(patch_hash) is None
    ):
        raise GitExecutorValidationError(
            "patch validation patch_hash must be a canonical SHA-256 reference"
        )
    return patch_hash


def _normalize_commit(value: object, field: str) -> str:
    if not isinstance(value, str) or _COMMIT_PATTERN.fullmatch(value) is None:
        raise GitExecutorValidationError(
            f"{field} must be a complete Git commit identifier"
        )
    return value.lower()


def _require_only_validated_tracked_changes(workspace: ClonedWorkspace) -> None:
    status = _git_text(
        workspace,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        action="inspect the workspace before staging",
    )
    if not status:
        raise GitExecutorValidationError("workspace has no changes to commit")

    status_entries = status.splitlines()
    if any(
        len(entry) < 3
        or entry[:2] in _UNMERGED_STATUS_CODES
        or entry[:2] in {"??", "!!"}
        for entry in status_entries
    ):
        raise GitExecutorValidationError(
            "workspace must contain only tracked changes from the validated patch"
        )


def _current_patch(workspace: ClonedWorkspace, *, cached: bool) -> bytes:
    arguments = ["diff", "--binary", "--no-ext-diff", "--no-renames"]
    if cached:
        arguments.append("--cached")
    arguments.extend(("HEAD", "--"))
    completed = _git_bytes(workspace, *arguments, action="read the remediation patch")
    if not completed.stdout:
        stage = "staging area" if cached else "working tree"
        raise GitExecutorValidationError(f"validated patch is absent from the {stage}")
    return completed.stdout


def _require_retriable_remediation_commit(
    workspace: ClonedWorkspace,
    *,
    base_commit: str,
    commit: str,
    expected_hash: str,
) -> None:
    status = _git_text(
        workspace,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        action="inspect the committed remediation retry",
    )
    if status:
        raise GitExecutorValidationError(
            "workspace must be clean when retrying a committed remediation"
        )

    commit_count = _git_text(
        workspace,
        "rev-list",
        "--count",
        f"{base_commit}..{commit}",
        action="verify the remediation commit count",
    ).strip()
    if commit_count != "1":
        raise GitExecutorValidationError(
            "workspace retry must contain exactly one remediation commit after the approved base"
        )

    committed_patch = _git_bytes(
        workspace,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-renames",
        base_commit,
        commit,
        "--",
        action="read the committed remediation patch",
    ).stdout
    _require_patch_hash(committed_patch, expected_hash, stage="committed remediation")


def _require_patch_hash(patch: bytes, expected_hash: str, *, stage: str) -> None:
    actual_hash = f"sha256:{hashlib.sha256(patch).hexdigest()}"
    if actual_hash != expected_hash:
        raise GitExecutorValidationError(
            f"validated patch hash does not match the {stage} ({actual_hash})"
        )


def _stage_validated_patch(workspace: ClonedWorkspace) -> None:
    _git_text(
        workspace,
        "add",
        "--all",
        "--",
        action="stage the validated patch",
    )


def _commit_staged_patch(workspace: ClonedWorkspace, branch: str) -> None:
    hooks_path = _empty_hooks_directory(workspace)
    try:
        _git_text(
            workspace,
            "-c",
            f"core.hooksPath={hooks_path}",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            f"fix(pipeline-guard): remediate {branch}",
            action="commit the validated patch",
        )
    finally:
        _remove_empty_hooks_directory(hooks_path)


def _empty_hooks_directory(workspace: ClonedWorkspace) -> Path:
    try:
        hooks_path = Path(
            tempfile.mkdtemp(prefix="git-executor-hooks-", dir=workspace.path.parent)
        )
    except OSError as error:
        raise GitExecutorError(
            "could not prepare the executor hook isolation directory"
        ) from error
    if (
        hooks_path.is_symlink()
        or not hooks_path.is_dir()
        or hooks_path.parent != workspace.path.parent
    ):
        raise GitExecutorError(
            "executor hook isolation directory is not a safe directory"
        )
    return hooks_path


def _remove_empty_hooks_directory(hooks_path: Path) -> None:
    """Remove the per-commit hook directory without recursively following content."""
    try:
        hooks_path.rmdir()
    except OSError as error:
        raise GitExecutorError(
            "could not remove the executor hook isolation directory"
        ) from error


def _current_commit(workspace: ClonedWorkspace) -> str:
    value = _git_text(
        workspace,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        action="read the current commit",
    ).strip()
    return _normalize_commit(value, "workspace HEAD")


def _push_incident_branch_if_needed(
    workspace: ClonedWorkspace,
    branch: str,
    commit: str,
    *,
    push_credential: str | None = None,
    provider: str | None = None,
) -> None:
    environment = isolated_push_environment(push_credential, provider=provider)
    remote_commit = _remote_branch_commit(workspace, branch, environment=environment)
    if remote_commit == commit:
        return
    if remote_commit is not None:
        raise GitExecutorValidationError(
            "origin already has the incident branch at a different commit"
        )
    _push_incident_branch(
        workspace,
        branch,
        push_credential,
        provider=provider,
        environment=environment,
    )


def _remote_branch_commit(
    workspace: ClonedWorkspace,
    branch: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> str | None:
    reference = f"refs/heads/{branch}"
    output = _git_text(
        workspace,
        "ls-remote",
        "--heads",
        "origin",
        reference,
        action="inspect the incident branch on origin",
        environment=environment,
    ).strip()
    if not output:
        return None

    lines = output.splitlines()
    if len(lines) != 1:
        raise GitExecutorError(
            "Git returned multiple origin references for the incident branch"
        )
    parts = lines[0].split("\t")
    if len(parts) != 2 or parts[1] != reference:
        raise GitExecutorError(
            "Git returned an unexpected origin reference for the incident branch"
        )
    return _normalize_commit(parts[0], "origin incident branch")


def isolated_push_environment(
    push_credential: str | None,
    *,
    provider: str | None = None,
) -> dict[str, str]:
    """Create an isolated environment for credentialed Git remote operations.

    Ambient credentials and global/system Git configuration are removed so the
    operation relies only on the explicit host-resolved credential. Git receives
    that credential through its configuration environment rather than its
    process arguments. Bitbucket Cloud Git accepts API tokens through Basic
    authentication with its static API-token username; other providers retain
    the existing Bearer header convention.
    """
    environment = _git_environment()
    for var in _LEAKING_GIT_ENV_VARS:
        environment.pop(var, None)
    for var in tuple(environment):
        if var == "GIT_CONFIG_COUNT" or var.startswith(_GIT_CONFIG_ENV_PREFIXES):
            environment.pop(var)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    if push_credential:
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": _push_authorization_header(
                    push_credential,
                    provider=provider,
                ),
            }
        )
    return environment


def _push_authorization_header(push_credential: str, *, provider: str | None) -> str:
    """Return the provider-compatible authorization header for isolated Git HTTP."""
    normalized_provider = (
        provider.strip().casefold() if isinstance(provider, str) else ""
    )
    if normalized_provider == "bitbucket":
        token_pair = f"x-token-auth:{push_credential}".encode()
        encoded_token_pair = b64encode(token_pair).decode("ascii")
        return f"Authorization: Basic {encoded_token_pair}"
    return f"Authorization: Bearer {push_credential}"


def _push_incident_branch(
    workspace: ClonedWorkspace,
    branch: str,
    push_credential: str | None = None,
    *,
    provider: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> None:
    repository_url = workspace.inputs.repository_url
    try:
        parsed = urlsplit(repository_url)
    except ValueError:
        raise GitExecutorValidationError(
            "repository.url is invalid for a Git push"
        ) from None
    local_path = Path(repository_url).is_absolute() and not repository_url.startswith(
        ("//", "\\\\")
    )
    if push_credential and parsed.scheme != "https" and not local_path:
        raise GitExecutorValidationError(
            "repository.url must use HTTPS for a header-based push credential"
        )
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    hooks_path = _empty_hooks_directory(workspace)
    try:
        _git_text(
            workspace,
            "-c",
            f"core.hooksPath={hooks_path}",
            "-c",
            f"remote.origin.pushurl={repository_url}",
            "push",
            "--no-verify",
            "--porcelain",
            "origin",
            refspec,
            action="push the incident branch",
            environment=(
                environment
                if environment is not None
                else isolated_push_environment(push_credential, provider=provider)
            ),
        )
    finally:
        _remove_empty_hooks_directory(hooks_path)


def _committed_files_changed(
    workspace: ClonedWorkspace,
    git_result: GitExecutionResult,
) -> list[dict[str, Any]]:
    """Derive changed-file metadata from the committed remediation patch."""
    try:
        base_commit, branch, commit = _require_prepared_incident_workspace(workspace)
    except (GitExecutorError, GitExecutorValidationError) as error:
        raise RemediationResultValidationError(
            "workspace cannot verify the committed remediation"
        ) from error

    if (
        git_result.base_commit != base_commit
        or git_result.branch != branch
        or git_result.commit != commit
    ):
        raise RemediationResultValidationError(
            "git_result must match the prepared workspace's branch, base commit, and HEAD"
        )

    patch = _git_bytes(
        workspace,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-renames",
        base_commit,
        commit,
        "--",
        action="read the committed remediation patch",
    ).stdout
    _require_patch_hash(patch, git_result.patch_hash, stage="committed remediation")

    numstat = _git_bytes(
        workspace,
        "diff",
        "--numstat",
        "-z",
        "--no-ext-diff",
        "--no-renames",
        base_commit,
        commit,
        "--",
        action="read committed remediation file statistics",
    ).stdout
    return _parse_numstat(numstat)


def _parse_numstat(value: bytes) -> list[dict[str, Any]]:
    """Normalize Git's NUL-delimited numstat output without trusting caller input."""
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(filter(None, value.split(b"\0"))):
        parts = entry.split(b"\t", maxsplit=2)
        if len(parts) != 3:
            raise RemediationResultValidationError(
                f"committed file statistic {index} has an unexpected format"
            )
        lines_added, lines_removed, raw_path = parts
        if not lines_added.isdigit() or not lines_removed.isdigit():
            raise RemediationResultValidationError(
                "committed remediation contains a binary file with no line statistics"
            )
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RemediationResultValidationError(
                f"committed file statistic {index} contains a non-UTF-8 path"
            ) from error
        normalized.append(
            {
                "path": _required_nonempty_text(
                    path, f"committed file statistic {index}.path"
                ),
                "lines_added": int(lines_added),
                "lines_removed": int(lines_removed),
            }
        )
    return normalized


def _normalized_test_results(test_result: object) -> tuple[list[dict[str, Any]], str]:
    if not hasattr(test_result, "results") or not hasattr(
        test_result, "validation_status"
    ):
        raise RemediationResultValidationError(
            "test_result must provide results and validation_status"
        )
    result_view = cast(_TestResultView, test_result)
    validation_status = result_view.validation_status
    if (
        not isinstance(validation_status, str)
        or validation_status not in _TEST_VALIDATION_STATUSES
    ):
        raise RemediationResultValidationError(
            "test_result.validation_status is invalid"
        )
    results = result_view.results
    if not isinstance(results, list):
        raise RemediationResultValidationError("test_result.results must be a list")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(results):
        for attribute in ("name", "status", "duration_seconds", "log_ref"):
            if not hasattr(item, attribute):
                raise RemediationResultValidationError(
                    f"test_result.results[{index}] is missing {attribute!r}"
                )
        command_view = cast(_CommandResultView, item)
        command = _required_nonempty_text(
            command_view.name, f"test_result.results[{index}].name"
        )
        status = command_view.status
        duration_seconds = command_view.duration_seconds
        log_reference = command_view.log_ref
        if status not in _TEST_RESULT_STATUSES:
            raise RemediationResultValidationError(
                f"test_result.results[{index}].status is invalid"
            )
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int | float)
            or duration_seconds < 0
        ):
            raise RemediationResultValidationError(
                f"test_result.results[{index}].duration_seconds must be non-negative"
            )
        if log_reference is not None and not isinstance(log_reference, str):
            raise RemediationResultValidationError(
                f"test_result.results[{index}].log_ref must be a string or None"
            )

        normalized_result: dict[str, Any] = {
            "command": command,
            "status": status,
            "duration_seconds": duration_seconds,
        }
        if log_reference:
            normalized_result["log_reference"] = log_reference
        normalized.append(normalized_result)

    derived_validation_status = _derived_test_validation_status(normalized)
    if validation_status != derived_validation_status:
        raise RemediationResultValidationError(
            "test_result.validation_status does not match its command results "
            f"(expected {derived_validation_status})"
        )
    return normalized, derived_validation_status


def _derived_test_validation_status(results: Sequence[Mapping[str, Any]]) -> str:
    if not results:
        return "INCOMPLETE"
    if any(result["status"] in {"FAILED", "TIMEOUT"} for result in results):
        return "HAS_FAILURES"
    return "ALL_PASSED"


def _normalized_pr_validation_status(value: object, test_validation_status: str) -> str:
    if not isinstance(value, str) or value not in _PR_VALIDATION_STATUSES:
        raise RemediationResultValidationError("pr_validation_status is invalid")
    if value == "VALIDATED" and test_validation_status != "ALL_PASSED":
        raise RemediationResultValidationError(
            "VALIDATED requires Test Executor status ALL_PASSED"
        )
    if value == "PUBLISHED_UNVALIDATED" and test_validation_status == "ALL_PASSED":
        raise RemediationResultValidationError(
            "PUBLISHED_UNVALIDATED requires incomplete or failed test validation"
        )
    return value


def _normalized_remediation_status(
    value: object,
    *,
    pr_validation_status: object | None,
    test_validation_status: str,
) -> str:
    if value is None:
        if pr_validation_status in _PR_VALIDATION_STATUSES:
            return "DRAFT_PR_PUBLISHED"
        if pr_validation_status == "BLOCKED":
            if test_validation_status == "ALL_PASSED":
                raise RemediationResultValidationError(
                    "BLOCKED publication requires incomplete or failed test validation"
                )
            return "BLOCKED_VALIDATION_FAILED"
        raise RemediationResultValidationError(
            "status must be supplied unless pr_validation_status is a policy decision"
        )
    if not isinstance(value, str) or value not in _REMEDIATION_RESULT_STATUSES:
        raise RemediationResultValidationError("status is invalid")
    if value == "BLOCKED_VALIDATION_FAILED" and test_validation_status == "ALL_PASSED":
        raise RemediationResultValidationError(
            "BLOCKED_VALIDATION_FAILED requires incomplete or failed test validation"
        )
    return value


def _normalized_remediation_reason(value: object | None, status: str) -> str:
    if value is not None:
        return _required_nonempty_text(value, "reason")
    if status == "DRAFT_PR_PUBLISHED":
        return "A draft pull request was published for the approved remediation."
    if status == "BLOCKED_VALIDATION_FAILED":
        return "Draft pull request publication was blocked because validation did not pass."
    if status == "BLOCKED_APPROVAL_INVALID":
        return "The remediation was blocked because its approval was invalid."
    if status == "BLOCKED_LIMIT_EXCEEDED":
        return "The remediation was blocked because a configured safety limit was exceeded."
    return "The remediation ended without publishing a draft pull request."


def _require_no_published_pr(
    draft_pr: object | None,
    *,
    pr_validation_status: object | None,
    status: str,
) -> None:
    if draft_pr is not None:
        raise RemediationResultValidationError(f"{status} must not include a draft_pr")
    if pr_validation_status not in (None, "BLOCKED"):
        raise RemediationResultValidationError(
            f"{status} must not include a published pr_validation_status"
        )


def _normalized_draft_pr(value: object | None) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise RemediationResultValidationError("draft_pr must be an object")
    if not set(value).issubset({"url", "id"}) or not value:
        raise RemediationResultValidationError(
            "draft_pr must contain only url and/or id"
        )

    normalized: dict[str, str] = {}
    for field in ("url", "id"):
        if field in value:
            normalized[field] = _required_nonempty_text(
                value[field], f"draft_pr.{field}"
            )
    return normalized


def _normalized_text_list(value: object, field: str) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise RemediationResultValidationError(f"{field} must be a list of strings")
    normalized: list[str] = []
    for index, item in enumerate(value):
        normalized.append(_required_nonempty_text(item, f"{field}[{index}]"))
    return normalized


def _required_nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RemediationResultValidationError(f"{field} must be a non-empty string")
    return value


def _validate_remediation_result(result: Mapping[str, Any]) -> None:
    validator = Draft202012Validator(_load_remediation_result_schema())
    errors = sorted(
        validator.iter_errors(result), key=lambda error: list(error.absolute_path)
    )
    if errors:
        first = errors[0]
        field = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise RemediationResultValidationError(
            f"remediation_result_v1 is invalid at {field}: {first.message}"
        )


@cache
def _load_remediation_result_schema() -> dict[str, Any]:
    try:
        schema = json.loads(_REMEDIATION_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RemediationResultValidationError(
            f"cannot load remediation_result_v1 schema: {_REMEDIATION_RESULT_SCHEMA_PATH}"
        ) from error
    Draft202012Validator.check_schema(schema)
    return schema


def _git_text(
    workspace: ClonedWorkspace,
    *arguments: str,
    action: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    try:
        completed = _run_git_command(
            ["git", "-C", str(workspace.path), "-c", "core.longpaths=true", *arguments],
            environment=dict(environment)
            if environment is not None
            else _git_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
            capture_stdout=True,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitExecutorError(f"Git could not {action}") from error
    if completed.returncode != 0:
        raise GitExecutorError(f"Git could not {action}")
    return completed.stdout


def _git_bytes(
    workspace: ClonedWorkspace,
    *arguments: str,
    action: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "git",
                "-C",
                str(workspace.path),
                "-c",
                "core.longpaths=true",
                *arguments,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitExecutorError(f"Git could not {action}") from error
    if completed.returncode != 0:
        raise GitExecutorError(f"Git could not {action}")
    return completed


def validate_remediation_git_configuration(
    config: Mapping[str, Any] | Path,
) -> dict[str, Any]:
    """Validate repository config for remediation Git permissions.

    Remediation requires an explicit credential reference and limits operations
    to push_incident_branch. Literal tokens and credentials in URLs are forbidden.
    """
    if isinstance(config, Path):
        from scripts.validate_config import validate_config

        config_dict = validate_config(config)
    elif isinstance(config, Mapping):
        config_dict = cast(dict[str, Any], config)
    else:
        raise GitExecutorValidationError("project_config must be an object or Path")

    repository = config_dict.get("repository")
    if not isinstance(repository, Mapping):
        raise GitExecutorValidationError("repository configuration must be an object")

    push_cred_ref = repository.get("push_credential_ref")
    if not push_cred_ref:
        raise GitExecutorValidationError(
            "remediation requires repository.push_credential_ref to be configured"
        )
    if (
        not isinstance(push_cred_ref, str)
        or _PUSH_CREDENTIAL_REF_PATTERN.fullmatch(push_cred_ref) is None
    ):
        raise GitExecutorValidationError(
            "repository.push_credential_ref must be an env:... or secret:... reference; "
            "literal tokens are forbidden"
        )

    url = repository.get("url")
    if isinstance(url, str):
        try:
            parsed = urlsplit(url)
        except ValueError:
            parsed = None
        if parsed is not None and (
            parsed.username is not None or parsed.password is not None
        ):
            raise GitExecutorValidationError(
                "repository.url must not contain embedded credentials"
            )
        local_path = Path(url).is_absolute() and not url.startswith(("//", "\\\\"))
        if parsed is None or (parsed.scheme != "https" and not local_path):
            raise GitExecutorValidationError(
                "repository.url must use HTTPS for a header-based push credential"
            )

    allowed_ops = repository.get("allowed_git_operations")
    if allowed_ops is None:
        raise GitExecutorValidationError(
            "remediation requires repository.allowed_git_operations to be declared"
        )
    if (
        not isinstance(allowed_ops, list)
        or not allowed_ops
        or set(allowed_ops) != _ALLOWED_GIT_OPERATIONS
    ):
        raise GitExecutorValidationError(
            "repository.allowed_git_operations must contain only 'push_incident_branch'"
        )

    return {
        "push_credential_ref": push_cred_ref,
        "allowed_git_operations": list(allowed_ops),
    }


def resolve_push_credential(
    credential_ref: str,
    host_environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve a credential reference from the host environment for the isolated push process.

    Literal values are forbidden. Returns the resolved token string.
    """
    if (
        not isinstance(credential_ref, str)
        or _PUSH_CREDENTIAL_REF_PATTERN.fullmatch(credential_ref) is None
    ):
        raise GitExecutorValidationError(
            "credential_ref must be an env:... or secret:... reference; "
            "literal tokens are forbidden"
        )

    env = host_environment if host_environment is not None else os.environ

    if credential_ref.startswith("env:"):
        var_name = credential_ref[4:]
    elif credential_ref.startswith("${") and credential_ref.endswith("}"):
        var_name = credential_ref[2:-1]
    elif credential_ref.startswith("secret:"):
        secret_name = credential_ref[7:].replace("/", "_").replace("-", "_").upper()
        var_name = f"SECRET_{secret_name}"
        if var_name not in env and credential_ref in env:
            var_name = credential_ref
    else:
        var_name = credential_ref

    resolved = env.get(var_name)
    if not resolved:
        raise GitExecutorError(
            f"resolved push credential for '{credential_ref}' was not found in the environment"
        )
    return resolved
