# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27.0", "jsonschema>=4.0.0", "pyyaml>=6.0.3"]
# ///
"""Run constrained, non-interactive remediation with selectable model backends."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

import yaml

try:  # Support both direct script execution and package-style imports.
    from .execution_budget import ExecutionBudgetExceeded
    from .model_backend import (
        HTTPClient,
        ModelBackendConfigurationError,
        ModelSettings,
        resolve_model_settings,
    )
    from .openai_authoring import (
        DirectAuthoringError,
        DirectAuthoringLimitReached,
        run_openai_authoring,
    )
    from .redaction import redact_text
    from .workspace_executor import _WindowsProcessJob
except ImportError:  # pragma: no cover - direct script execution
    from execution_budget import ExecutionBudgetExceeded
    from model_backend import (
        HTTPClient,
        ModelBackendConfigurationError,
        ModelSettings,
        resolve_model_settings,
    )
    from openai_authoring import (
        DirectAuthoringError,
        DirectAuthoringLimitReached,
        run_openai_authoring,
    )
    from redaction import redact_text
    from workspace_executor import _WindowsProcessJob

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_AGENT_NAME = "remediation-agent"
_ALLOWED_TOOLS = ("Read", "Write", "Edit")
_DEFAULT_MAX_TURNS = 8
_DEFAULT_TERMINATION_GRACE_SECONDS = 5.0
_IS_WINDOWS = os.name == "nt"
_IS_LINUX = sys.platform.startswith("linux")
_PROC_ROOT = Path("/proc")
_DEAD_PROCESS_STATES = frozenset({"Z", "X", "x"})
_MODEL_ENVIRONMENT = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_MODEL",
    }
)
_SECRET_MODEL_ENVIRONMENT = frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS"}
)
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_T = TypeVar("_T")


class AuthoringRunnerError(RuntimeError):
    """Raised when constrained authorship cannot produce a trustworthy result."""


class AuthoringRunnerConfigurationError(AuthoringRunnerError, ValueError):
    """Raised before starting a model when the invocation is unsafe or incomplete."""


class AuthoringLimitReached(AuthoringRunnerError):
    """Stable limit signal consumed by the unattended orchestration layer."""

    outcome = "LIMIT_REACHED"

    def __init__(self, reason_code: str) -> None:
        if reason_code not in {"TIME_LIMIT", "TOKEN_LIMIT", "TOOL_CALL_LIMIT"}:
            raise ValueError(
                "reason_code must be TIME_LIMIT, TOKEN_LIMIT, or TOOL_CALL_LIMIT"
            )
        self.reason_code = reason_code
        super().__init__(
            f"Authoring stopped because the {reason_code.lower()} was reached"
        )


@dataclass(frozen=True)
class AuthoringResult:
    """Sanitized authoring events and their single terminal result."""

    result: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    stderr: str
    total_tokens: int


class AuthorshipWorkspace(Protocol):
    """Narrow subset of ``ClonedWorkspace`` required by the runner."""

    @property
    def path(self) -> Path: ...

    def authorship_environment(self) -> dict[str, str]: ...


class AuthoringBudget(Protocol):
    """Budget interface shared with ``ExecutionBudget``."""

    @property
    def remaining_seconds(self) -> float: ...

    @property
    def remaining_tokens(self) -> int: ...

    def check(self) -> None: ...

    def record_tokens(self, tokens: int) -> None: ...

    def call_tool(self, operation: Callable[[], _T]) -> _T: ...


class ProcessJob(Protocol):
    """Windows job-object operations used to retain the child process tree."""

    def assign(self, pid: int) -> None: ...

    def terminate(self) -> None: ...

    def wait_empty(self) -> None: ...

    def close(self) -> None: ...


def run_authoring(
    workspace: AuthorshipWorkspace,
    *,
    prompt: str,
    approved_read_paths: Sequence[str],
    budget: AuthoringBudget,
    max_turns: int = _DEFAULT_MAX_TURNS,
    plugin_root: Path = _PLUGIN_ROOT,
    claude_executable: str | None = None,
    model_environment: Mapping[str, str] | None = None,
    model_settings: ModelSettings | None = None,
    http_client: HTTPClient | None = None,
    termination_grace_seconds: float = _DEFAULT_TERMINATION_GRACE_SECONDS,
) -> AuthoringResult:
    """Execute the trusted remediation agent without inheriting host credentials.

    The consumer repository is the process working directory, while the generated
    settings and agent definition live in a sibling temporary directory owned by
    the isolated workspace. Only model endpoint variables are added to the exact
    environment returned by ``ClonedWorkspace.authorship_environment``.
    """

    normalized_prompt = _required_text(prompt, "prompt")
    normalized_paths = _normalize_approved_read_paths(approved_read_paths)
    normalized_turns = _positive_integer(max_turns, "max_turns")
    grace_seconds = _positive_number(
        termination_grace_seconds, "termination_grace_seconds"
    )
    workspace_path = Path(workspace.path).resolve(strict=True)
    if not workspace_path.is_dir():
        raise AuthoringRunnerConfigurationError(
            "workspace.path must be an existing directory"
        )
    resolved_plugin_root = Path(plugin_root).resolve(strict=True)
    if not resolved_plugin_root.is_dir():
        raise AuthoringRunnerConfigurationError(
            "plugin_root must be an existing directory"
        )

    _assert_unprivileged_process()
    model_source = os.environ if model_environment is None else model_environment
    try:
        settings = model_settings or resolve_model_settings(
            provider="anthropic",
            model=model_source.get("ANTHROPIC_MODEL") or "sonnet",
            environment=model_source,
        )
    except ModelBackendConfigurationError as error:
        raise AuthoringRunnerConfigurationError(str(error)) from error
    if settings.provider == "openai-compatible":
        return _run_openai_compatible_authoring(
            workspace_path,
            prompt=normalized_prompt,
            approved_read_paths=normalized_paths,
            budget=budget,
            max_turns=normalized_turns,
            plugin_root=resolved_plugin_root,
            settings=settings,
            http_client=http_client,
        )

    environment = workspace.authorship_environment()
    if not isinstance(environment, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in environment.items()
    ):
        raise AuthoringRunnerConfigurationError(
            "workspace.authorship_environment() must return string environment entries"
        )
    model_variables = _filtered_model_environment(
        model_source, require_credential=settings.api_key is None
    )
    model_variables["ANTHROPIC_MODEL"] = settings.model
    if settings.base_url is not None:
        model_variables["ANTHROPIC_BASE_URL"] = settings.base_url
    if settings.api_key is not None:
        credential_variable = (
            "ANTHROPIC_API_KEY"
            if settings.credential_kind == "api-key"
            else "ANTHROPIC_AUTH_TOKEN"
        )
        model_variables[credential_variable] = settings.api_key
    environment.update(model_variables)
    environment.update(
        {
            "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
            "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "NO_COLOR": "1",
            "PIPELINE_GUARD_APPROVED_READ_PATHS": json.dumps(
                normalized_paths, separators=(",", ":")
            ),
        }
    )

    executable = claude_executable or _resolve_claude_executable()
    if executable is None:
        raise AuthoringRunnerConfigurationError("Claude Code CLI is not available")

    try:
        budget.check()
    except ExecutionBudgetExceeded as error:
        raise _limit_from_budget(error) from error
    token_limit = budget.remaining_tokens
    if token_limit <= 0:
        raise AuthoringLimitReached("TOKEN_LIMIT")
    effective_max_turns = min(normalized_turns, token_limit)
    response_token_limit = max(1, token_limit // effective_max_turns)
    # Bound generated tokens across every allowed turn before starting Claude.
    # Aggregate usage, including input and cache tokens, is checked below, so
    # an over-budget process cannot authorize or publish workspace changes.
    environment["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(response_token_limit)
    timeout = budget.remaining_seconds
    if timeout <= 0:
        raise AuthoringLimitReached("TIME_LIMIT")

    trusted_parent = workspace_path.parent
    with tempfile.TemporaryDirectory(
        prefix="pipeline-guard-authoring-", dir=trusted_parent
    ) as trusted_directory:
        trusted_root = Path(trusted_directory).resolve(strict=True)
        if trusted_root == workspace_path or trusted_root.is_relative_to(
            workspace_path
        ):
            raise AuthoringRunnerConfigurationError(
                "trusted authoring inputs must be outside the consumer repository"
            )
        settings_path, agents_path = _write_trusted_inputs(
            trusted_root,
            plugin_root=resolved_plugin_root,
            model_environment=model_variables,
            max_turns=effective_max_turns,
        )
        command = _authoring_command(
            executable,
            plugin_root=resolved_plugin_root,
            settings_path=settings_path,
            agents_path=agents_path,
            max_turns=effective_max_turns,
            prompt=normalized_prompt,
        )
        stdout, stderr, return_code, timed_out = _run_process(
            command,
            cwd=workspace_path,
            environment=environment,
            timeout=timeout,
            termination_grace_seconds=grace_seconds,
        )

    secrets = tuple(
        value
        for name, value in model_variables.items()
        if name in _SECRET_MODEL_ENVIRONMENT and value
    )
    sanitized_stdout = _sanitize_output(stdout, secrets)
    sanitized_stderr = _sanitize_output(stderr, secrets)
    if timed_out:
        raise AuthoringLimitReached("TIME_LIMIT")

    try:
        events, result = _parse_stream(sanitized_stdout)
    except AuthoringRunnerError as error:
        if return_code != 0:
            raise AuthoringRunnerError(
                f"Claude Code authoring failed with exit {return_code}"
            ) from error
        raise
    total_tokens = _total_tokens(result)
    try:
        budget.record_tokens(total_tokens)
    except ExecutionBudgetExceeded as error:
        raise _limit_from_budget(error) from error
    if return_code != 0:
        raise AuthoringRunnerError(
            f"Claude Code authoring failed with exit {return_code}"
        )
    if result.get("is_error") is not False:
        raise AuthoringRunnerError("Claude Code authoring returned is_error=true")

    return AuthoringResult(
        result=result,
        events=tuple(events),
        stderr=sanitized_stderr,
        total_tokens=total_tokens,
    )


def _run_openai_compatible_authoring(
    workspace_path: Path,
    *,
    prompt: str,
    approved_read_paths: Sequence[str],
    budget: AuthoringBudget,
    max_turns: int,
    plugin_root: Path,
    settings: ModelSettings,
    http_client: HTTPClient | None,
) -> AuthoringResult:
    definition = _agent_definition(
        plugin_root,
        model_environment={"ANTHROPIC_MODEL": settings.model},
        max_turns=max_turns,
    )
    agent_prompt = definition[_AGENT_NAME].get("prompt")
    if not isinstance(agent_prompt, str) or not agent_prompt:
        raise AuthoringRunnerConfigurationError(
            "remediation-agent prompt must not be empty"
        )
    try:
        direct = run_openai_authoring(
            workspace_path,
            prompt=prompt,
            agent_prompt=agent_prompt,
            approved_read_paths=approved_read_paths,
            settings=settings,
            budget=budget,
            max_turns=max_turns,
            client=http_client,
        )
    except DirectAuthoringLimitReached as error:
        raise AuthoringLimitReached(error.reason_code) from error
    except DirectAuthoringError as error:
        raise AuthoringRunnerError(str(error)) from error

    secrets = settings.secret_values
    sanitized_result = _sanitize_document(direct.result, secrets)
    sanitized_events = tuple(
        _sanitize_document(event, secrets) for event in direct.events
    )
    return AuthoringResult(
        result=sanitized_result,
        events=sanitized_events,
        stderr="",
        total_tokens=direct.total_tokens,
    )


def _sanitize_document(
    document: Mapping[str, Any], secrets: Sequence[str]
) -> dict[str, Any]:
    serialized = json.dumps(document, ensure_ascii=False)
    sanitized = _sanitize_output(serialized, secrets)
    parsed = json.loads(sanitized)
    if not isinstance(parsed, dict):
        raise AuthoringRunnerError("sanitized authoring event must be an object")
    return parsed


def _filtered_model_environment(
    source: Mapping[str, str], *, require_credential: bool = True
) -> dict[str, str]:
    environment: dict[str, str] = {}
    for name, value in source.items():
        normalized_name = name.upper()
        if normalized_name not in _MODEL_ENVIRONMENT:
            continue
        if not isinstance(value, str):
            raise AuthoringRunnerConfigurationError(
                f"model environment variable {normalized_name} must be a string"
            )
        environment[normalized_name] = value
    if (
        require_credential
        and not environment.get("ANTHROPIC_AUTH_TOKEN")
        and not environment.get("ANTHROPIC_API_KEY")
    ):
        raise AuthoringRunnerConfigurationError(
            "ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY is required for authoring"
        )
    return environment


def _normalize_approved_read_paths(paths: Sequence[str]) -> list[str]:
    if isinstance(paths, (str, bytes)) or not paths:
        raise AuthoringRunnerConfigurationError(
            "approved_read_paths must be a non-empty sequence of exact relative file paths"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for supplied_path in paths:
        if not isinstance(supplied_path, str) or not supplied_path.strip():
            raise AuthoringRunnerConfigurationError(
                "approved_read_paths must contain non-empty strings"
            )
        if any(character in supplied_path for character in "*?["):
            raise AuthoringRunnerConfigurationError(
                "approved_read_paths must not contain glob patterns"
            )
        path = Path(supplied_path)
        if path.is_absolute() or ".." in path.parts:
            raise AuthoringRunnerConfigurationError(
                "approved_read_paths must stay inside the consumer workspace"
            )
        normalized_path = path.as_posix().removeprefix("./")
        if normalized_path in {"", "."}:
            raise AuthoringRunnerConfigurationError(
                "approved_read_paths must name exact files"
            )
        key = normalized_path.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(normalized_path)
    return normalized


def _write_trusted_inputs(
    trusted_root: Path,
    *,
    plugin_root: Path,
    model_environment: Mapping[str, str],
    max_turns: int,
) -> tuple[Path, Path]:
    settings_path = trusted_root / "settings.json"
    settings_path.write_text(
        json.dumps({"permissions": {"blockReadsOutsideWorkingDirectories": True}}),
        encoding="utf-8",
    )
    agents_path = trusted_root / "agents.json"
    agents_path.write_text(
        json.dumps(
            _agent_definition(
                plugin_root,
                model_environment=model_environment,
                max_turns=max_turns,
            )
        ),
        encoding="utf-8",
    )
    for path in (settings_path, agents_path):
        path.chmod(0o600)
    return settings_path, agents_path


def _agent_definition(
    plugin_root: Path,
    *,
    model_environment: Mapping[str, str],
    max_turns: int,
) -> dict[str, dict[str, object]]:
    agent_path = plugin_root / "agents" / "remediation-agent.md"
    try:
        document = agent_path.read_text(encoding="utf-8")
    except OSError as error:
        raise AuthoringRunnerConfigurationError(
            "trusted remediation-agent.md is unavailable"
        ) from error
    parts = document.split("---", maxsplit=2)
    if len(parts) != 3:
        raise AuthoringRunnerConfigurationError(
            "remediation-agent.md must contain YAML frontmatter"
        )
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict) or metadata.get("name") != _AGENT_NAME:
        raise AuthoringRunnerConfigurationError(
            "remediation-agent.md must define name=remediation-agent"
        )
    raw_tools = metadata.get("tools")
    if not isinstance(raw_tools, str):
        raise AuthoringRunnerConfigurationError(
            "remediation-agent tools must be a string"
        )
    tools = tuple(tool.strip() for tool in raw_tools.split(",") if tool.strip())
    if tools != _ALLOWED_TOOLS:
        raise AuthoringRunnerConfigurationError(
            "remediation-agent tools must be exactly Read, Write and Edit"
        )
    model = model_environment.get("ANTHROPIC_MODEL") or metadata.get("model")
    if not isinstance(model, str) or not model.strip():
        raise AuthoringRunnerConfigurationError(
            "remediation-agent model must be configured"
        )
    agent_prompt = parts[2].strip()
    if not agent_prompt:
        raise AuthoringRunnerConfigurationError(
            "remediation-agent prompt must not be empty"
        )
    return {
        _AGENT_NAME: {
            "description": _required_text(
                metadata.get("description"), "agent description"
            ),
            "prompt": agent_prompt,
            "tools": list(tools),
            "model": model,
            "maxTurns": max_turns,
        }
    }


def _authoring_command(
    executable: str,
    *,
    plugin_root: Path,
    settings_path: Path,
    agents_path: Path,
    max_turns: int,
    prompt: str,
) -> list[str]:
    return [
        executable,
        "--restricted",
        "--print",
        "--agents",
        str(agents_path),
        "--agent",
        _AGENT_NAME,
        "--plugin-dir",
        str(plugin_root),
        "--setting-sources",
        "user",
        "--settings",
        str(settings_path),
        "--tools",
        ",".join(_ALLOWED_TOOLS),
        "--disallowedTools",
        "AskUserQuestion,mcp__*",
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--no-session-persistence",
        "--max-turns",
        str(max_turns),
        prompt,
    ]


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
    termination_grace_seconds: float,
    process_factory: Callable[..., subprocess.Popen[str]] | None = None,
) -> tuple[str, str, int, bool]:
    factory: Callable[..., subprocess.Popen[str]] = process_factory or subprocess.Popen
    job = _create_process_job()
    process: subprocess.Popen[str] | None = None
    try:
        process_options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "cwd": cwd,
            "env": environment,
        }
        if _IS_WINDOWS:
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        process = factory(command, **process_options)
        if job is not None:
            job.assign(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            _stop_completed_process_group(
                process,
                job=job,
                grace_seconds=termination_grace_seconds,
            )
            return stdout, stderr, process.returncode, False
        except subprocess.TimeoutExpired:
            stdout, stderr = _stop_timed_out_process(
                process,
                job=job,
                grace_seconds=termination_grace_seconds,
            )
            return stdout, stderr, process.returncode, True
    except OSError as error:
        raise AuthoringRunnerError("Could not execute Claude Code authoring") from error
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        if job is not None:
            job.close()


def _stop_completed_process_group(
    process: subprocess.Popen[str],
    *,
    job: ProcessJob | None,
    grace_seconds: float,
) -> None:
    """Stop descendants that remain after the process leader exits or times out."""

    _terminate_process_group(process, job)
    if job is not None:
        job.wait_empty()
        return
    if _IS_WINDOWS:
        return

    if _wait_for_process_group_exit(process.pid, grace_seconds):
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if not _wait_for_process_group_exit(process.pid, grace_seconds):
        raise AuthoringRunnerError(
            "Authoring process group did not exit after termination"
        )


def _wait_for_process_group_exit(process_group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not _process_group_has_live_members(process_group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _process_group_has_live_members(process_group_id: int) -> bool:
    if _IS_LINUX:
        linux_result = _linux_process_group_has_live_members(process_group_id)
        if linux_result is not None:
            return linux_result
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _linux_process_group_has_live_members(process_group_id: int) -> bool | None:
    """Return live membership from /proc, ignoring zombies; None means unavailable."""

    try:
        process_directories = tuple(_PROC_ROOT.iterdir())
    except OSError:
        return None

    for process_directory in process_directories:
        if not process_directory.name.isdecimal():
            continue
        try:
            raw_stat = (process_directory / "stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            # Processes can disappear between listing /proc and reading stat.
            # Descendants in this group share our uid and remain readable.
            continue
        parsed_stat = _parse_linux_process_stat(raw_stat)
        if parsed_stat is None:
            continue
        state, process_group = parsed_stat
        if process_group == process_group_id and state not in _DEAD_PROCESS_STATES:
            return True
    return False


def _parse_linux_process_stat(raw_stat: str) -> tuple[str, int] | None:
    command_end = raw_stat.rfind(")")
    if command_end < 0:
        return None
    fields = raw_stat[command_end + 1 :].split()
    if len(fields) < 3:
        return None
    try:
        return fields[0], int(fields[2])
    except ValueError:
        return None


def _stop_timed_out_process(
    process: subprocess.Popen[str],
    *,
    job: ProcessJob | None,
    grace_seconds: float,
) -> tuple[str, str]:
    _interrupt_process_group(process)
    try:
        interrupted_output = process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        interrupted_output = None
    _stop_completed_process_group(process, job=job, grace_seconds=grace_seconds)
    if interrupted_output is not None:
        return interrupted_output
    try:
        output = process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        output = process.communicate()
    return output


def _interrupt_process_group(process: subprocess.Popen[str]) -> None:
    try:
        if _IS_WINDOWS:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        pass


def _terminate_process_group(
    process: subprocess.Popen[str], job: ProcessJob | None
) -> None:
    if _IS_WINDOWS:
        if process.poll() is None:
            with suppress(OSError):
                process.terminate()
        if job is not None:
            job.terminate()
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)


def _create_process_job() -> ProcessJob | None:
    return _WindowsProcessJob() if _IS_WINDOWS else None


def _parse_stream(stdout: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise AuthoringRunnerError(
                f"Claude stream line {line_number} is not valid JSON"
            ) from error
        if not isinstance(event, dict):
            raise AuthoringRunnerError(
                f"Claude stream line {line_number} must be a JSON object"
            )
        events.append(event)
    results = [event for event in events if event.get("type") == "result"]
    if len(results) != 1:
        raise AuthoringRunnerError(
            f"Claude stream must contain exactly one result event; received {len(results)}"
        )
    return events, results[0]


def _total_tokens(result: Mapping[str, Any]) -> int:
    usage = result.get("usage")
    if not isinstance(usage, Mapping):
        raise AuthoringRunnerError("Claude result must contain usage")
    total = 0
    for field in _TOKEN_FIELDS:
        if field in {"input_tokens", "output_tokens"} and field not in usage:
            raise AuthoringRunnerError(f"Claude result must contain usage.{field}")
        value = usage.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AuthoringRunnerError(
                f"Claude result usage.{field} must be a non-negative integer"
            )
        total += value
    return total


def _sanitize_output(text: str, secrets: Sequence[str]) -> str:
    sanitized = redact_text(text)
    for secret in secrets:
        sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


def _resolve_claude_executable() -> str | None:
    if os.name == "nt":
        if native_executable := shutil.which("claude.exe"):
            return native_executable
        if command_shim := shutil.which("claude.cmd"):
            native_executable = (
                Path(command_shim).parent
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code"
                / "bin"
                / "claude.exe"
            )
            if native_executable.is_file():
                return str(native_executable)
    return shutil.which("claude")


def _assert_unprivileged_process() -> None:
    if os.name == "posix" and hasattr(os, "geteuid"):
        if os.geteuid() == 0:
            raise AuthoringRunnerConfigurationError(
                "authoring must run as an unprivileged operating-system user"
            )
        return
    if os.name == "nt":
        import ctypes

        if ctypes.windll.shell32.IsUserAnAdmin():
            raise AuthoringRunnerConfigurationError(
                "authoring must run from a non-elevated Windows process"
            )
        return
    raise AuthoringRunnerConfigurationError(
        f"cannot verify an unprivileged authoring identity on platform {os.name!r}"
    )


def _limit_from_budget(error: ExecutionBudgetExceeded) -> AuthoringLimitReached:
    if error.reason == "max_seconds":
        return AuthoringLimitReached("TIME_LIMIT")
    if error.reason == "max_tool_calls":
        return AuthoringLimitReached("TOOL_CALL_LIMIT")
    return AuthoringLimitReached("TOKEN_LIMIT")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthoringRunnerConfigurationError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AuthoringRunnerConfigurationError(f"{field} must be a positive integer")
    return value


def _positive_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise AuthoringRunnerConfigurationError(f"{field} must be a positive number")
    return float(value)
