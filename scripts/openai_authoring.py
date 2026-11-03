# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27.0", "pyyaml>=6.0.3"]
# ///
"""Constrained file-tool loop for OpenAI-compatible remediation models."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

try:  # Support direct script execution and package-style imports.
    from hooks.enforce_write_paths import file_access_denial
    from scripts.execution_budget import ExecutionBudgetExceeded
    from scripts.model_backend import (
        HTTPClient,
        ModelBackendError,
        ModelSettings,
        openai_chat_completion,
    )
except ImportError:  # pragma: no cover - direct script execution
    from execution_budget import ExecutionBudgetExceeded
    from model_backend import (
        HTTPClient,
        ModelBackendError,
        ModelSettings,
        openai_chat_completion,
    )

    from hooks.enforce_write_paths import file_access_denial

_TOKEN_BOUND_OVERHEAD = 512
_MAX_FILE_BYTES = 1_000_000
_T = TypeVar("_T")
_TOOLS: tuple[dict[str, object], ...] = (
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one exact caller-approved workspace-relative file.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file_path"],
                "properties": {"file_path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Replace one policy-approved file with complete UTF-8 content.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file_path", "content"],
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace one unique exact string in a policy-approved UTF-8 file."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file_path", "old_text", "new_text"],
                "properties": {
                    "file_path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
            },
        },
    },
)


class DirectAuthoringError(RuntimeError):
    """Raised when an OpenAI-compatible author cannot produce a safe result."""


class DirectAuthoringLimitReached(DirectAuthoringError):
    """Stable execution-limit signal for the outer authoring runner."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class AuthoringBudget(Protocol):
    @property
    def remaining_seconds(self) -> float: ...

    @property
    def remaining_tokens(self) -> int: ...

    def check(self) -> None: ...

    def record_tokens(self, tokens: int) -> None: ...

    def call_tool(self, operation: Callable[[], _T]) -> _T: ...


@dataclass(frozen=True)
class DirectAuthoringResult:
    result: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    total_tokens: int


def run_openai_authoring(
    workspace: Path,
    *,
    prompt: str,
    agent_prompt: str,
    approved_read_paths: Sequence[str],
    settings: ModelSettings,
    budget: AuthoringBudget,
    max_turns: int,
    client: HTTPClient | None = None,
) -> DirectAuthoringResult:
    """Run a bounded Chat Completions tool loop with no shell or network tools."""

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                agent_prompt + "\n\nTool mapping for this backend: Read=read_file, "
                "Write=write_file, Edit=edit_file. Use only these supplied tools."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    events: list[dict[str, Any]] = []
    total_tokens = 0
    total_input_tokens = 0
    total_output_tokens = 0
    for turn in range(1, max_turns + 1):
        _check_budget(budget)
        input_bound = _token_bound({"messages": messages, "tools": _TOOLS})
        _record_tokens(budget, input_bound)
        total_tokens += input_bound
        total_input_tokens += input_bound
        output_limit = budget.remaining_tokens - 1
        if output_limit < 1:
            raise DirectAuthoringLimitReached("TOKEN_LIMIT")
        try:
            response = openai_chat_completion(
                settings,
                {
                    "model": settings.model,
                    "messages": messages,
                    "tools": list(_TOOLS),
                    "tool_choice": "auto",
                    "max_tokens": output_limit,
                    "stream": False,
                },
                timeout=budget.remaining_seconds,
                client=client,
            )
        except ModelBackendError:
            raise
        choice = _single_choice(response)
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise DirectAuthoringError("model response did not contain a message")
        completion_tokens = _completion_tokens(response.get("usage"), message)
        _record_tokens(budget, completion_tokens)
        total_tokens += completion_tokens
        total_output_tokens += completion_tokens

        assistant_message = _assistant_message(message)
        messages.append(assistant_message)
        tool_calls = assistant_message.get("tool_calls", [])
        events.append(
            {
                "type": "model_turn",
                "turn": turn,
                "finish_reason": choice.get("finish_reason"),
                "tool_calls": [
                    call.get("function", {}).get("name")
                    for call in tool_calls
                    if isinstance(call, Mapping)
                ],
            }
        )
        if tool_calls:
            for call in tool_calls:
                try:
                    tool_message, event = budget.call_tool(
                        lambda call=call: _execute_tool_call(
                            call,
                            workspace=workspace,
                            approved_read_paths=approved_read_paths,
                        )
                    )
                except ExecutionBudgetExceeded as error:
                    raise _limit_from_budget(error) from error
                messages.append(tool_message)
                events.append(event)
            continue

        if choice.get("finish_reason") != "stop":
            raise DirectAuthoringError("model stopped before completing authoring")
        content = assistant_message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DirectAuthoringError("model returned no authoring report")
        result = {
            "type": "result",
            "is_error": False,
            "provider": settings.profile,
            "result": content,
            "usage": {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            },
        }
        return DirectAuthoringResult(result, tuple(events), total_tokens)

    raise DirectAuthoringError("model exceeded the configured authoring turn limit")


def _execute_tool_call(
    call: object,
    *,
    workspace: Path,
    approved_read_paths: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(call, Mapping):
        raise DirectAuthoringError("model returned an invalid tool call")
    call_id = call.get("id")
    function = call.get("function")
    if not isinstance(call_id, str) or not call_id or not isinstance(function, Mapping):
        raise DirectAuthoringError("model returned an invalid tool call")
    name = function.get("name")
    raw_arguments = function.get("arguments")
    if not isinstance(name, str) or not isinstance(raw_arguments, str):
        raise DirectAuthoringError("model returned invalid tool arguments")
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        raise DirectAuthoringError(
            "model returned invalid tool argument JSON"
        ) from error
    if not isinstance(arguments, dict):
        raise DirectAuthoringError("model tool arguments must be an object")

    if name == "read_file":
        file_path = _required_argument(arguments, "file_path")
        target = _authorized_target(
            workspace,
            tool_name="Read",
            file_path=file_path,
            approved_read_paths=approved_read_paths,
        )
        content = _read_file(target)
        output = json.dumps({"content": content}, ensure_ascii=False)
    elif name == "write_file":
        file_path = _required_argument(arguments, "file_path")
        content = _required_argument(arguments, "content", allow_empty=True)
        target = _authorized_target(
            workspace,
            tool_name="Write",
            file_path=file_path,
            approved_read_paths=approved_read_paths,
        )
        _atomic_write(target, content)
        output = json.dumps({"written": file_path}, ensure_ascii=False)
    elif name == "edit_file":
        file_path = _required_argument(arguments, "file_path")
        old_text = _required_argument(arguments, "old_text")
        new_text = _required_argument(arguments, "new_text", allow_empty=True)
        target = _authorized_target(
            workspace,
            tool_name="Edit",
            file_path=file_path,
            approved_read_paths=approved_read_paths,
        )
        original = _read_file(target)
        if original.count(old_text) != 1:
            raise DirectAuthoringError(
                "edit_file old_text must occur exactly once in the target"
            )
        _atomic_write(target, original.replace(old_text, new_text, 1))
        output = json.dumps({"edited": file_path}, ensure_ascii=False)
    else:
        raise DirectAuthoringError("model requested an unsupported tool")

    return (
        {"role": "tool", "tool_call_id": call_id, "content": output},
        {"type": "tool_call", "tool": name, "file_path": file_path},
    )


def _authorized_target(
    workspace: Path,
    *,
    tool_name: str,
    file_path: str,
    approved_read_paths: Sequence[str],
) -> Path:
    denial = file_access_denial(
        workspace_root=workspace,
        tool_name=tool_name,
        file_path=file_path,
        approved_read_paths=approved_read_paths,
    )
    if denial is not None:
        raise DirectAuthoringError(denial)
    resolved_workspace = workspace.resolve(strict=True)
    target = (resolved_workspace / file_path).resolve(strict=False)
    try:
        target.relative_to(resolved_workspace)
    except ValueError as error:
        raise DirectAuthoringError("file tool target escapes the workspace") from error
    return target


def _read_file(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise DirectAuthoringError("file tool target exceeds the read size limit")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise DirectAuthoringError("file tool target is not readable UTF-8") from error


def _atomic_write(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_FILE_BYTES:
        raise DirectAuthoringError("file tool content exceeds the write size limit")
    if not path.parent.is_dir():
        raise DirectAuthoringError("file tool target parent does not exist")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".pipeline-guard-write-",
            delete=False,
        ) as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    except OSError as error:
        raise DirectAuthoringError("file tool target could not be written") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _single_choice(response: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise DirectAuthoringError("model response must contain exactly one choice")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise DirectAuthoringError("model response choice must be an object")
    return choice


def _assistant_message(message: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"role": "assistant"}
    content = message.get("content")
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        result["tool_calls"] = tool_calls
    if isinstance(content, str):
        result["content"] = content
    elif "tool_calls" not in result:
        result["content"] = None
    return result


def _completion_tokens(usage: object, message: object) -> int:
    if isinstance(usage, Mapping):
        value = usage.get("completion_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return _token_bound(message)


def _token_bound(value: object) -> int:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(serialized.encode("utf-8")) + _TOKEN_BOUND_OVERHEAD


def _check_budget(budget: AuthoringBudget) -> None:
    try:
        budget.check()
    except ExecutionBudgetExceeded as error:
        raise _limit_from_budget(error) from error
    if budget.remaining_seconds <= 0:
        raise DirectAuthoringLimitReached("TIME_LIMIT")
    if budget.remaining_tokens <= 0:
        raise DirectAuthoringLimitReached("TOKEN_LIMIT")


def _record_tokens(budget: AuthoringBudget, tokens: int) -> None:
    try:
        budget.record_tokens(tokens)
    except ExecutionBudgetExceeded as error:
        raise _limit_from_budget(error) from error


def _limit_from_budget(error: ExecutionBudgetExceeded) -> DirectAuthoringLimitReached:
    reason = {
        "max_seconds": "TIME_LIMIT",
        "max_tokens": "TOKEN_LIMIT",
        "max_tool_calls": "TOOL_CALL_LIMIT",
    }.get(error.reason, "TOKEN_LIMIT")
    return DirectAuthoringLimitReached(reason)


def _required_argument(
    arguments: Mapping[str, object], field: str, *, allow_empty: bool = False
) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise DirectAuthoringError(f"tool argument {field} must be a string")
    return value
