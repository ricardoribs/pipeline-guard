from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
DIAGNOSE_CLI = PLUGIN_ROOT / "scripts/diagnose_cli.py"
DBT_PRESET = PLUGIN_ROOT / "config/presets/dbt.yaml"
HOOKS_CONFIG = PLUGIN_ROOT / "hooks/hooks.json"
PLUGIN_MANIFEST = PLUGIN_ROOT / ".claude-plugin/plugin.json"
PLUGIN_README = PLUGIN_ROOT / "README.md"
DIAGNOSE_SKILL = PLUGIN_ROOT / "skills/diagnose/SKILL.md"
TRIGGER_CASES = PLUGIN_ROOT / "tests/skills/test_cases/diagnose.yaml"
UV_EXECUTABLE = shutil.which("uv")

HOOK_FILTERS = {
    "Agent": "Agent(pipeline-guard:diagnosis-agent)",
    "Bash": "Bash(uv run *diagnose_cli.py*)",
    "PowerShell": "PowerShell(uv run *diagnose_cli.py*)",
}

HOOK_COMMAND = {
    "type": "command",
    "command": "uv",
    "args": ["run", "${CLAUDE_PLUGIN_ROOT}/hooks/redact.py", "--hook"],
    "timeout": 30,
    "onFailure": "block",
}


def _require_uv(executable: str | None = UV_EXECUTABLE) -> str:
    if executable is not None:
        return executable
    ci_enabled = bool(os.environ.get("BITBUCKET_BUILD_NUMBER")) or os.environ.get(
        "CI", ""
    ).casefold() not in {"", "0", "false", "no", "off"}
    if ci_enabled:
        pytest.fail("uv is required to exercise the diagnosis redaction boundary in CI")
    pytest.skip("uv is required to exercise the diagnosis redaction boundary")


def _registered_hooks() -> dict[str, dict]:
    configuration = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
    registrations = configuration["hooks"]["PreToolUse"]
    matching = [
        registration
        for registration in registrations
        if registration["matcher"] == "Agent|Bash|PowerShell"
    ]
    assert len(matching) == 1
    registration = matching[0]
    hooks = registration["hooks"]
    assert len(hooks) == len(HOOK_FILTERS)

    hooks_by_filter = {hook["if"]: hook for hook in hooks}
    assert set(hooks_by_filter) == set(HOOK_FILTERS.values())
    for hook_filter, hook in hooks_by_filter.items():
        assert hook == {"if": hook_filter, **HOOK_COMMAND}
    return hooks_by_filter


def _registered_prompt_hook() -> dict:
    configuration = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
    registrations = configuration["hooks"]["UserPromptSubmit"]
    assert len(registrations) == 1
    registration = registrations[0]
    assert "matcher" not in registration
    hooks = registration["hooks"]
    assert hooks == [HOOK_COMMAND]
    return hooks[0]


def _run_registered_hook(event: dict) -> subprocess.CompletedProcess[str]:
    uv_executable = _require_uv()

    if event.get("hook_event_name") == "UserPromptSubmit":
        hook = _registered_prompt_hook()
    else:
        hook_filter = HOOK_FILTERS[event["tool_name"]]
        hook = _registered_hooks()[hook_filter]
    environment = os.environ.copy()
    environment["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    command = [
        uv_executable,
        *[
            argument.replace("${CLAUDE_PLUGIN_ROOT}", PLUGIN_ROOT.as_posix())
            for argument in hook["args"]
        ],
    ]
    return subprocess.run(
        command,
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=False,
        cwd=PLUGIN_ROOT,
        env=environment,
        timeout=hook["timeout"],
    )


def _documented_skill_command() -> str:
    skill = DIAGNOSE_SKILL.read_text(encoding="utf-8")
    command_block = re.search(r"```bash\n(?P<command>.*?)\n```", skill, re.DOTALL)
    assert command_block is not None
    return command_block.group("command").replace("\\\n", " ")


def test_registered_hook_sanitizes_diagnosis_agent_input() -> None:
    email = "data.owner@example.com"
    token = "DATABRICKS_TOKEN=placeholder"
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "pipeline-guard:diagnosis-agent",
            "description": "Diagnose supplied evidence",
            "prompt": f"Evidence owner: {email}\nDatabricks PAT: {token}",
        },
    }

    completed = _run_registered_hook(event)

    assert completed.returncode == 0
    assert email not in completed.stdout
    assert token not in completed.stdout
    response = json.loads(completed.stdout)
    output = response["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in output
    assert output["updatedInput"]["subagent_type"].endswith(":diagnosis-agent")
    assert "[REDACTED_EMAIL]" in output["updatedInput"]["prompt"]
    assert "[REDACTED_SECRET]" in output["updatedInput"]["prompt"]


@pytest.mark.parametrize(
    ("prompt", "secret"),
    [
        (
            '$diagnose {"error_summary":"Cookie: sessionid=literal-session-cookie"}',
            "literal-session-cookie",
        ),
        (
            "Diagnostique a falha do pipeline. "
            "SharedAccessSignature=sv=1&sig=literal-sas-signature&se=2",
            "literal-sas-signature",
        ),
        (
            "Invoke pipeline-guard:diagnosis-agent with "
            "Cookie: sessionid=literal-agent-session",
            "literal-agent-session",
        ),
        (
            "Analise a execução do pipeline com senha=literal-senha",  # codespell:ignore analise
            "literal-senha",
        ),
    ],
)
def test_registered_prompt_hook_blocks_sensitive_diagnosis_before_model(
    prompt: str, secret: str
) -> None:
    event = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }

    completed = _run_registered_hook(event)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert secret not in completed.stdout
    response = json.loads(completed.stdout)
    assert response["decision"] == "block"
    assert "incident contents" in response["reason"]
    assert response["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit",
        "suppressOriginalPrompt": True,
    }


def test_registered_prompt_hook_allows_safe_diagnosis_path() -> None:
    completed = _run_registered_hook(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "$diagnose incidents/dbt-run-42.json",
        }
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_registered_prompt_hook_does_not_intercept_unrelated_sensitive_prompt() -> None:
    completed = _run_registered_hook(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Format this example: Cookie: sessionid=literal-session-cookie",
        }
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


@pytest.mark.parametrize("tool_name", ["Bash", "PowerShell"])
def test_registered_hook_sanitizes_headless_diagnosis_invocation(tool_name: str) -> None:
    email = "pipeline.owner@example.com"
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {
            "command": (
                f'uv run "/plugin path/scripts/diagnose_cli.py" '
                f"--config config.yaml --note '{email}'"
            )
        },
    }

    completed = _run_registered_hook(event)

    assert completed.returncode == 0
    assert email not in completed.stdout
    updated = json.loads(completed.stdout)["hookSpecificOutput"]["updatedInput"]
    assert "[REDACTED_EMAIL]" in updated["command"]
    assert "diagnose_cli.py" in updated["command"]


def test_registered_hooks_are_scoped_to_diagnosis_calls() -> None:
    hooks = _registered_hooks()

    assert "Task" not in HOOK_FILTERS
    assert all("diagnos" in hook_filter for hook_filter in hooks)


@pytest.mark.parametrize("tool_name", ["Bash", "PowerShell"])
def test_documented_skill_command_matches_registered_shell_filter(tool_name: str) -> None:
    command = _documented_skill_command()
    hook_filter = HOOK_FILTERS[tool_name]
    prefix = f"{tool_name}("

    assert hook_filter.startswith(prefix)
    assert hook_filter.endswith(")")
    assert fnmatchcase(command, hook_filter[len(prefix) : -1])


def test_plugin_registers_diagnose_skill_and_diagnosis_agent() -> None:
    manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))

    assert "./agents/diagnosis-agent.md" in manifest["agents"]
    assert "./skills/diagnose" in manifest["skills"]
    assert HOOKS_CONFIG.is_file()


def test_plugin_documents_fail_closed_hook_runtime_requirement() -> None:
    documentation = PLUGIN_README.read_text(encoding="utf-8")
    normalized = " ".join(documentation.split())

    assert "Claude Code 2.1.295 or later" in documentation
    assert "onFailure" in documentation
    assert "`uv` must be installed" in normalized
    assert "every submitted prompt" in normalized
    assert "does not support `matcher`" in normalized


def test_plugin_documents_redaction_coverage_and_limits() -> None:
    documentation = PLUGIN_README.read_text(encoding="utf-8")
    normalized = " ".join(documentation.split())

    assert "Atlassian" in normalized
    assert "DigitalOcean" in normalized
    assert "AWS secret access keys" in normalized
    assert "cookies" in normalized
    assert "English and Portuguese" in normalized
    assert "Shared Access Signatures" in normalized
    assert "Arbitrary unlabeled opaque values" in normalized


def test_missing_uv_fails_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")

    with pytest.raises(pytest.fail.Exception, match="redaction boundary in CI"):
        _require_uv(executable=None)


def test_diagnose_skill_has_only_the_canonical_headless_execution_path() -> None:
    skill = DIAGNOSE_SKILL.read_text(encoding="utf-8")
    components = (
        "incident_validator",
        "configured pipeline adapter",
        "diagnosis-agent",
        "diagnosis_validator",
        "eligibility_gate",
        "report_renderer",
    )

    assert "${CLAUDE_PLUGIN_ROOT}/scripts/diagnose_cli.py" in skill
    assert "single source of truth" in skill
    assert "Do not reconstruct the diagnostic stages" in skill
    assert "entire execution" in skill
    assert "python -m" not in skill
    positions = [skill.index(component) for component in components]
    assert positions == sorted(positions)
    assert all(f"`{exit_code}`" in skill for exit_code in range(4))


def test_diagnose_skill_keeps_interactive_incidents_out_of_shell_input() -> None:
    skill = DIAGNOSE_SKILL.read_text(encoding="utf-8")
    normalized = " ".join(skill.split())

    assert "In an interactive Claude Code session, require a path" in normalized
    assert "standard input for external headless orchestrators" in normalized
    assert "must not synthesize this form with a heredoc" in normalized
    assert "do not place incident contents directly in a shell command" in normalized


def test_documented_skill_command_matches_direct_cli(tmp_path: Path) -> None:
    uv_executable = _require_uv()

    consumer_root = tmp_path / "consumer"
    config_path = consumer_root / "config/pipeline-guard.yaml"
    config_path.parent.mkdir(parents=True)
    shutil.copyfile(DBT_PRESET, config_path)
    incident_path = consumer_root / "incident.json"
    incident_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "incident_id": "skill-parity-incident",
                "source": "ci",
                "environment": "test",
                "pipeline_id": "dbt-build",
                "run_id": "skill-parity-run",
                "failed_step": "dbt-build",
                "occurred_at": "2026-10-01T12:00:00Z",
                "error_summary": "No evidence was supplied",
                "project_profile": "dbt",
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )

    skill_command = _documented_skill_command()
    skill_command = (
        skill_command.replace("${CLAUDE_PLUGIN_ROOT}", PLUGIN_ROOT.as_posix())
        .replace("<consumer-root>/config/pipeline-guard.yaml", config_path.as_posix())
        .replace("<consumer-root>", consumer_root.as_posix())
        .replace("<incident-input-path>", incident_path.as_posix())
    )

    environment = os.environ.copy()
    environment["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "plugin-data")
    environment["ANTHROPIC_MODEL"] = "unused-no-evidence-model"
    common = {
        "text": True,
        "capture_output": True,
        "check": False,
        "cwd": consumer_root,
        "env": environment,
        "timeout": 60,
    }
    skill_result = subprocess.run(skill_command, shell=True, **common)
    direct_result = subprocess.run(
        [
            uv_executable,
            "run",
            str(DIAGNOSE_CLI),
            "--config",
            str(config_path),
            "--project-root",
            str(consumer_root),
            str(incident_path),
        ],
        **common,
    )

    assert skill_result.returncode == direct_result.returncode == 3
    assert json.loads(skill_result.stdout) == json.loads(direct_result.stdout)


def test_diagnose_description_requires_invocation_before_missing_inputs() -> None:
    skill = DIAGNOSE_SKILL.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(skill.split("---", maxsplit=2)[1])
    description = frontmatter["description"]

    assert "$diagnose" in description
    assert "every request whose primary intent" in description
    assert "safe UNKNOWN result when evidence is insufficient" in description
    assert "before asking any questions or requesting files" in description
    assert "must never delay invocation" in description
    assert "NEVER invoke it when the primary intent" in description


def test_diagnose_trigger_cases_cover_positive_and_negative_intent() -> None:
    cases = yaml.safe_load(TRIGGER_CASES.read_text(encoding="utf-8"))

    assert cases["skill"] == "diagnose"
    assert cases["explicit_cases"]["expect_trigger"] is True
    assert cases["implicit_cases"]["expect_trigger"] is True
    assert cases["negative_cases"]["expect_trigger"] is False
    assert len(cases["explicit_cases"]["cases"]) >= 2
    assert len(cases["implicit_cases"]["cases"]) >= 3
    assert len(cases["negative_cases"]["cases"]) >= 4
