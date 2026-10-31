"""Contract tests for the constrained pipeline remediation agent profile."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
AGENT_PATH = PLUGIN_ROOT / "agents" / "remediation-agent.md"


def _load_profile() -> tuple[dict[str, Any], str]:
    document = AGENT_PATH.read_text(encoding="utf-8")
    parts = document.split("---", maxsplit=2)
    assert len(parts) == 3, "remediation-agent.md must contain YAML frontmatter"

    metadata = yaml.safe_load(parts[1])
    assert isinstance(metadata, dict)
    return metadata, parts[2]


def test_profile_exposes_only_workspace_file_tools() -> None:
    metadata, _ = _load_profile()

    assert metadata["name"] == "remediation-agent"
    assert metadata["model"] == "sonnet"
    assert metadata["tools"] == "Read, Write, Edit"


def test_profile_requires_approved_inputs_and_an_isolated_workspace() -> None:
    _, body = _load_profile()
    normalized_body = " ".join(body.split())

    required_rules = (
        "a validated approval bound to one incident, diagnosis hash, and base commit",
        "the validated diagnosis and only the evidence needed for the correction",
        "`PIPELINE_GUARD_APPROVED_READ_PATHS`",
        "an isolated workspace whose base commit was checked by the workspace executor",
        "`safety.editable_paths`, `safety.editable_extensions`, and `safety.sensitive_paths`",
        "the workspace-relative target paths identified by the diagnosis or caller",
        "The current working directory is the isolated workspace root",
        "Use only workspace-relative paths",
    )
    for rule in required_rules:
        assert rule in normalized_body


def test_profile_has_no_execution_or_external_side_effect_tools() -> None:
    metadata, body = _load_profile()

    forbidden_tools = {"Bash", "PowerShell", "WebFetch", "WebSearch", "Task"}
    declared_tools = {tool.strip() for tool in metadata["tools"].split(",")}
    assert forbidden_tools.isdisjoint(declared_tools)

    normalized_body = " ".join(body.split())
    required_rules = (
        "You have no terminal or network tools",
        "perform Git operations, create commits, push branches, open pull requests",
        "query or write to a warehouse, database, pipeline platform, or other external service",
        (
            "The test executor, patch validator, Git executor, and pull-request integration "
            "are separate controls"
        ),
    )
    for rule in required_rules:
        assert rule in normalized_body


def test_profile_has_no_directory_or_search_tools() -> None:
    metadata, body = _load_profile()

    declared_tools = {tool.strip() for tool in metadata["tools"].split(",")}
    assert {"Glob", "Grep", "LS"}.isdisjoint(declared_tools)
    assert "you have no search or directory-listing tools" in " ".join(body.split())
