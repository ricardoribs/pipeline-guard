# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Render channel-neutral reports from a diagnosis and its eligibility decision.

Usage: uv run report_renderer.py diagnosis.json decision.json --output-dir ./reports
The host chooses the output directory and handles any publication.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "contracts" / "schema"


class ReportRenderingError(ValueError):
    """Raised when report inputs are invalid or refer to different diagnoses."""


def render_report(diagnosis: object, eligibility_decision: object) -> tuple[str, dict[str, Any]]:
    """Return Markdown and an equivalent JSON document without side effects."""
    diagnosis_document = _validate(diagnosis, "pipeline_diagnosis_v1")
    decision_document = _validate(eligibility_decision, "eligibility_decision_v1")
    if diagnosis_document["incident_id"] != decision_document["incident_id"]:
        raise ReportRenderingError("diagnosis and eligibility decision incident_id differ")
    canonical = json.dumps(
        diagnosis_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    expected_hash = "sha256:" + hashlib.sha256(canonical).hexdigest()
    if decision_document["diagnosis_hash"] != expected_hash:
        raise ReportRenderingError("eligibility decision diagnosis_hash does not match diagnosis")

    # Copy through JSON so callers cannot mutate the report through nested input objects.
    report = json.loads(
        json.dumps(
            {"diagnosis": diagnosis_document, "eligibility_decision": decision_document},
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    lines = [
        "# Pipeline diagnosis report",
        "",
        f"Incident: {_text(diagnosis_document['incident_id'])}",
        "",
        "## Cause",
        "",
        _text(diagnosis_document["cause"]),
        "",
        f"Failure family: {diagnosis_document['failure_family']}",
        "",
        f"Confidence: {diagnosis_document['confidence']}",
        "",
        f"Cause status: {diagnosis_document['cause_status']}",
    ]
    if "impact_summary" in diagnosis_document:
        lines.extend(["", "## Impact", "", _text(diagnosis_document["impact_summary"])])
    _items(lines, "Affected resources", diagnosis_document.get("affected_resources", []))
    lines.extend(["", "## Evidence", ""])
    citations = diagnosis_document["evidence_citations"]
    lines.extend(
        [f"- {_text(item['evidence_id'])}: {_text(item['claim'])}" for item in citations]
        or ["No evidence citations supplied."]
    )
    lines.extend(
        [
            "",
            "## Eligibility decision",
            "",
            decision_document["decision"],
            "",
            f"Policy version: {_text(decision_document['policy_version'])}",
            "",
            f"Rule: {_text(decision_document['rule_id'])}",
            "",
            f"Diagnosis hash: {_text(decision_document['diagnosis_hash'])}",
        ]
    )
    if "reason" in decision_document:
        lines.extend(["", f"Reason: {_text(decision_document['reason'])}"])
    _items(lines, "Independent signals used", decision_document.get("signals_used", []))
    if "recommended_action" in diagnosis_document:
        lines.extend(
            ["", "## Recommended action", "", _text(diagnosis_document["recommended_action"])]
        )
    _items(lines, "Diagnosis limitations", diagnosis_document.get("limitations", []))
    _items(lines, "Eligibility limitations", decision_document.get("limitations", []))
    if "adapter_details" in diagnosis_document:
        details = json.dumps(
            diagnosis_document["adapter_details"], ensure_ascii=False, sort_keys=True, indent=2
        )
        # An indented code block keeps arbitrary adapter content inert in Markdown.
        lines.extend(["", "## Adapter details", ""])
        lines.extend("    " + line for line in details.splitlines())
    return "\n".join(lines) + "\n", report


def write_report(
    diagnosis: object, eligibility_decision: object, *, output_dir: Path
) -> tuple[Path, Path]:
    """Validate both inputs, then write report.md and report.json to a host-selected directory."""
    markdown, report = render_report(diagnosis, eligibility_decision)
    json_report = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "report.md"
    json_path = output_dir / "report.json"
    markdown_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json_report, encoding="utf-8")
    return markdown_path, json_path


def _validate(document: object, contract: str) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise ReportRenderingError(f"{contract} must be an object")
    schema_path = _SCHEMA_DIR / f"{contract}.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReportRenderingError(f"cannot load schema: {schema_path}") from error
    validator = Draft202012Validator(schema)
    error = next(validator.iter_errors(document), None)
    if error is not None:
        field = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ReportRenderingError(f"invalid {contract} at {field}: {error.message}")
    return dict(document)


def _text(value: str) -> str:
    """Display untrusted text without interpreting HTML, mentions or Markdown links."""
    escaped = html.escape(" ".join(value.splitlines()), quote=True)
    for character in "\\`*_{}[]()#!|":
        escaped = escaped.replace(character, "\\" + character)
    return escaped


def _items(lines: list[str], title: str, items: Sequence[str]) -> None:
    if items:
        lines.extend(["", f"## {title}", ""])
        lines.extend(f"- {_text(item)}" for item in items)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a channel-neutral pipeline report.")
    parser.add_argument("diagnosis", type=Path)
    parser.add_argument("eligibility_decision", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        diagnosis = json.loads(args.diagnosis.read_text(encoding="utf-8"))
        decision = json.loads(args.eligibility_decision.read_text(encoding="utf-8"))
        write_report(diagnosis, decision, output_dir=args.output_dir)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Cannot render report: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
