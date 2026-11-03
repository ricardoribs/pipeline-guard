# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Stable failure identity and remediation branch derivation for unattended runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class FailureSignatureError(ValueError):
    """Raised when a failure does not have enough trusted identity fields."""


def canonical_repository_identity(repository_url: object) -> str:
    """Normalize one credential-free repository address for hashing."""
    value = _required_text(repository_url, "repository_url")
    local_path = Path(value).expanduser()
    if local_path.is_absolute() and not value.startswith(("//", "\\\\")):
        return "local:" + local_path.resolve().as_posix().rstrip("/").removesuffix(
            ".git"
        )

    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise FailureSignatureError("repository_url could not be parsed") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise FailureSignatureError(
            "repository_url must be credential-free HTTPS or an absolute local path"
        )
    path = parsed.path.rstrip("/").removesuffix(".git")
    if len([part for part in path.split("/") if part]) < 2:
        raise FailureSignatureError(
            "repository_url must identify an owner and repository"
        )
    host = parsed.hostname.casefold()
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit(("https", host, path, "", ""))


def calculate_failure_signature(
    *,
    repository_url: object,
    pipeline_id: object,
    failed_step: object,
    commit: object,
    target_branch: object,
) -> str:
    """Hash one repair identity per repository, pipeline, commit and target.

    ``failed_step`` remains required as incident evidence, but is deliberately
    excluded from the identity. Parallel failed steps from the same pipeline
    execution must converge on one remediation branch and one draft PR.
    """
    normalized_commit = _required_text(commit, "code_version.commit")
    _required_text(failed_step, "failed_step")
    if len(normalized_commit) != 40 or any(
        character not in "0123456789abcdef" for character in normalized_commit
    ):
        raise FailureSignatureError(
            "code_version.commit must be a lowercase 40-digit hash"
        )
    identity = {
        "repository": canonical_repository_identity(repository_url),
        "pipeline_id": _required_text(pipeline_id, "pipeline_id"),
        "commit": normalized_commit,
        "target_branch": _required_text(target_branch, "target_branch"),
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def derive_remediation_branch(branch_prefix: object, failure_signature: object) -> str:
    """Return the deterministic, retry-safe branch associated with a signature."""
    prefix = _required_text(
        branch_prefix, "repository.remediation_branch_prefix"
    ).rstrip("/")
    signature = _validated_signature(failure_signature)
    return f"{prefix}/unattended-{signature.removeprefix('sha256:')[:24]}"


def attach_failure_signature(
    incident: Mapping[str, Any], failure_signature: object
) -> dict[str, Any]:
    """Copy an incident and attach its derived signature without mutating the input."""
    signature = _validated_signature(failure_signature)
    document = deepcopy(dict(incident))
    correlation = document.get("correlation")
    if correlation is None:
        correlation = {}
    if not isinstance(correlation, Mapping):
        raise FailureSignatureError("incident.correlation must be an object")
    normalized_correlation = dict(correlation)
    existing = normalized_correlation.get("signature")
    if existing is not None and existing != signature:
        raise FailureSignatureError(
            "incident.correlation.signature conflicts with derived value"
        )
    normalized_correlation["signature"] = signature
    document["correlation"] = normalized_correlation
    return document


def _validated_signature(value: object) -> str:
    signature = _required_text(value, "failure_signature")
    digest = signature.removeprefix("sha256:")
    if (
        not signature.startswith("sha256:")
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise FailureSignatureError(
            "failure_signature must use sha256 with 64 lowercase digits"
        )
    return signature


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FailureSignatureError(f"{field} must be a non-empty string")
    return value.strip()
