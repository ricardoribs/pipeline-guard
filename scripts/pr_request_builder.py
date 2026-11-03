# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Assemble a neutral PR request from remediation and test execution outputs."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

try:  # Support both direct script execution and package-style imports.
    from .redaction import redact_text
except ImportError:  # pragma: no cover - direct script execution
    from redaction import redact_text

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_INCIDENT_SCHEMA_PATH = (
    _PLUGIN_ROOT / "contracts" / "schema" / "incident_input_v1.schema.json"
)
_DIAGNOSIS_SCHEMA_PATH = (
    _PLUGIN_ROOT / "contracts" / "schema" / "pipeline_diagnosis_v1.schema.json"
)


class PRRequestBuilderError(ValueError):
    """Raised when a PR request input is invalid or inconsistent."""


@dataclass(frozen=True)
class PRRequest:
    title: str
    body: str
    branch: str
    base: str
    draft: bool
    failure_signature: str | None = None


def build_pr_request(
    incident: object,
    diagnosis: object,
    *,
    branch: str,
    base: str,
    commit: str,
    files_changed: list[dict[str, Any]],
    test_result: object,
    limitations: list[str] | None = None,
    failure_signature: str | None = None,
    secret_values: Sequence[str] = (),
) -> PRRequest:
    """Build a neutral PR request. Does not make network calls."""
    incident_doc = _validated_mapping(
        incident, schema_path=_INCIDENT_SCHEMA_PATH, label="incident_input_v1"
    )
    diagnosis_doc = _validated_mapping(
        diagnosis, schema_path=_DIAGNOSIS_SCHEMA_PATH, label="pipeline_diagnosis_v1"
    )

    if incident_doc["incident_id"] != diagnosis_doc["incident_id"]:
        raise PRRequestBuilderError(
            f"incident_id mismatch: incident has {incident_doc['incident_id']!r}, "
            f"diagnosis has {diagnosis_doc['incident_id']!r}"
        )

    if not branch:
        raise PRRequestBuilderError("branch must not be empty")
    if not base:
        raise PRRequestBuilderError("base must not be empty")
    if not commit:
        raise PRRequestBuilderError("commit must not be empty")
    normalized_signature = _failure_signature(failure_signature)

    _check_test_result(test_result)

    incident_run_id = incident_doc.get("run_id")
    test_run_id = test_result.run_id  # type: ignore[attr-defined]
    if incident_run_id is not None and test_run_id and incident_run_id != test_run_id:
        raise PRRequestBuilderError(
            f"run_id mismatch: incident has {incident_run_id!r}, test_result has {test_run_id!r}"
        )

    merged_limitations = _merge_limitations(
        list(diagnosis_doc.get("limitations") or []),
        list(limitations or []),
    )

    title = _sanitize_text(_build_title(incident_doc, diagnosis_doc), secret_values)
    body = _sanitize_text(
        _build_body(
            incident_doc,
            diagnosis_doc,
            files_changed,
            test_result,
            merged_limitations,
            commit=commit,
            failure_signature=normalized_signature,
        ),
        secret_values,
    )
    return PRRequest(
        title=title,
        body=body,
        branch=branch,
        base=base,
        draft=True,
        failure_signature=normalized_signature,
    )


def _check_test_result(test_result: object) -> None:
    for attr in ("validation_status", "results", "run_id"):
        if not hasattr(test_result, attr):
            raise PRRequestBuilderError(
                f"test_result is missing required attribute {attr!r}"
            )


def _merge_limitations(from_diagnosis: list[str], from_arg: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for item in (*from_diagnosis, *from_arg):
        if item not in seen:
            seen.add(item)
            merged.append(item)
    return merged


def _build_title(incident: dict[str, Any], diagnosis: dict[str, Any]) -> str:
    failure_family = diagnosis["failure_family"]
    pipeline_id = incident.get("pipeline_id")
    failed_step = incident.get("failed_step")

    scope = f"({_inline(pipeline_id)})" if pipeline_id else ""
    step_part = f" in {_inline(failed_step)}" if failed_step else ""

    return f"fix{scope}: remediate {failure_family} failure{step_part}"


def _build_body(
    incident: dict[str, Any],
    diagnosis: dict[str, Any],
    files_changed: list[dict[str, Any]],
    test_result: object,
    limitations: list[str],
    *,
    commit: str,
    failure_signature: str | None,
) -> str:
    lines: list[str] = []
    _section_incident(
        lines,
        incident,
        commit=commit,
        failure_signature=failure_signature,
    )
    _section_diagnosis(lines, diagnosis)
    _section_evidence(lines, diagnosis)
    _section_files(lines, files_changed)
    _section_tests(lines, test_result)
    _section_limitations(lines, limitations)
    return "\n".join(lines) + "\n"


def _section_incident(
    lines: list[str],
    incident: dict[str, Any],
    *,
    commit: str,
    failure_signature: str | None,
) -> None:
    def _field(key: str) -> str:
        v = incident.get(key)
        return _text(v) if v else "unavailable"

    lines += [
        "## Incident",
        "",
        f"ID: {_text(incident['incident_id'])}",
        f"Pipeline: {_field('pipeline_id')}",
        f"Run: {_inline_code(_field('run_id'))}",
        f"Step: {_field('failed_step')}",
        f"Environment: {_field('environment')}",
        f"Occurred at: {_field('occurred_at')}",
        f"Error summary: {_field('error_summary')}",
        f"Commit: {_text(commit)}",
    ]
    if failure_signature is not None:
        lines.append(f"Signature: {failure_signature}")


def _section_diagnosis(lines: list[str], diagnosis: dict[str, Any]) -> None:
    lines += [
        "",
        "## Diagnosis",
        "",
        f"Cause: {_text(diagnosis['cause'])}",
        f"Failure family: {diagnosis['failure_family']}",
        f"Confidence: {diagnosis['confidence']}",
        f"Cause status: {diagnosis['cause_status']}",
    ]
    if diagnosis.get("impact_summary"):
        lines.append(f"Impact: {_text(diagnosis['impact_summary'])}")


def _section_evidence(lines: list[str], diagnosis: dict[str, Any]) -> None:
    lines += ["", "## Evidence", ""]
    citations = diagnosis.get("evidence_citations") or []
    if citations:
        lines += [
            f"- {_text(c['evidence_id'])}: {_text(c['claim'])}" for c in citations
        ]
    else:
        lines.append("No evidence citations.")


def _section_files(lines: list[str], files_changed: list[dict[str, Any]]) -> None:
    lines += ["", "## Files changed", ""]
    if not files_changed:
        lines.append("No files changed.")
    else:
        lines += [
            "| File | Lines added | Lines removed |",
            "| --- | --- | --- |",
        ]
        for f in files_changed:
            lines.append(
                f"| {_text(str(f['path']))} | {f['lines_added']} | {f['lines_removed']} |"
            )


def _section_tests(lines: list[str], test_result: object) -> None:
    lines += ["", "## Tests", ""]
    lines.append(f"Validation status: {test_result.validation_status}")  # type: ignore[attr-defined]
    lines.append(f"Run: {_inline_code(_text(test_result.run_id))}")  # type: ignore[attr-defined]
    lines.append("")
    results = test_result.results  # type: ignore[attr-defined]
    if not results:
        lines.append("No test commands were executed.")
    else:
        lines += [
            "| Command | Status | Duration (s) |",
            "| --- | --- | --- |",
        ]
        for r in results:
            duration = _inline_code(_format_duration(r.duration_seconds))
            lines.append(f"| {_text(r.name)} | {r.status} | {duration} |")


def _section_limitations(lines: list[str], limitations: list[str]) -> None:
    lines += ["", "## Limitations", ""]
    if not limitations:
        lines.append("None.")
    else:
        lines += [f"- {_text(item)}" for item in limitations]


def _inline(value: str) -> str:
    """Strip newlines from a value used inline in a single-line context."""
    return " ".join(value.splitlines())


def _inline_code(value: str) -> str:
    """Render an already escaped scalar as an inline Markdown code value."""
    return f"`{value}`"


def _format_duration(value: float) -> str:
    """Keep recorded durations readable and avoid provider auto-linking long decimals."""
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _text(value: str) -> str:
    """Escape untrusted text for safe embedding in Markdown."""
    escaped = html.escape(" ".join(value.splitlines()), quote=True)
    for character in "\\`*_{}[]()#!|":
        escaped = escaped.replace(character, "\\" + character)
    return escaped


def _failure_signature(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
    ):
        raise PRRequestBuilderError(
            "failure_signature must use sha256 with 64 lowercase hexadecimal digits"
        )
    return value


def _sanitize_text(value: str, secrets: Sequence[str]) -> str:
    if isinstance(secrets, (str, bytes)):
        raise PRRequestBuilderError("secret_values must be a sequence of strings")
    sanitized = redact_text(value)
    for secret in secrets:
        if not isinstance(secret, str):
            raise PRRequestBuilderError("secret_values must contain only strings")
        if secret:
            encoded_candidates = {
                secret,
                html.escape(secret, quote=False),
                html.escape(secret, quote=True),
                json.dumps(secret, ensure_ascii=False)[1:-1],
                _text(secret),
            }
            for candidate in encoded_candidates:
                sanitized = sanitized.replace(candidate, "[REDACTED]")
    return sanitized


def _validated_mapping(
    value: object,
    *,
    schema_path: Path,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PRRequestBuilderError(f"{label} must be an object")
    document = dict(value)
    validator = Draft202012Validator(_load_schema(schema_path))
    errors = sorted(
        validator.iter_errors(document), key=lambda e: list(e.absolute_path)
    )
    if errors:
        first = errors[0]
        field = ".".join(str(p) for p in first.absolute_path) or "<root>"
        raise PRRequestBuilderError(f"invalid {label} at {field}: {first.message}")
    return document


@cache
def _load_schema(path: Path) -> dict[str, Any]:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PRRequestBuilderError(f"cannot load schema: {path}") from error
    Draft202012Validator.check_schema(schema)
    return schema
