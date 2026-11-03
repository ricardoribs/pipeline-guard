# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Build, sanitize, validate, and persist unattended terminal results."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, TextIO, cast

from jsonschema import Draft202012Validator, FormatChecker

try:  # Support both direct script execution and package-style imports.
    from .redaction import redact_payload, redact_text
except ImportError:  # pragma: no cover - direct script execution
    from redaction import redact_payload, redact_text

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = (
    _PLUGIN_ROOT / "contracts" / "schema" / "unattended_run_result_v1.schema.json"
)
_SAFE_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class UnattendedResultError(ValueError):
    """Raised when a terminal result cannot be retained safely."""


def write_unattended_result(
    result: Mapping[str, Any],
    *,
    output_dir: Path | str,
    artifacts: Mapping[str, object] | None = None,
    secret_values: Sequence[str] = (),
    stdout: TextIO | None = None,
) -> dict[str, Any]:
    """Write a schema-valid result, human summary, and hashed terminal artifacts."""
    target = Path(output_dir).expanduser().resolve()
    if not target.is_dir():
        raise UnattendedResultError("output_dir must be an existing directory")
    secrets = _normalized_secrets(secret_values)
    document = _sanitize_result(deepcopy(dict(result)), secrets)
    if not isinstance(
        document, dict
    ):  # pragma: no cover - deepcopy(dict) remains a dict
        raise UnattendedResultError("result must be an object")

    now = datetime.now(UTC)
    document.setdefault("finished_at", _timestamp(now))
    started_at = _parsed_timestamp(document.get("started_at"), "started_at")
    finished_at = _parsed_timestamp(document.get("finished_at"), "finished_at")
    document["duration_seconds"] = max(0.0, (finished_at - started_at).total_seconds())

    retained = _write_artifacts(
        target,
        artifacts or {},
        secret_values=secrets,
    )
    document["artifacts"] = retained
    summary = _render_summary(document)
    summary = _sanitize_text(summary, secrets)
    document["summary"] = summary
    document = _sanitize_result(document, secrets)
    _validate(document)

    _atomic_write(target / "summary.md", summary.encode("utf-8"))
    serialized = (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    _atomic_write(target / "result.json", serialized)
    stream = sys.stdout if stdout is None else stdout
    stream.write(summary)
    if not summary.endswith("\n"):
        stream.write("\n")
    return document


def _write_artifacts(
    output_dir: Path,
    artifacts: Mapping[str, object],
    *,
    secret_values: Sequence[str],
) -> list[dict[str, str]]:
    artifact_dir = output_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    retained: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, value in artifacts.items():
        if not isinstance(name, str) or _SAFE_ARTIFACT_NAME.fullmatch(name) is None:
            raise UnattendedResultError(f"unsafe artifact name: {name!r}")
        key = name.casefold()
        if key in seen:
            raise UnattendedResultError(f"duplicate artifact name: {name}")
        seen.add(key)
        content = _artifact_bytes(_sanitize(value, secret_values))
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UnattendedResultError(
                f"terminal artifact {name!r} must be UTF-8 text"
            ) from error
        sanitized = _sanitize_text(text, secret_values).encode("utf-8")
        path = artifact_dir / name
        _atomic_write(path, sanitized)
        retained.append(
            {
                "name": name,
                "path": path.relative_to(output_dir).as_posix(),
                "sha256": hashlib.sha256(sanitized).hexdigest(),
            }
        )
    return retained


def _artifact_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, Path):
        try:
            return value.read_bytes()
        except OSError as error:
            raise UnattendedResultError(
                f"cannot read terminal artifact {value}"
            ) from error
    if isinstance(value, Mapping | list | tuple):
        return (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        )
    raise UnattendedResultError(
        "terminal artifacts must be bytes, text, paths, or JSON-compatible containers"
    )


def _render_summary(result: Mapping[str, Any]) -> str:
    outcome = str(result.get("outcome", "ERROR"))
    reason = result.get("reason_code")
    diagnosis = result.get("diagnosis")
    diagnosis_text = "Unavailable."
    if isinstance(diagnosis, Mapping):
        cause = diagnosis.get("cause")
        family = diagnosis.get("failure_family")
        status = diagnosis.get("cause_status")
        details = [str(value) for value in (family, status, cause) if value]
        if details:
            diagnosis_text = " — ".join(details)

    lines = [
        "# Pipeline Guard unattended result",
        "",
        f"- Outcome: **{outcome}**",
        f"- Exit code: `{result.get('exit_code', 1)}`",
    ]
    if reason:
        lines.append(f"- Reason: `{reason}`")
    lines += ["", "## Diagnosis", "", diagnosis_text]

    host_run = result.get("host_run")
    if isinstance(host_run, Mapping):
        related = host_run.get("related_failed_steps")
        if isinstance(related, list) and related:
            lines += ["", "## Related failed steps", ""]
            lines.extend(f"- {step}" for step in related)

    limitations = result.get("limitations")
    lines += ["", "## Limitations", ""]
    if isinstance(limitations, list) and limitations:
        lines.extend(f"- {item}" for item in limitations)
    else:
        lines.append("None recorded.")

    draft_pr = result.get("draft_pr")
    if isinstance(draft_pr, Mapping) and draft_pr.get("url"):
        lines += ["", "## Draft PR", "", str(draft_pr["url"])]
    pushed_branch = result.get("pushed_branch")
    if pushed_branch:
        lines += ["", "## Pushed branch", "", f"`{pushed_branch}`"]
    return "\n".join(lines).rstrip() + "\n"


def _sanitize(value: Any, secrets: Sequence[str]) -> Any:
    redacted = redact_payload(value)
    if isinstance(redacted, str):
        return _sanitize_text(redacted, secrets)
    if isinstance(redacted, list):
        return [_sanitize(item, secrets) for item in redacted]
    if isinstance(redacted, dict):
        return {key: _sanitize(item, secrets) for key, item in redacted.items()}
    return redacted


def _sanitize_result(value: dict[str, Any], secrets: Sequence[str]) -> dict[str, Any]:
    """Sanitize a result without treating its safe authorization summary as a credential."""
    authorization = value.pop("authorization", None)
    sanitized = _sanitize(value, secrets)
    if not isinstance(sanitized, dict):  # pragma: no cover - input is always a dict
        raise UnattendedResultError("result must remain an object after sanitization")
    if authorization is not None:
        if not isinstance(authorization, Mapping):
            raise UnattendedResultError("authorization must be an object")
        sanitized["authorization"] = {
            str(key): _sanitize(item, secrets) for key, item in authorization.items()
        }
    return sanitized


def _sanitize_text(value: str, secrets: Sequence[str]) -> str:
    sanitized = redact_text(value)
    for secret in secrets:
        encoded_candidates = {
            secret,
            html.escape(secret, quote=False),
            html.escape(secret, quote=True),
            json.dumps(secret, ensure_ascii=False)[1:-1],
        }
        for candidate in encoded_candidates:
            sanitized = sanitized.replace(candidate, "[REDACTED]")
    return sanitized


def _normalized_secrets(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise UnattendedResultError("secret_values must be a sequence of strings")
    return tuple(value for value in values if isinstance(value, str) and value)


def _parsed_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise UnattendedResultError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise UnattendedResultError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.utcoffset() is None:
        raise UnattendedResultError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as error:
        raise UnattendedResultError(
            f"cannot write terminal output {path.name}"
        ) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _validate(value: object) -> None:
    errors = sorted(
        _validator().iter_errors(cast(Any, value)),
        key=lambda error: tuple(error.absolute_path),
    )
    if errors:
        first = errors[0]
        field = ".".join(str(part) for part in first.absolute_path) or "$"
        raise UnattendedResultError(
            f"unattended_run_result_v1 validation failed at {field}: {first.message}"
        )


@cache
def _validator() -> Draft202012Validator:
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UnattendedResultError("cannot load unattended result schema") from error
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())
