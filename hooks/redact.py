#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Protect diagnosis-bound Claude Code prompts and tool inputs.

The ``--hook`` mode implements the registered ``UserPromptSubmit`` and
``PreToolUse`` boundaries for interactive diagnosis invocations. The default
and ``--text`` modes expose the same shared redaction primitives for
command-line use.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_plugin_root_was_added = str(_PLUGIN_ROOT) not in sys.path
if _plugin_root_was_added:
    sys.path.insert(0, str(_PLUGIN_ROOT))
try:
    from scripts.redaction import redact_payload, redact_text
finally:
    if _plugin_root_was_added:
        sys.path.remove(str(_PLUGIN_ROOT))

_DIAGNOSIS_AGENT = "diagnosis-agent"
_DIAGNOSE_ENTRYPOINT = "diagnose_cli.py"
_DIAGNOSIS_MARKER_RE = re.compile(
    r"(?:\$diagnose\b|/(?:[a-z0-9_-]+:)?diagnose\b|"
    r"(?:[a-z0-9_-]+:)?diagnosis-agent\b|diagnose_cli\.py\b)",
    re.IGNORECASE,
)
_DIAGNOSIS_ACTION_RE = re.compile(
    r"\b(?:diagnos\w*|diagn[oó]st\w*|investig\w*|analis\w*|analy[sz]\w*|"
    r"explain\w*|explic\w*|check\w*|verific\w*)\b",
    re.IGNORECASE,
)
_DIAGNOSIS_CONTEXT_RE = re.compile(
    r"\b(?:pipeline|dbt|incident\w*|run|execu(?:cao|ção|ções)|falha\w*|fail\w*)\b",
    re.IGNORECASE,
)


def _is_diagnosis_agent_call(tool_name: object, tool_input: Mapping[str, Any]) -> bool:
    if tool_name != "Agent":
        return False

    agent_name = tool_input.get("subagent_type", tool_input.get("agent_type"))
    if not isinstance(agent_name, str):
        return False
    return agent_name.rsplit(":", maxsplit=1)[-1] == _DIAGNOSIS_AGENT


def _is_headless_diagnosis_call(tool_name: object, tool_input: Mapping[str, Any]) -> bool:
    command = tool_input.get("command")
    return (
        tool_name in {"Bash", "PowerShell"}
        and isinstance(command, str)
        and _DIAGNOSE_ENTRYPOINT in command
    )


def _is_diagnosis_prompt(prompt: str) -> bool:
    return _DIAGNOSIS_MARKER_RE.search(prompt) is not None or (
        _DIAGNOSIS_ACTION_RE.search(prompt) is not None
        and _DIAGNOSIS_CONTEXT_RE.search(prompt) is not None
    )


def _guard_user_prompt(event: Mapping[str, Any]) -> dict[str, Any] | None:
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("hook event prompt must be a string")
    if not _is_diagnosis_prompt(prompt) or redact_text(prompt) == prompt:
        return None

    return {
        "decision": "block",
        "reason": (
            "Diagnosis prompt blocked because it contains sensitive data. "
            "Pass only a safe path to the UTF-8 incident JSON; do not paste incident "
            "contents or credentials into the prompt."
        ),
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "suppressOriginalPrompt": True,
        },
    }


def redact_hook_event(event: object) -> dict[str, Any] | None:
    """Protect a diagnosis-bound prompt or return sanitized tool input."""
    if not isinstance(event, Mapping):
        raise ValueError("hook event must be a JSON object")

    hook_event_name = event.get("hook_event_name")
    if hook_event_name == "UserPromptSubmit":
        return _guard_user_prompt(event)
    if hook_event_name not in {None, "PreToolUse"}:
        return None

    tool_input = event.get("tool_input")
    if not isinstance(tool_input, Mapping):
        raise ValueError("hook event tool_input must be a JSON object")

    tool_name = event.get("tool_name")
    if not (
        _is_diagnosis_agent_call(tool_name, tool_input)
        or _is_headless_diagnosis_call(tool_name, tool_input)
    ):
        return None

    updated_input = redact_payload(tool_input)
    if not isinstance(updated_input, dict):
        raise TypeError("sanitized tool_input must remain a JSON object")
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated_input,
        }
    }


def _usage() -> str:
    return "usage: redact.py [--text | --hook] < input"


def main(argv: Sequence[str] | None = None) -> int:
    """Sanitize stdin as JSON, arbitrary text, or a Claude Code hook event."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ["--text"], ["--hook"]):
        print(_usage(), file=sys.stderr)
        return 2

    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
    except UnicodeDecodeError:
        print("redaction input must be valid UTF-8", file=sys.stderr)
        return 2

    if args == ["--text"]:
        sys.stdout.write(redact_text(raw))
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print("redaction input must be valid JSON", file=sys.stderr)
        return 2

    if args == ["--hook"]:
        try:
            result = redact_hook_event(payload)
        except (TypeError, ValueError):
            print("redaction hook input must be a valid supported hook event", file=sys.stderr)
            return 2
        if result is None:
            return 0
    else:
        result = redact_payload(payload)

    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
