# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Deterministic validation for patches against safety controls before remediation applies them."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from functools import cache
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


class PatchValidationError(ValueError):
    """Raised when a patch violates safety controls and cannot proceed."""


def _normalize_path(path: object) -> tuple[str, str, str | None]:
    """Return display, policy, and optional ambiguity-violation paths."""
    if not isinstance(path, str) or not path:
        raise PatchValidationError("patch file paths must be non-empty strings")
    if "\\" in path:
        raise PatchValidationError(
            f"path '{path}' contains a backslash; only forward-slash separators are accepted"
        )
    posix_path = PurePosixPath(path)
    if posix_path.is_absolute() or PureWindowsPath(path).drive:
        raise PatchValidationError(
            f"path '{path}' is absolute and cannot be used in a patch"
        )
    policy_parts: list[str] = []
    windows_ambiguous = False
    for segment in posix_path.parts:
        if segment == "..":
            raise PatchValidationError(
                f"path '{path}' contains a '..' segment and cannot be used in a patch"
            )
        if segment.endswith((".", " ")):
            windows_ambiguous = True
        # Win32 resolves a component with trailing spaces or periods as this
        # canonical component.  Match policies against that identity too.
        policy_parts.append(segment.rstrip(". ") or segment)
    display_path = str(posix_path)
    if display_path == ".":
        raise PatchValidationError("patch file paths must name files")
    policy_path = "/".join(policy_parts)
    ambiguity_violation = None
    if windows_ambiguous:
        ambiguity_violation = (
            f"file '{display_path}' contains a path component ending in a dot or space "
            "and is ambiguous on Windows"
        )
    return display_path, policy_path, ambiguity_violation


def _normalize_pattern(pattern: object, field: str) -> str:
    if not isinstance(pattern, str):
        raise PatchValidationError(f"{field} entries must be strings")
    normalized = pattern.replace("\\", "/")
    if not normalized or normalized != normalized.strip():
        raise PatchValidationError(
            f"{field} entries must be non-empty without surrounding spaces"
        )
    if normalized.startswith("/") or PureWindowsPath(normalized).drive:
        raise PatchValidationError(f"{field} entries must be workspace-relative")
    if ".." in normalized.split("/"):
        raise PatchValidationError(
            f"{field} entries must not contain parent-directory segments"
        )
    if any(
        part.endswith((".", " "))
        for part in normalized.split("/")
        if part not in {"", "."}
    ):
        raise PatchValidationError(
            f"{field} entries must not end a path component with a dot or space"
        )
    normalized = "/".join(
        part for part in normalized.split("/") if part not in {"", "."}
    )
    if not normalized:
        raise PatchValidationError(f"{field} entries must name a path or extension")
    return normalized


def _matches_glob(path: str, pattern: str) -> bool:
    """Match safety globs with the same segment semantics as the PreToolUse hook."""
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


def _normalise_patch_files(
    patch_files: object,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(patch_files, list):
        raise PatchValidationError("patch_files must be a list")

    normalized: list[dict[str, Any]] = []
    violations: list[str] = []
    for index, file_data in enumerate(patch_files):
        if not isinstance(file_data, Mapping):
            raise PatchValidationError(f"patch file at index {index} must be an object")
        path, policy_path, ambiguity_violation = _normalize_path(file_data.get("path"))
        if ambiguity_violation is not None:
            violations.append(ambiguity_violation)
        lines_changed = file_data.get("lines_changed")
        if type(lines_changed) is not int or lines_changed < 0:
            violations.append(
                f"file '{path}' has invalid lines_changed value '{lines_changed}'; "
                "lines_changed must be a non-negative integer"
            )
            continue
        changed_content = file_data.get("changed_content")
        if changed_content is not None and not isinstance(changed_content, str):
            violations.append(
                f"file '{path}' has invalid changed_content; changed_content must be a string"
            )
            continue
        normalized.append(
            {
                "path": path,
                "policy_path": policy_path,
                "lines_changed": lines_changed,
                "changed_content": changed_content,
            }
        )
    return normalized, violations


def _merge_patch_files(patch_files: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for file_data in patch_files:
        policy_path = file_data["policy_path"]
        identity = policy_path.casefold()
        changed_content = file_data["changed_content"]
        changed_lines = (
            None if changed_content is None else changed_content.splitlines()
        )
        if identity not in merged:
            merged[identity] = {
                "path": file_data["path"],
                "policy_path": policy_path,
                "lines_changed": 0,
                "changed_lines": changed_lines,
            }
        else:
            existing_lines = merged[identity]["changed_lines"]
            if existing_lines is None or changed_lines is None:
                merged[identity]["changed_lines"] = None
            else:
                existing_lines.extend(changed_lines)
        merged[identity]["lines_changed"] += file_data["lines_changed"]
    return list(merged.values())


def _normalize_base_commit(base_commit: object) -> str:
    """Require the complete commit identifier verified by the workspace executor."""
    if (
        not isinstance(base_commit, str)
        or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", base_commit) is None
    ):
        raise PatchValidationError(
            "base_commit must be a complete 40- or 64-character hexadecimal commit identifier"
        )
    return base_commit.lower()


def _require_applied_patch(applied_patch: object) -> bytes:
    """Require the exact non-empty bytes produced from the prepared workspace's diff."""
    if not isinstance(applied_patch, bytes) or not applied_patch:
        raise PatchValidationError(
            "applied_patch must be non-empty bytes from the prepared workspace"
        )
    return applied_patch


def _hash_applied_patch(applied_patch: bytes) -> str:
    """Hash exact applied diff bytes after their type and presence were checked."""
    return f"sha256:{hashlib.sha256(applied_patch).hexdigest()}"


def _parse_diff_header_token(line: bytes, start: int) -> tuple[bytes, int] | None:
    """Parse one Git C-style quoted or unquoted path token from a diff header."""
    if start >= len(line):
        return None
    if line[start] != ord('"'):
        end = line.find(b" ", start)
        if end == -1:
            end = len(line)
        token = line[start:end]
        if not token or b'"' in token or b"\\" in token:
            return None
        return token, end

    decoded = bytearray()
    index = start + 1
    escapes = {
        ord('"'): ord('"'),
        ord("\\"): ord("\\"),
        ord("a"): ord("\a"),
        ord("b"): ord("\b"),
        ord("f"): ord("\f"),
        ord("n"): ord("\n"),
        ord("r"): ord("\r"),
        ord("t"): ord("\t"),
        ord("v"): ord("\v"),
    }
    while index < len(line):
        character = line[index]
        if character == ord('"'):
            return bytes(decoded), index + 1
        if character != ord("\\"):
            decoded.append(character)
            index += 1
            continue

        index += 1
        if index >= len(line):
            return None
        escaped = line[index]
        if escaped in escapes:
            decoded.append(escapes[escaped])
            index += 1
            continue
        if ord("0") <= escaped <= ord("7"):
            octal = line[index : index + 3]
            if len(octal) != 3 or any(
                byte < ord("0") or byte > ord("7") for byte in octal
            ):
                return None
            decoded.append(int(octal, 8))
            index += 3
            continue
        return None
    return None


def _parse_git_mode(mode: bytes) -> int | None:
    """Return a six-digit octal Git mode or ``None`` when it is malformed."""
    if re.fullmatch(rb"[0-7]{6}", mode) is None:
        return None
    return int(mode, 8)


def _record_git_mode(
    file_data: dict[str, Any], mode: int, violations: list[str]
) -> None:
    """Reject Git entry types that cannot be validated by path policy."""
    modes: set[int] = file_data["modes"]
    if mode in modes:
        return
    modes.add(mode)

    path = file_data["path"]
    if mode == 0o120000:
        violations.append(
            f"applied_patch contains a symbolic link for '{path}', which cannot be "
            "validated against path safety controls"
        )
    elif mode == 0o160000:
        violations.append(
            f"applied_patch contains a submodule for '{path}', which cannot be "
            "validated against path safety controls"
        )
    elif mode not in {0o100644, 0o100755}:
        violations.append(
            f"applied_patch contains unsupported Git mode {mode:06o} for '{path}'"
        )


def _empty_applied_file(path: str, section: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": path,
        "policy_path": path,
        "lines_changed": 0,
        "changed_lines": [],
        "changed_content": "",
        "binary": False,
        "modes": set(),
        "old_mode": None,
        "new_mode": None,
        "section": section,
    }


def _parse_applied_patch(
    applied_patch: bytes,
    *,
    entry_modes: Mapping[str, object] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse paths, changed-line counts, and added lines from a Git binary/text diff.

    A binary patch is rejected for volume and content validation: its changed
    line count and content cannot be established from the textual diff.
    """
    files: dict[str, dict[str, Any]] = {}
    violations: list[str] = []
    header_count = 0
    current_file: dict[str, Any] | None = None
    in_hunk = False
    # Git diff records are delimited by LF.  Do not use bytes.splitlines(): a
    # bare CR is valid added content and must not create a new parser record.
    # A CR immediately before the LF delimiter is a normal CRLF line ending,
    # so discard it to retain the pre-existing normalized text behavior.
    for line_number, raw_line in enumerate(applied_patch.split(b"\n"), start=1):
        line = raw_line.removesuffix(b"\r")
        if not line.startswith(b"diff --git "):
            if current_file is None:
                continue
            if line == b"GIT binary patch" or line.startswith(b"Binary files "):
                current_file["binary"] = True
                in_hunk = False
                continue
            if line.startswith(
                (b"rename from ", b"rename to ", b"copy from ", b"copy to ")
            ):
                current_file["section"]["rename_or_copy"] = True
                continue
            for prefix, field in (
                (b"new file mode ", None),
                (b"deleted file mode ", None),
                (b"old mode ", "old_mode"),
                (b"new mode ", "new_mode"),
            ):
                if line.startswith(prefix):
                    mode = _parse_git_mode(line[len(prefix) :])
                    if mode is None:
                        violations.append(
                            f"applied_patch has an invalid Git mode at line {line_number}"
                        )
                    else:
                        _record_git_mode(current_file, mode, violations)
                        current_file["section"]["modes"].add(mode)
                        if field is not None:
                            current_file[field] = mode
                    break
            else:
                if line.startswith(b"index "):
                    index_parts = line.split(b" ")
                    if len(index_parts) == 3:
                        mode = _parse_git_mode(index_parts[2])
                        if mode is None:
                            violations.append(
                                f"applied_patch has an invalid Git mode at line {line_number}"
                            )
                        else:
                            _record_git_mode(current_file, mode, violations)
                            current_file["section"]["modes"].add(mode)
                    continue
                if line.startswith(b"@@ "):
                    in_hunk = True
                    continue
                if in_hunk:
                    if line.startswith(b"+"):
                        changed_line = line[1:]
                        # Count logical source lines as well as Git records.  A
                        # bare CR within a record must not evade volume limits.
                        current_file["lines_changed"] += changed_line.count(b"\r") + 1
                        current_file["changed_lines"].append(changed_line)
                    elif line.startswith(b"-"):
                        current_file["lines_changed"] += line[1:].count(b"\r") + 1
                continue
            continue

        header_count += 1
        current_file = None
        in_hunk = False
        start = len(b"diff --git ")
        previous = _parse_diff_header_token(line, start)
        if previous is None:
            violations.append(
                f"applied_patch has an invalid diff --git header at line {line_number}"
            )
            continue
        previous_path, next_start = previous
        if next_start >= len(line) or line[next_start] != ord(" "):
            violations.append(
                f"applied_patch has an invalid diff --git header at line {line_number}"
            )
            continue
        current = _parse_diff_header_token(line, next_start + 1)
        if current is None or current[1] != len(line):
            violations.append(
                f"applied_patch has an invalid diff --git header at line {line_number}"
            )
            continue
        current_path, _ = current

        header_paths: list[tuple[str, str]] = []
        for side, prefix, header_path in (
            ("previous", b"a/", previous_path),
            ("current", b"b/", current_path),
        ):
            if not header_path.startswith(prefix) or len(header_path) == len(prefix):
                violations.append(
                    f"applied_patch has an invalid diff --git path at line {line_number}"
                )
                continue
            try:
                path = header_path[len(prefix) :].decode("utf-8")
            except UnicodeDecodeError:
                violations.append(
                    f"applied_patch has a non-UTF-8 diff --git path at line {line_number}"
                )
                continue
            if any(ord(character) < 32 or ord(character) == 127 for character in path):
                violations.append(
                    "applied_patch has a control character in a diff --git path "
                    f"at line {line_number}"
                )
                continue
            try:
                _display_path, policy_path, ambiguity_violation = _normalize_path(path)
            except PatchValidationError as error:
                violations.append(
                    f"applied_patch has an invalid diff --git path: {error}"
                )
                continue
            if ambiguity_violation is not None:
                violations.append(f"applied_patch {ambiguity_violation}")
                continue
            header_paths.append((side, policy_path))

        if len(header_paths) != 2:
            continue
        previous_policy_path = header_paths[0][1]
        current_policy_path = header_paths[1][1]
        section_paths = [current_policy_path]
        if previous_policy_path.casefold() != current_policy_path.casefold():
            section_paths.insert(0, previous_policy_path)
        duplicate = next(
            (path for path in section_paths if path.casefold() in files),
            None,
        )
        if duplicate is not None:
            violations.append(
                f"applied_patch contains multiple diff sections for '{duplicate}'"
            )
            continue
        section = {
            "paths": tuple(section_paths),
            "rename_or_copy": False,
            "modes": set(),
        }
        for path in section_paths:
            files[path.casefold()] = _empty_applied_file(path, section)
        current_file = files[current_policy_path.casefold()]

    if header_count == 0:
        violations.append("applied_patch must contain at least one diff --git header")
    normalized_entry_modes = {
        path.casefold(): value
        for path, value in (entry_modes or {}).items()
        if isinstance(path, str)
    }
    seen_sections: set[int] = set()
    for file_data in files.values():
        section = file_data["section"]
        section_id = id(section)
        if section_id in seen_sections:
            continue
        seen_sections.add(section_id)
        if not section["rename_or_copy"] or section["modes"]:
            continue
        trusted_modes: set[int] = set()
        for path in section["paths"]:
            supplied_modes = normalized_entry_modes.get(path.casefold(), ())
            if isinstance(supplied_modes, int) and not isinstance(supplied_modes, bool):
                trusted_modes.add(supplied_modes)
            elif isinstance(supplied_modes, (list, tuple, set, frozenset)):
                trusted_modes.update(
                    mode
                    for mode in supplied_modes
                    if isinstance(mode, int) and not isinstance(mode, bool)
                )
        if not trusted_modes:
            violations.append(
                "applied_patch rename or copy lacks trusted Git mode provenance for "
                f"'{file_data['path']}'"
            )
            continue
        for path in section["paths"]:
            target = files[path.casefold()]
            for mode in trusted_modes:
                _record_git_mode(target, mode, violations)
    for file_data in files.values():
        old_mode = file_data["old_mode"]
        new_mode = file_data["new_mode"]
        if (old_mode is None and new_mode is not None) or (
            old_mode is not None and new_mode is None
        ):
            violations.append(
                f"applied_patch has an incomplete Git mode change for '{file_data['path']}'"
            )
        elif old_mode is not None and old_mode != new_mode:
            violations.append(
                f"applied_patch changes Git file mode for '{file_data['path']}' from "
                f"{old_mode:06o} to {new_mode:06o}; mode changes are not allowed by safety policy"
            )
        if file_data["binary"]:
            violations.append(
                f"applied_patch contains binary changes for '{file_data['path']}', "
                "which cannot be checked against volume or blocked_patterns controls"
            )
            continue
        try:
            file_data["changed_content"] = b"\n".join(
                file_data["changed_lines"]
            ).decode("utf-8")
        except UnicodeDecodeError:
            violations.append(
                f"applied_patch contains non-UTF-8 added content for '{file_data['path']}'"
            )
    return list(files.values()), violations


def _reconcile_applied_patch(
    patch_files: Sequence[dict[str, Any]],
    applied_patch: bytes,
    *,
    entry_modes: Mapping[str, object] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Reject metadata unless paths, counts, and added content equal applied diff bytes."""
    applied_files, violations = _parse_applied_patch(
        applied_patch, entry_modes=entry_modes
    )
    expected_files = {
        file_data["policy_path"].casefold(): file_data for file_data in patch_files
    }
    applied_by_path = {
        file_data["policy_path"].casefold(): file_data for file_data in applied_files
    }
    expected_paths = set(expected_files)
    applied_paths = set(applied_by_path)
    if applied_paths != expected_paths:
        unexpected_paths = sorted(applied_paths - expected_paths)
        missing_paths = sorted(expected_paths - applied_paths)
        details: list[str] = []
        if unexpected_paths:
            details.append(f"only in applied_patch: {', '.join(unexpected_paths)}")
        if missing_paths:
            details.append(f"missing from applied_patch: {', '.join(missing_paths)}")
        violations.append(
            "applied_patch paths do not match the normalized patch_files paths ("
            + "; ".join(details)
            + ")"
        )
        return applied_files, violations

    for identity, expected_file in expected_files.items():
        applied_file = applied_by_path[identity]
        expected_content = expected_file["changed_lines"]
        expected_line_count = expected_file["lines_changed"]
        if expected_line_count != applied_file["lines_changed"]:
            violations.append(
                f"file '{expected_file['path']}' lines_changed does not match applied_patch"
            )
        if expected_content is None:
            violations.append(
                f"file '{expected_file['path']}' has no changed_content "
                "to reconcile with applied_patch"
            )
        elif expected_content != applied_file.get("changed_content", "").splitlines():
            violations.append(
                f"file '{expected_file['path']}' changed_content does not match applied_patch"
            )

    return applied_files, violations


def validate_patch(
    patch_files: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    verified_base_commit: object,
    applied_patch: object,
    entry_modes: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Validate a list of patch files against the safety configuration.

    Returns a result dict with:
    - ``accepted``: True only when all controls pass.
    - ``violations``: one sentence per failed control.
    - ``requires_human_review``: True when any control or input validation fails.
    - ``base_commit`` and ``patch_hash``: provenance for an accepted patch.

    Each item must contain ``path``, a non-negative integer ``lines_changed``,
    and ``changed_content`` representing its added lines. Those metadata values
    are reconciled with the applied diff and never used as the source of the
    content or volume controls.

    ``verified_base_commit`` must be obtained from the prepared workspace's
    verified base, not copied from untrusted remediation output.
    ``applied_patch`` must be the exact non-empty bytes of that workspace's
    applied diff (including binary changes when present), and its Git diff
    headers must name exactly the files validated here. ``entry_modes`` must
    come from the same workspace's raw Git diff when a rename or copy omits mode
    lines. The returned mapping is the intermediate provenance record that later
    test and Git stages must persist unchanged and re-check against their workspace.
    """
    normalized_base_commit = _normalize_base_commit(verified_base_commit)
    applied_patch = _require_applied_patch(applied_patch)
    patch_hash = _hash_applied_patch(applied_patch)
    normalized_files, violations = _normalise_patch_files(patch_files)
    requires_human_review = bool(violations)
    if not isinstance(config, Mapping) or not isinstance(
        config.get("safety", {}), Mapping
    ):
        raise PatchValidationError("config.safety must be an object")

    safety = config.get("safety", {})
    editable_extensions = safety.get("editable_extensions") or []
    editable_paths = safety.get("editable_paths") or []
    sensitive_paths = safety.get("sensitive_paths") or []
    limits = safety.get("limits") or {}
    if not all(
        isinstance(value, list)
        for value in (editable_extensions, editable_paths, sensitive_paths)
    ):
        raise PatchValidationError("safety path and extension controls must be lists")
    if not isinstance(limits, Mapping):
        raise PatchValidationError("safety.limits must be an object")

    editable_extensions = [
        _normalize_pattern(extension, "editable_extensions").casefold()
        for extension in editable_extensions
    ]
    editable_paths = [
        _normalize_pattern(pattern, "editable_paths") for pattern in editable_paths
    ]
    sensitive_paths = [
        _normalize_pattern(pattern, "sensitive_paths") for pattern in sensitive_paths
    ]
    metadata_patch_files = _merge_patch_files(normalized_files)
    applied_patch_files, applied_patch_violations = _reconcile_applied_patch(
        metadata_patch_files,
        applied_patch,
        entry_modes=entry_modes,
    )
    if applied_patch_violations:
        requires_human_review = True
        violations.extend(applied_patch_violations)

    # Control 1 — allowed extensions
    if not editable_extensions:
        requires_human_review = True
        violations.append(
            "editable_extensions is empty or absent; all file extensions are blocked"
            " until the list is configured"
        )
    else:
        for f in metadata_patch_files:
            path = f["path"]
            ext = PurePosixPath(f["policy_path"]).suffix.casefold()
            if ext not in editable_extensions:
                requires_human_review = True
                violations.append(
                    f"file '{path}' has extension '{ext}' which is not in editable_extensions"
                )

    # Control 2 — allowed paths
    if not editable_paths:
        requires_human_review = True
        violations.append(
            "editable_paths is empty or absent; all paths are blocked until the list is configured"
        )
    else:
        for f in metadata_patch_files:
            path = f["path"]
            if not _matches_any(f["policy_path"], editable_paths):
                requires_human_review = True
                violations.append(
                    f"file '{path}' does not match any pattern in editable_paths"
                )

    # Control 3 — sensitive paths (always blocks, never skipped)
    for f in metadata_patch_files:
        path = f["path"]
        if _matches_any_sensitive_path(f["policy_path"], sensitive_paths):
            requires_human_review = True
            violations.append(
                f"file '{path}' matches a sensitive_paths pattern and requires human review"
            )

    # Control 4 — volume limits
    max_files = limits.get("max_files")
    max_lines_per_file = limits.get("max_lines_per_file")
    max_total_lines = limits.get("max_total_lines")
    blocked_patterns = limits.get("blocked_patterns") or []
    if not isinstance(blocked_patterns, list) or any(
        not isinstance(pattern, str) for pattern in blocked_patterns
    ):
        raise PatchValidationError(
            "safety.limits.blocked_patterns must be a list of strings"
        )
    for name, limit in (
        ("max_files", max_files),
        ("max_lines_per_file", max_lines_per_file),
        ("max_total_lines", max_total_lines),
    ):
        if type(limit) is not int or limit < 1:
            requires_human_review = True
            violations.append(f"safety.limits.{name} must be a positive integer")

    compiled_patterns: list[tuple[str, re.Pattern[str]]] = []
    for pattern in blocked_patterns:
        try:
            compiled_patterns.append((pattern, re.compile(pattern)))
        except re.error as error:
            requires_human_review = True
            violations.append(
                f"blocked_patterns entry '{pattern}' is not a valid regex ({error})"
            )
    for file_data in applied_patch_files:
        path = file_data["path"]
        content = file_data["changed_content"]
        for pattern, expression in compiled_patterns:
            if expression.search(content):
                requires_human_review = True
                violations.append(
                    f"file '{path}' changed content matches blocked_patterns regex '{pattern}'"
                )

    if type(max_files) is int and len(applied_patch_files) > max_files:
        requires_human_review = True
        violations.append(
            f"patch contains {len(applied_patch_files)} files which exceeds "
            f"max_files limit of {max_files}"
        )

    total_lines = 0
    for f in applied_patch_files:
        lines = f["lines_changed"]
        total_lines += lines
        if type(max_lines_per_file) is int and lines > max_lines_per_file:
            requires_human_review = True
            violations.append(
                f"file '{f['path']}' has {lines} changed lines"
                f" which exceeds max_lines_per_file limit of {max_lines_per_file}"
            )

    if type(max_total_lines) is int and total_lines > max_total_lines:
        requires_human_review = True
        violations.append(
            f"patch totals {total_lines} changed lines"
            f" which exceeds max_total_lines limit of {max_total_lines}"
        )

    result: dict[str, Any] = {
        "accepted": len(violations) == 0,
        "violations": violations,
        "requires_human_review": requires_human_review,
    }
    if result["accepted"]:
        result["base_commit"] = normalized_base_commit
        result["patch_hash"] = patch_hash
    return result
