from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.report_renderer import (
    ReportRenderingError,
    main,
    render_report,
    write_report,
)

sys.path.remove(str(PLUGIN_ROOT))

REFERENCE_CASES = json.loads(
    (
        PLUGIN_ROOT
        / "tests"
        / "pipeline-guard"
        / "fixtures"
        / "dbt"
        / "reference_cases.json"
    ).read_text(encoding="utf-8")
)


def _inputs(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build controlled report inputs, without evaluating model or gate correctness."""
    family = case["expected_failure_family"]
    diagnosis = {
        "schema_version": "1.0",
        "incident_id": case["incident"]["incident_id"],
        "failure_family": family,
        "confidence": "LOW" if case["blind"] else "HIGH",
        "cause_status": "UNKNOWN" if case["blind"] else "CONFIRMED",
        "cause": case["incident"]["error_summary"],
        "evidence_citations": (
            []
            if case["blind"]
            else [
                {"evidence_id": case["case_id"], "claim": "The reference run failed."}
            ]
        ),
    }
    decision = {
        "schema_version": "1.0",
        "incident_id": diagnosis["incident_id"],
        "decision": (
            "ESCALATE"
            if case["blind"]
            else "ELIGIBLE_FOR_REMEDIATION"
            if family == "CODE"
            else "REPORT_ONLY"
        ),
        "policy_version": "1.0",
        "rule_id": "report-fixture",
        "reason": "Controlled decision supplied by the host.",
        "signals_used": [],
        "limitations": [],
    }
    _bind_hash(diagnosis, decision)
    return diagnosis, decision


def _bind_hash(diagnosis: dict[str, Any], decision: dict[str, Any]) -> None:
    canonical = json.dumps(
        diagnosis, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    decision["diagnosis_hash"] = "sha256:" + hashlib.sha256(canonical).hexdigest()


@pytest.mark.parametrize("case", REFERENCE_CASES, ids=lambda case: case["case_id"])
def test_both_reports_preserve_cause_evidence_and_decision_for_reference_cases(
    case: dict[str, Any], tmp_path: Path
) -> None:
    diagnosis, decision = _inputs(case)
    before = deepcopy((diagnosis, decision))

    markdown_path, json_path = write_report(diagnosis, decision, output_dir=tmp_path)

    assert markdown_path.name == "report.md"
    assert json_path.name == "report.json"
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report == {"diagnosis": diagnosis, "eligibility_decision": decision}
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "## Cause" in markdown
    assert "## Evidence" in markdown
    assert "## Eligibility decision" in markdown
    assert diagnosis["incident_id"] in markdown
    assert diagnosis["cause"] in markdown
    assert decision["decision"] in markdown
    for citation in diagnosis["evidence_citations"]:
        assert citation["evidence_id"] in markdown
        assert citation["claim"] in markdown
    if case["blind"]:
        assert "No evidence citations supplied." in markdown
    for channel_markup in (
        "<@",
        "<!channel>",
        "<at>",
        "<http",
        "Block Kit",
        "AdaptiveCard",
    ):
        assert channel_markup not in markdown
        assert channel_markup not in json_path.read_text(encoding="utf-8")
    assert (diagnosis, decision) == before


def test_optional_details_and_unicode_survive_in_both_formats() -> None:
    diagnosis, decision = _inputs(REFERENCE_CASES[0])
    diagnosis.update(
        {
            "impact_summary": "Atualização interrompida",
            "affected_resources": ["orders.sql"],
            "recommended_action": "Verificar a transformação",
            "limitations": ["Consulta indisponível"],
            "adapter_details": {"dbt": {"status": "error"}},
        }
    )
    decision.update(
        {"signals_used": ["workspace_clean"], "limitations": ["Review required"]}
    )
    _bind_hash(diagnosis, decision)

    markdown, report = render_report(diagnosis, decision)

    for text in (
        "Atualização interrompida",
        "orders.sql",
        "Verificar a transformação",
        "Consulta indisponível",
        "workspace\\_clean",
        "Review required",
        '"dbt"',
    ):
        assert text in markdown
    assert report["diagnosis"] == diagnosis
    assert report["eligibility_decision"] == decision
    report["diagnosis"]["adapter_details"]["dbt"]["status"] = "changed"
    assert diagnosis["adapter_details"]["dbt"]["status"] == "error"


def test_untrusted_text_cannot_inject_markdown_html_or_channel_mentions() -> None:
    diagnosis, decision = _inputs(REFERENCE_CASES[0])
    diagnosis["cause"] = (
        "<@U123> <at>Team</at> <script>alert(1)</script>\n# Fake heading [go](url)"
    )
    _bind_hash(diagnosis, decision)

    markdown, report = render_report(diagnosis, decision)

    assert "<@U123>" not in markdown
    assert "<script>" not in markdown
    assert "\n# Fake heading" not in markdown
    assert "[go](url)" not in markdown
    assert "&lt;@U123&gt;" in markdown
    assert report["diagnosis"]["cause"] == diagnosis["cause"]


@pytest.mark.parametrize(
    "invalid", ["diagnosis-schema", "decision-schema", "incident", "hash"]
)
def test_invalid_or_mismatched_inputs_produce_no_files(
    invalid: str, tmp_path: Path
) -> None:
    diagnosis, decision = _inputs(REFERENCE_CASES[0])
    if invalid == "diagnosis-schema":
        diagnosis["approved"] = True
    elif invalid == "decision-schema":
        decision["decision"] = "APPROVED"
    elif invalid == "incident":
        decision["incident_id"] = "another-incident"
    else:
        diagnosis["cause"] = "A changed diagnosis after the gate ran."
    output_dir = tmp_path / "reports"

    with pytest.raises(ReportRenderingError):
        write_report(diagnosis, decision, output_dir=output_dir)

    assert not output_dir.exists()


def test_rendering_is_deterministic_and_ignores_key_order() -> None:
    diagnosis, decision = _inputs(REFERENCE_CASES[0])

    assert render_report(diagnosis, decision) == render_report(
        dict(reversed(list(diagnosis.items()))), dict(reversed(list(decision.items())))
    )


def test_cli_writes_reports_and_rejects_invalid_json(
    tmp_path: Path, capsys: Any
) -> None:
    diagnosis, decision = _inputs(REFERENCE_CASES[0])
    diagnosis_path = tmp_path / "diagnosis.json"
    decision_path = tmp_path / "decision.json"
    diagnosis_path.write_text(json.dumps(diagnosis), encoding="utf-8")
    decision_path.write_text(json.dumps(decision), encoding="utf-8")
    args = [
        str(diagnosis_path),
        str(decision_path),
        "--output-dir",
        str(tmp_path / "reports"),
    ]

    assert main(args) == 0
    assert (tmp_path / "reports" / "report.md").is_file()
    assert (tmp_path / "reports" / "report.json").is_file()
    decision_path.write_text("invalid JSON", encoding="utf-8")
    assert main(args) == 2
    assert "Cannot render report:" in capsys.readouterr().err
