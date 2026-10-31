"""Bounded filtering and deterministic indexing for large log evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

_CRITICAL_PATTERN = re.compile(
    r"\b(ERROR|CRITICAL|EXCEPTION|FATAL|Traceback|FAILED)\b",
    re.IGNORECASE,
)
_WARNING_PATTERN = re.compile(r"\bWARN(ING)?\b", re.IGNORECASE)


class LargeLogProcessingError(ValueError):
    """Raised when a large log cannot be preserved and filtered safely."""


class LogArtifactStore(Protocol):
    """Artifact store operations required by the large-log processor."""

    def put(self, run_id: str, reference: str, artifact: bytes) -> bool: ...

    def get(self, run_id: str, reference: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class LargeLogPolicy:
    """Deterministic limits applied before log evidence reaches diagnosis."""

    max_input_bytes: int = 10_000_000
    max_inline_bytes: int = 40_000
    chunk_lines: int = 80
    overlap_lines: int = 10
    max_critical_lines: int = 50
    max_warning_lines: int = 20
    warning_critical_threshold: int = 20
    tail_lines: int = 10
    max_line_chars: int = 2_000

    def __post_init__(self) -> None:
        positive_fields = (
            "max_input_bytes",
            "max_inline_bytes",
            "chunk_lines",
            "max_critical_lines",
            "max_warning_lines",
            "warning_critical_threshold",
            "tail_lines",
            "max_line_chars",
        )
        for field_name in positive_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")

        if self.max_inline_bytes > self.max_input_bytes:
            raise ValueError("max_inline_bytes cannot exceed max_input_bytes")

        if (
            isinstance(self.overlap_lines, bool)
            or not isinstance(self.overlap_lines, int)
            or self.overlap_lines < 0
            or self.overlap_lines >= self.chunk_lines
        ):
            raise ValueError("overlap_lines must be an integer from zero to chunk_lines - 1")


@dataclass(frozen=True, slots=True)
class ProcessedLargeLog:
    """Filtered log evidence plus its immutable original reference."""

    content: str
    reference: str


DEFAULT_LARGE_LOG_POLICY = LargeLogPolicy()


def filter_and_index_large_log(
    content: bytes | str | Mapping[str, object],
    *,
    incident_id: str,
    origin: str,
    reference: str | None,
    artifact_store: LogArtifactStore | None,
    policy: LargeLogPolicy = DEFAULT_LARGE_LOG_POLICY,
) -> ProcessedLargeLog | None:
    """Preserve and summarize a large log, returning ``None`` for inline-sized content."""
    raw_content = _content_as_bytes(content, max_input_bytes=policy.max_input_bytes)
    if len(raw_content) <= policy.max_inline_bytes:
        return None
    if artifact_store is None:
        raise LargeLogProcessingError(
            "large log evidence requires an ArtifactStore; refusing to return the full log"
        )

    original_digest = hashlib.sha256(raw_content).hexdigest()
    artifact_run_id = _artifact_run_id(incident_id)
    original_reference = reference or _generated_reference(origin, original_digest)
    _put_immutable(
        artifact_store,
        artifact_run_id,
        original_reference,
        raw_content,
    )

    text = raw_content.decode("utf-8", errors="replace")
    lines = text.splitlines()
    chunks = _build_chunk_index(lines, policy)
    index_document = {
        "schema_version": "1.0",
        "source": {
            "artifact_run_id": artifact_run_id,
            "reference": original_reference,
            "hash": f"sha256:{original_digest}",
            "size_bytes": len(raw_content),
            "total_lines": len(lines),
        },
        "chunk_lines": policy.chunk_lines,
        "overlap_lines": policy.overlap_lines,
        "chunks": chunks,
    }
    index_bytes = _canonical_json(index_document).encode("utf-8")
    index_digest = hashlib.sha256(index_bytes).hexdigest()
    index_reference = f"{original_reference}.index-{index_digest[:16]}.json"
    _put_immutable(
        artifact_store,
        artifact_run_id,
        index_reference,
        index_bytes,
    )

    critical_count, warning_count, critical, warnings = _classify_lines(lines, policy)
    selected = critical[: policy.max_critical_lines]
    if critical_count < policy.warning_critical_threshold:
        selected.extend(warnings[: policy.max_warning_lines])
    if not critical and not warnings:
        start = max(0, len(lines) - policy.tail_lines)
        selected = [
            (line_number, "tail", lines[line_number - 1])
            for line_number in range(start + 1, len(lines) + 1)
        ]

    selected.sort(key=lambda entry: entry[0])
    entries = [
        _bounded_entry(line_number, classification, line, policy.max_line_chars)
        for line_number, classification, line in selected
    ]
    summary = {
        "schema_version": "1.0",
        "filtered": True,
        "original": index_document["source"],
        "index": {
            "artifact_run_id": artifact_run_id,
            "reference": index_reference,
            "hash": f"sha256:{index_digest}",
            "chunk_count": len(chunks),
        },
        "summary": {
            "critical_count": critical_count,
            "warning_count": warning_count,
            "selected_count": len(entries),
            "truncated": len(entries) < len(lines),
        },
        "entries": entries,
    }
    return ProcessedLargeLog(
        content=_canonical_json(summary),
        reference=original_reference,
    )


def _artifact_run_id(incident_id: str) -> str:
    digest = hashlib.sha256(incident_id.encode("utf-8")).hexdigest()
    return f"incident-{digest[:24]}"


def _generated_reference(origin: str, content_digest: str) -> str:
    origin_digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()
    return f"logs/{origin_digest[:12]}/{content_digest}.log"


def _put_immutable(
    artifact_store: LogArtifactStore,
    run_id: str,
    reference: str,
    artifact: bytes,
) -> None:
    created = artifact_store.put(run_id, reference, artifact)
    if not created and artifact_store.get(run_id, reference) != artifact:
        raise LargeLogProcessingError(
            f"artifact reference collision for {reference!r}; existing content was preserved"
        )


def _content_as_bytes(
    content: bytes | str | Mapping[str, object],
    *,
    max_input_bytes: int,
) -> bytes:
    if isinstance(content, bytes):
        raw_content = content
    elif isinstance(content, str):
        if len(content) > max_input_bytes:
            raise _oversized_log_error(len(content), max_input_bytes, lower_bound=True)
        raw_content = content.encode("utf-8")
    else:
        try:
            raw_content = _canonical_json(content).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise LargeLogProcessingError("large log mappings must be JSON serializable") from error

    if len(raw_content) > max_input_bytes:
        raise _oversized_log_error(len(raw_content), max_input_bytes)

    return raw_content


def _oversized_log_error(
    observed_bytes: int,
    max_input_bytes: int,
    *,
    lower_bound: bool = False,
) -> LargeLogProcessingError:
    qualifier = "at least " if lower_bound else ""
    return LargeLogProcessingError(
        f"log input is {qualifier}{observed_bytes} bytes and exceeds max_input_bytes="
        f"{max_input_bytes}; provide a bounded excerpt or external reference"
    )


def _build_chunk_index(lines: list[str], policy: LargeLogPolicy) -> list[dict[str, object]]:
    chunks: list[dict[str, object]] = []
    step = policy.chunk_lines - policy.overlap_lines
    for start in range(0, len(lines), step):
        end = min(start + policy.chunk_lines, len(lines))
        chunk_text = "\n".join(lines[start:end])
        chunks.append(
            {
                "chunk_id": len(chunks),
                "start_line": start + 1,
                "end_line": end,
                "hash": f"sha256:{hashlib.sha256(chunk_text.encode('utf-8')).hexdigest()}",
            }
        )
        if end == len(lines):
            break
    return chunks


def _classify_lines(
    lines: list[str],
    policy: LargeLogPolicy,
) -> tuple[
    int,
    int,
    list[tuple[int, str, str]],
    list[tuple[int, str, str]],
]:
    critical_count = 0
    warning_count = 0
    critical: list[tuple[int, str, str]] = []
    warnings: list[tuple[int, str, str]] = []
    for line_number, line in enumerate(lines, start=1):
        if _CRITICAL_PATTERN.search(line):
            critical_count += 1
            if len(critical) < policy.max_critical_lines:
                critical.append((line_number, "critical", line))
        elif _WARNING_PATTERN.search(line):
            warning_count += 1
            if len(warnings) < policy.max_warning_lines:
                warnings.append((line_number, "warning", line))
    return critical_count, warning_count, critical, warnings


def _bounded_entry(
    line_number: int,
    classification: str,
    line: str,
    max_line_chars: int,
) -> dict[str, object]:
    truncated = len(line) > max_line_chars
    return {
        "line_number": line_number,
        "classification": classification,
        "text": line[:max_line_chars],
        "line_truncated": truncated,
    }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
