"""Live execution tests for the Pipeline Guard diagnosis agent."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.api

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
DBT_FIXTURE_DIR = PLUGIN_ROOT / "tests" / "pipeline-guard" / "fixtures" / "dbt"
AGENT_PATH = PLUGIN_ROOT / "agents" / "diagnosis-agent.md"
DIAGNOSIS_SCHEMA_PATH = PLUGIN_ROOT / "contracts" / "schema" / "pipeline_diagnosis_v1.schema.json"

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

DbtAdapter = import_module("adapters.dbt").DbtAdapter
validate_diagnosis = import_module("scripts.diagnosis_validator").validate_diagnosis
validate_incident_input = import_module("scripts.incident_validator").validate_incident_input

DBT_DETAIL_KEYS = {
    "adapter_response",
    "compiled_code",
    "failures",
    "lineage",
    "name",
    "original_file_path",
    "relation_name",
    "resource_type",
    "status",
    "unique_id",
}


def _load_agent_definition() -> dict[str, dict[str, object]]:
    document = AGENT_PATH.read_text(encoding="utf-8")
    parts = document.split("---", maxsplit=2)
    assert len(parts) == 3, "diagnosis-agent.md must contain YAML frontmatter"

    metadata = yaml.safe_load(parts[1])
    assert isinstance(metadata, dict)

    return {
        metadata["name"]: {
            "description": metadata["description"],
            "prompt": parts[2].strip(),
            "tools": metadata["tools"],
            "model": metadata["model"],
            "maxTurns": 3,
        }
    }


def _artifact_reference(filename: str, artifact_type: str) -> dict[str, str]:
    artifact_path = DBT_FIXTURE_DIR / filename
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    return {
        "type": artifact_type,
        "uri": f"artifact://dbt/{filename}",
        "hash": f"sha256:{digest}",
    }


def _dbt_incident() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "incident_id": "INC-DBT-LIVE-001",
        "source": "ci",
        "environment": "test",
        "pipeline_id": "daily-dbt-build",
        "run_id": "run-live-001",
        "failed_step": "dbt build",
        "occurred_at": "2026-10-02T12:00:04Z",
        "error_summary": "A model failed and a data test returned three rows.",
        "project_profile": "dbt",
        "artifacts": [
            _artifact_reference("run_results.json", "dbt_run_results"),
            _artifact_reference("manifest.json", "dbt_manifest"),
        ],
    }


def _workspace_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _claude_executable() -> str | None:
    """Resolve the native Claude binary behind the Windows npm shim."""
    if os.name == "nt":
        if native_executable := shutil.which("claude.exe"):
            return native_executable
        if command_shim := shutil.which("claude.cmd"):
            npm_root = Path(command_shim).parent
            native_executable = (
                npm_root / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            )
            if native_executable.is_file():
                return str(native_executable)

    return shutil.which("claude")


def _stream_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            pytest.fail(f"Claude stream line {line_number} is not JSON: {error}")
        assert isinstance(event, dict)
        events.append(event)

    assert events, "Claude returned no stream events"
    return events


def _diagnosis_from_result(event: Mapping[str, object]) -> dict[str, Any]:
    """Extract structured output, with a strict JSON fallback for agent sessions."""
    structured_output = event.get("structured_output")
    if structured_output is not None:
        assert isinstance(structured_output, dict), "Claude structured_output must be a JSON object"
        return structured_output

    raw_result = event.get("result")
    assert isinstance(raw_result, str), (
        "Claude result contained neither structured_output nor JSON text"
    )
    assert raw_result.strip(), "Claude result fallback was empty"
    try:
        diagnosis = json.loads(raw_result)
    except json.JSONDecodeError as error:
        pytest.fail(
            "Claude result fallback must contain only a valid JSON object: "
            f"{error.msg} at line {error.lineno}, column {error.colno}"
        )
    assert isinstance(diagnosis, dict), "Claude result fallback must be a JSON object"
    return diagnosis


def _run_live_agent(
    *, workspace: Path, diagnosis_input: Mapping[str, object], agents_path: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str], dict[str, str]]:
    """Run the diagnosis agent with one inline incident and return its output and snapshots."""
    prompt = (
        "Diagnose this incident from the supplied evidence. The complete input is inline, so "
        "do not read or discover any files. Return exactly one pipeline_diagnosis_v1 object.\n"
        + json.dumps(diagnosis_input, ensure_ascii=False)
    )
    diagnosis_schema = json.loads(DIAGNOSIS_SCHEMA_PATH.read_text(encoding="utf-8"))
    cli_output_schema = {
        key: value for key, value in diagnosis_schema.items() if key not in {"$id", "$schema"}
    }
    before = _workspace_snapshot(workspace)
    claude_executable = _claude_executable()
    assert claude_executable is not None, "Claude Code CLI is required for API tests"

    environment = os.environ.copy()
    environment.update(
        {
            "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
            "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "NO_COLOR": "1",
        }
    )
    completed = subprocess.run(
        [
            claude_executable,
            "--print",
            "--agents",
            str(agents_path),
            "--agent",
            "diagnosis-agent",
            "--restricted",
            "--settings",
            "{}",
            "--tools",
            "Read",
            "--disallowedTools",
            "mcp__*",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--max-turns",
            "3",
            "--json-schema",
            json.dumps(cli_output_schema, separators=(",", ":")),
            prompt,
        ],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=240,
        check=False,
    )
    events = _stream_events(completed.stdout) if completed.stdout.strip() else []
    result_events = [event for event in events if event.get("type") == "result"]
    if completed.returncode != 0:
        result_message = next(
            (
                message
                for event in reversed(result_events)
                if isinstance((message := event.get("result")), str) and message
            ),
            "",
        )
        failure_detail = result_message or completed.stderr.strip() or "unknown error"
        if "Not logged in" in failure_detail:
            pytest.fail(
                "Claude Code is not authenticated. Run `claude auth login` or export "
                "ANTHROPIC_AUTH_TOKEN before executing API tests."
            )
        pytest.fail(f"Claude execution failed with exit {completed.returncode}: {failure_detail}")

    assert len(result_events) == 1
    return _diagnosis_from_result(result_events[0]), events, before, _workspace_snapshot(workspace)


def _tool_names(value: object) -> set[str]:
    if isinstance(value, Mapping):
        names = (
            {name}
            if value.get("type") == "tool_use" and isinstance((name := value.get("name")), str)
            else set()
        )
        return names | {name for nested in value.values() for name in _tool_names(nested)}
    if isinstance(value, list):
        return {name for nested in value for name in _tool_names(nested)}
    return set()


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return set(value) | {
            nested_key
            for nested_value in value.values()
            for nested_key in _nested_keys(nested_value)
        }
    if isinstance(value, list):
        return {nested_key for item in value for nested_key in _nested_keys(item)}
    return set()


def _dbt_specific_values(evidences: Iterable[Mapping[str, object]]) -> set[str]:
    fields = (
        "compiled_code",
        "name",
        "original_file_path",
        "relation_name",
        "resource_type",
        "unique_id",
    )
    payloads = [json.loads(str(evidence["content"])) for evidence in evidences]
    return {
        value
        for payload in payloads
        for field in fields
        if isinstance((value := payload.get(field)), str) and value
    }


@pytest.fixture(scope="module")
def live_dbt_agent_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    claude_executable = _claude_executable()
    assert claude_executable is not None, "Claude Code CLI is required for API tests"

    workspace = tmp_path_factory.mktemp("diagnosis-agent-live")
    agents_path = workspace / "agents.json"
    agents_path.write_text(
        json.dumps(_load_agent_definition(), ensure_ascii=False),
        encoding="utf-8",
    )

    incident = _dbt_incident()
    adapter_output = DbtAdapter(artifacts_dir=DBT_FIXTURE_DIR).run(incident)
    diagnosis_input = {
        "incident": {"incident_id": incident["incident_id"]},
        "evidences": adapter_output["evidences"],
        "adapter_details": adapter_output["adapter_details"],
    }
    prompt = (
        "Diagnose this incident from the supplied evidence. The complete input is inline, so "
        "do not read or discover any files. Return exactly one pipeline_diagnosis_v1 object.\n"
        + json.dumps(diagnosis_input, ensure_ascii=False)
    )
    diagnosis_schema = json.loads(DIAGNOSIS_SCHEMA_PATH.read_text(encoding="utf-8"))
    cli_output_schema = {
        key: value for key, value in diagnosis_schema.items() if key not in {"$id", "$schema"}
    }
    before = _workspace_snapshot(workspace)

    environment = os.environ.copy()
    environment.update(
        {
            "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
            "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "NO_COLOR": "1",
        }
    )
    completed = subprocess.run(
        [
            claude_executable,
            "--print",
            "--agents",
            str(agents_path),
            "--agent",
            "diagnosis-agent",
            "--restricted",
            "--settings",
            "{}",
            "--tools",
            "Read",
            "--disallowedTools",
            "mcp__*",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--max-turns",
            "3",
            "--json-schema",
            json.dumps(cli_output_schema, separators=(",", ":")),
            prompt,
        ],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=240,
        check=False,
    )
    events = _stream_events(completed.stdout) if completed.stdout.strip() else []
    result_events = [event for event in events if event.get("type") == "result"]
    if completed.returncode != 0:
        result_message = next(
            (
                message
                for event in reversed(result_events)
                if isinstance((message := event.get("result")), str) and message
            ),
            "",
        )
        failure_detail = result_message or completed.stderr.strip() or "unknown error"
        if "Not logged in" in failure_detail:
            pytest.fail(
                "Claude Code is not authenticated. Run `claude auth login` or export "
                "ANTHROPIC_AUTH_TOKEN before executing API tests."
            )
        pytest.fail(f"Claude execution failed with exit {completed.returncode}: {failure_detail}")

    assert len(result_events) == 1
    diagnosis = _diagnosis_from_result(result_events[0])

    return {
        "adapter_output": adapter_output,
        "after": _workspace_snapshot(workspace),
        "before": before,
        "diagnosis": diagnosis,
        "events": events,
        "incident": incident,
    }


def test_live_agent_produces_valid_diagnosis_without_writes(
    live_dbt_agent_run: dict[str, Any],
) -> None:
    diagnosis = live_dbt_agent_run["diagnosis"]
    incident = live_dbt_agent_run["incident"]
    adapter_output = live_dbt_agent_run["adapter_output"]
    schema = json.loads(DIAGNOSIS_SCHEMA_PATH.read_text(encoding="utf-8"))

    Draft202012Validator(schema).validate(diagnosis)
    assert (
        validate_diagnosis(
            diagnosis,
            incident_id=incident["incident_id"],
            evidences=adapter_output["evidences"],
        )
        == diagnosis
    )

    called_tools = _tool_names(live_dbt_agent_run["events"])
    assert called_tools <= {"Read"}
    assert live_dbt_agent_run["after"] == live_dbt_agent_run["before"]


def test_live_dbt_diagnosis_keeps_specific_details_out_of_core(
    live_dbt_agent_run: dict[str, Any],
) -> None:
    diagnosis = live_dbt_agent_run["diagnosis"]
    adapter_output = live_dbt_agent_run["adapter_output"]

    assert "dbt" in diagnosis["adapter_details"]
    dbt_details = diagnosis["adapter_details"]["dbt"]
    assert _nested_keys(dbt_details) >= DBT_DETAIL_KEYS

    diagnosis_core = {
        field: value for field, value in diagnosis.items() if field != "adapter_details"
    }
    assert DBT_DETAIL_KEYS.isdisjoint(_nested_keys(diagnosis_core))

    serialized_core = json.dumps(diagnosis_core, ensure_ascii=False, sort_keys=True)
    for detail_value in _dbt_specific_values(adapter_output["evidences"]):
        assert json.dumps(detail_value, ensure_ascii=False) not in serialized_core


def _load_reference_cases() -> list[dict[str, Any]]:
    cases = json.loads((DBT_FIXTURE_DIR / "reference_cases.json").read_text(encoding="utf-8"))
    assert isinstance(cases, list)
    assert len(cases) == 12
    assert len({case["case_id"] for case in cases}) == 12
    assert sum(case["blind"] for case in cases) == 4
    return cases


@pytest.fixture(scope="module")
def live_dbt_reference_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> list[dict[str, Any]]:
    """Run all reference incidents through validation, dbt adaptation, and diagnosis."""
    workspace_root = tmp_path_factory.mktemp("diagnosis-agent-reference-cases")
    agents_path = workspace_root / "agents.json"
    agents_path.write_text(
        json.dumps(_load_agent_definition(), ensure_ascii=False),
        encoding="utf-8",
    )
    runs = []

    for case in _load_reference_cases():
        case_dir = workspace_root / case["case_id"]
        case_dir.mkdir()
        artifact_references = []
        for filename, artifact_type in (
            ("run_results.json", "dbt_run_results"),
            ("manifest.json", "dbt_manifest"),
        ):
            artifact_path = case_dir / filename
            artifact_path.write_text(
                json.dumps(case[filename.removesuffix(".json")], ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            artifact_references.append(
                {
                    "type": artifact_type,
                    "uri": f"artifact://dbt/{case['case_id']}/{filename}",
                    "hash": f"sha256:{digest}",
                }
            )

        incident = {
            "schema_version": "1.0",
            **case["incident"],
            "artifacts": artifact_references,
        }
        validate_incident_input(incident)
        adapter_output = DbtAdapter(artifacts_dir=case_dir).run(incident)
        diagnosis_input = {
            "incident": incident,
            "evidences": adapter_output["evidences"],
            "adapter_details": adapter_output["adapter_details"],
        }
        diagnosis, events, before, after = _run_live_agent(
            workspace=case_dir,
            diagnosis_input=diagnosis_input,
            agents_path=agents_path,
        )
        validated_diagnosis = validate_diagnosis(
            diagnosis,
            incident_id=incident["incident_id"],
            evidences=adapter_output["evidences"],
        )
        runs.append(
            {
                "adapter_output": adapter_output,
                "after": after,
                "before": before,
                "blind": case["blind"],
                "case_id": case["case_id"],
                "diagnosis": validated_diagnosis,
                "events": events,
                "expected_failure_family": case["expected_failure_family"],
                "incident": incident,
            }
        )

    return runs


@pytest.mark.parametrize("case_id", [case["case_id"] for case in _load_reference_cases()])
def test_live_dbt_reference_case_has_valid_diagnosis_and_no_writes(
    case_id: str,
    live_dbt_reference_runs: list[dict[str, Any]],
) -> None:
    run = next(run for run in live_dbt_reference_runs if run["case_id"] == case_id)
    diagnosis = run["diagnosis"]
    schema = json.loads(DIAGNOSIS_SCHEMA_PATH.read_text(encoding="utf-8"))

    Draft202012Validator(schema).validate(diagnosis)
    assert (
        validate_diagnosis(
            diagnosis,
            incident_id=run["incident"]["incident_id"],
            evidences=run["adapter_output"]["evidences"],
        )
        == diagnosis
    )
    assert _tool_names(run["events"]) <= {"Read"}
    assert run["after"] == run["before"]


def test_live_dbt_reference_cases_meet_expected_family_threshold(
    live_dbt_reference_runs: list[dict[str, Any]],
) -> None:
    correct_cases = [
        run
        for run in live_dbt_reference_runs
        if run["diagnosis"]["failure_family"] == run["expected_failure_family"]
    ]
    mismatches = [run["case_id"] for run in live_dbt_reference_runs if run not in correct_cases]
    assert len(correct_cases) >= 10, (
        f"Expected at least 10 of 12 reference cases to match; "
        f"matched {len(correct_cases)} of {len(live_dbt_reference_runs)}. "
        f"Mismatches: {mismatches}"
    )


def test_live_dbt_blind_cases_stop_safely(
    live_dbt_reference_runs: list[dict[str, Any]],
) -> None:
    blind_runs = [run for run in live_dbt_reference_runs if run["blind"]]
    assert len(blind_runs) == 4
    for run in blind_runs:
        diagnosis = run["diagnosis"]
        assert diagnosis["failure_family"] == "UNKNOWN" or diagnosis["confidence"] == "LOW", (
            f"Blind case {run['case_id']} did not stop safely: "
            f"{diagnosis['failure_family']}/{diagnosis['confidence']}"
        )
