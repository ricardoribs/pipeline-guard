#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pyyaml>=6.0.3",
# ]
# ///
"""Enforce remediation file-access boundaries before Claude Code tools run."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path
from typing import Any

import yaml

_WRITE_TOOLS = frozenset({"Write", "Edit"})
_READ_TOOLS = frozenset({"Read"})
_FILE_TOOLS = _WRITE_TOOLS | _READ_TOOLS
_REMEDIATION_AGENT = "remediation-agent"
_CONFIG_RELATIVE_PATH = Path("config") / "pipeline-guard.yaml"
_APPROVED_READ_PATHS_ENV = "PIPELINE_GUARD_APPROVED_READ_PATHS"
_READ_SENSITIVE_PATTERNS = (
    "**/.env*",
    "**/*credential*",
    "**/*credential*/**",
    "**/*secret*",
    "**/*secret*/**",
    "**/*token*",
    "**/*token*/**",
    "**/.aws/credentials",
    "**/.databrickscfg",
    "**/.docker/config.json",
    "**/.git/config",
    "**/.kube/config",
    "**/.npmrc",
    "**/.netrc",
    "**/.pypirc",
    "**/.ssh/**",
    "**/*.jks",
    "**/*.keystore",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.password",
    "**/*.tfvars",
    "**/*.tfvars.json",
    "**/*.tfstate",
    "**/*.tfstate.*",
    "**/profiles.yml",
    "**/.pgpass",
    "**/.m2/settings.xml",
    "**/.htpasswd",
    "**/*.env",
    "**/.s3cfg",
    "**/.boto",
    "**/.yarnrc.yml",
    "**/.config/gh/hosts.yml",
    "**/airflow.cfg",
    "**/.gnupg/secring.gpg",
    "**/*.kdbx",
    "**/id_rsa",
    "**/id_ed25519",
    "**/*service-account*.json",
    "**/auth.json",
    "**/kubeconfig*",
    "**/kubeconfig*/**",
)


class _PolicyError(ValueError):
    """Raised when a file-tool call cannot be verified against project policy."""


def _is_remediation_agent(agent_type: object) -> bool:
    return (
        isinstance(agent_type, str)
        and agent_type.rsplit(":", maxsplit=1)[-1] == _REMEDIATION_AGENT
    )


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _PolicyError(f"{field} must be a list of strings")
    return value


def _normalize_pattern(pattern: str) -> str:
    normalized = pattern.replace("\\", "/")
    if not normalized or normalized != normalized.strip():
        raise _PolicyError(
            "safety path patterns must be non-empty and have no surrounding spaces"
        )
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise _PolicyError("safety path patterns must be workspace-relative")

    parts = normalized.split("/")
    if ".." in parts:
        raise _PolicyError(
            "safety path patterns must not contain parent-directory segments"
        )
    if any(part.endswith((".", " ")) for part in parts if part not in {"", "."}):
        raise _PolicyError(
            "safety path patterns must not end a component with a dot or space"
        )

    return "/".join(part for part in parts if part not in {"", "."})


def _load_safety(config_path: Path) -> tuple[list[str], list[str], list[str]]:
    if config_path.is_symlink() or not config_path.is_file():
        raise _PolicyError("project safety configuration is missing or unsafe")

    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise _PolicyError("project safety configuration could not be read") from error

    if not isinstance(config, Mapping):
        raise _PolicyError("project safety configuration must be a YAML object")

    safety = config.get("safety", {})
    if not isinstance(safety, Mapping):
        raise _PolicyError("project safety configuration must contain an object")

    editable_paths = [
        _normalize_pattern(pattern)
        for pattern in _string_list(safety.get("editable_paths", []), "editable_paths")
    ]
    editable_extensions = _string_list(
        safety.get("editable_extensions", []), "editable_extensions"
    )
    sensitive_paths = [
        _normalize_pattern(pattern)
        for pattern in _string_list(
            safety.get("sensitive_paths", []), "sensitive_paths"
        )
    ]

    return editable_paths, editable_extensions, sensitive_paths


def _matches_glob(path: str, pattern: str) -> bool:
    path_parts = tuple(
        part.casefold() for part in path.split("/") if part not in {"", "."}
    )
    pattern_parts = tuple(
        part.casefold() for part in pattern.split("/") if part not in {"", "."}
    )

    @cache
    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)

        pattern_part = pattern_parts[pattern_index]
        if pattern_part == "**":
            return matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and matches(path_index + 1, pattern_index)
            )

        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], pattern_part)
            and matches(path_index + 1, pattern_index + 1)
        )

    return matches(0, 0)


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(_matches_glob(path, pattern) for pattern in patterns)


def _matches_sensitive_path(path: str, pattern: str) -> bool:
    """Match a sensitive path selected by a pattern and all its descendants."""
    return _matches_glob(path, pattern) or _matches_glob(path, f"{pattern}/**")


def _matches_any_sensitive_path(path: str, patterns: Sequence[str]) -> bool:
    return any(_matches_sensitive_path(path, pattern) for pattern in patterns)


def _relative_target(tool_path: object, workspace_root: Path) -> tuple[str, Path]:
    if not isinstance(tool_path, str) or not tool_path:
        raise _PolicyError("file tool target is missing")

    supplied_path = Path(tool_path)
    if ".." in supplied_path.parts:
        raise _PolicyError("parent-directory paths are not allowed")

    unresolved_path = (
        supplied_path if supplied_path.is_absolute() else workspace_root / supplied_path
    )
    try:
        lexical_path = Path(os.path.abspath(unresolved_path))
        lexical_relative = lexical_path.relative_to(workspace_root)
    except (OSError, ValueError) as error:
        raise _PolicyError("target is outside the isolated workspace") from error

    current_path = workspace_root
    for part in lexical_relative.parts:
        current_path /= part
        if current_path.is_symlink():
            raise _PolicyError("symbolic-link targets are not allowed")

    try:
        resolved_path = lexical_path.resolve(strict=False)
        resolved_relative = resolved_path.relative_to(workspace_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise _PolicyError(
            "target cannot be resolved inside the isolated workspace"
        ) from error

    relative_path = resolved_relative.as_posix()
    return ("" if relative_path == "." else relative_path), resolved_path


def _approved_read_paths(workspace_root: Path) -> frozenset[str]:
    """Load the exact read scope injected by the trusted remediation caller.

    The remediation agent has neither a shell nor a way to mutate its parent
    process environment.  Its prompt cannot enlarge this scope.  The caller
    must include every source target and any already-sanitized evidence file
    the agent may read; glob patterns are deliberately not accepted here.
    """
    raw_paths = os.environ.get(_APPROVED_READ_PATHS_ENV)
    if raw_paths is None:
        raise _PolicyError("approved remediation read paths are missing")

    try:
        supplied_paths = json.loads(raw_paths)
    except json.JSONDecodeError as error:
        raise _PolicyError("approved remediation read paths must be JSON") from error

    if not isinstance(supplied_paths, list) or not supplied_paths:
        raise _PolicyError(
            "approved remediation read paths must be a non-empty JSON list"
        )

    approved_paths: set[str] = set()
    for supplied_path in supplied_paths:
        if not isinstance(supplied_path, str) or any(
            character in supplied_path for character in "*?["
        ):
            raise _PolicyError(
                "approved remediation read paths must be exact file paths"
            )
        if Path(supplied_path).is_absolute():
            raise _PolicyError(
                "approved remediation read paths must be workspace-relative"
            )
        relative_path, _ = _relative_target(supplied_path, workspace_root)
        if not relative_path:
            raise _PolicyError("approved remediation read paths must name files")
        approved_paths.add(relative_path.casefold())

    return frozenset(approved_paths)


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def file_access_denial(
    *,
    workspace_root: Path,
    tool_name: str,
    file_path: object,
    approved_read_paths: Sequence[str] | None = None,
) -> str | None:
    """Return the shared deterministic denial reason for one file operation."""

    action = "read" if tool_name in _READ_TOOLS else "write"
    try:
        resolved_root = workspace_root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise _PolicyError("isolated workspace is not a directory")
        config_path = resolved_root / _CONFIG_RELATIVE_PATH
        config_path.resolve(strict=False).relative_to(resolved_root)
        relative_path, resolved_path = _relative_target(file_path, resolved_root)
        editable_paths, editable_extensions, sensitive_paths = _load_safety(config_path)
    except (OSError, RuntimeError, ValueError) as error:
        return f"Remediation {action} blocked: policy could not be verified ({error})."

    if tool_name in _READ_TOOLS:
        try:
            if approved_read_paths is None:
                approved = _approved_read_paths(resolved_root)
            else:
                approved = frozenset(
                    _relative_target(path, resolved_root)[0].casefold()
                    for path in approved_read_paths
                )
        except (OSError, RuntimeError, ValueError) as error:
            reason = "Remediation read blocked: caller-approved read scope could not be verified"
            return f"{reason} ({error})."
        read_sensitive_paths = (*sensitive_paths, *_READ_SENSITIVE_PATTERNS)
        if _matches_any_sensitive_path(relative_path, read_sensitive_paths):
            return (
                "Remediation read blocked: target matches a protected sensitive path."
            )
        if relative_path.casefold() not in approved:
            return (
                "Remediation read blocked: target is outside the caller-approved "
                "read scope."
            )
        return None

    if relative_path.casefold() == _CONFIG_RELATIVE_PATH.as_posix().casefold():
        return (
            "Remediation write blocked: the project safety configuration is protected."
        )
    if _matches_any_sensitive_path(relative_path, sensitive_paths):
        return "Remediation write blocked: target matches safety.sensitive_paths."
    if not _matches_any(relative_path, editable_paths):
        return "Remediation write blocked: target is outside safety.editable_paths."
    if resolved_path.suffix.casefold() not in {
        extension.casefold() for extension in editable_extensions
    }:
        return "Remediation write blocked: target extension is not editable."
    return None


def enforce_file_event(event: object) -> dict[str, Any] | None:
    """Return a deny decision for an unauthorized remediation file-tool call."""
    if not isinstance(event, Mapping):
        raise _PolicyError("hook input must be a JSON object")

    tool_name = event.get("tool_name")
    if tool_name not in _FILE_TOOLS:
        return None
    if not _is_remediation_agent(event.get("agent_type")):
        return None

    tool_input = event.get("tool_input")
    if not isinstance(tool_input, Mapping):
        action = "read" if tool_name in _READ_TOOLS else "write"
        return _deny(f"Remediation {action} blocked: file tool input is invalid.")

    cwd = event.get("cwd", os.getcwd())
    if not isinstance(cwd, str) or not cwd:
        action = "read" if tool_name in _READ_TOOLS else "write"
        return _deny(
            f"Remediation {action} blocked: isolated workspace is unavailable."
        )

    denial = file_access_denial(
        workspace_root=Path(cwd),
        tool_name=tool_name,
        file_path=tool_input.get("file_path"),
    )
    return _deny(denial) if denial is not None else None


def main() -> int:
    """Read a Claude Code file-tool event and emit a deny decision when required."""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        event = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        print("file-access hook input must be valid UTF-8 JSON", file=sys.stderr)
        return 2

    try:
        response = enforce_file_event(event)
    except _PolicyError as error:
        print(f"file-access hook refused the tool call: {error}", file=sys.stderr)
        return 2

    if response is not None:
        json.dump(response, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
