"""Execute validation commands declared in a project config."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

try:
    from .artifact_store import ArtifactStore
except ImportError:
    from artifact_store import ArtifactStore

# Redaction: uses redact_text from scripts/redaction.py when importable.
# Falls back to _fallback_redact (inline regex) only when the module cannot be loaded.
try:
    try:
        from .redaction import redact_text as _redact_fn
    except ImportError:
        from redaction import redact_text as _redact_fn
except ImportError:
    _FALLBACK_REDACT_RE = re.compile(
        r"(?P<prefix>[\"']?(?:password|token|secret|apikey|api_key|authorization)[\"']?\s*[:=]\s*)"
        r"(?P<value>\S+)",
        re.IGNORECASE,
    )

    def _redact_fn(text: str) -> str:  # type: ignore[misc]
        return _FALLBACK_REDACT_RE.sub(r"\g<prefix>[REDACTED]", text)


class TestExecutorConfigurationError(ValueError):
    """Raised when the validation configuration is invalid."""


class UndeclaredCommandError(ValueError):
    """Raised when attempting to execute a command not declared in validation.commands."""


class UnsupportedAdapterCommandError(ValueError):
    """Raised when a logical command is not proposed by the selected adapter."""


class ValidationCommandProposer(Protocol):
    """Adapter interface for validation commands safe to request by logical name."""

    def propose_validation_commands(self) -> Sequence[str]:
        """Return logical command names supported by this adapter."""
        ...


@dataclass
class CommandResult:
    name: str
    status: str  # "PASSED", "FAILED", or "TIMEOUT"
    duration_seconds: float
    log_ref: str | None


@dataclass
class TestExecutorResult:
    run_id: str
    results: list[CommandResult]
    validation_status: str  # "ALL_PASSED", "HAS_FAILURES", or "INCOMPLETE"


class TestExecutor:
    """Execute validation commands declared in a project config allowlist."""

    def __init__(
        self,
        project_config: Mapping[str, Any],
        *,
        run_id: str,
        store: ArtifactStore | None = None,
        cwd: Path | None = None,
    ) -> None:
        try:
            commands = project_config["validation"]["commands"]
        except (KeyError, TypeError):
            raise TestExecutorConfigurationError(
                "project config must contain 'validation.commands'"
            ) from None
        if not isinstance(commands, list):
            raise TestExecutorConfigurationError("'validation.commands' must be a list")

        self._allowlist: dict[str, dict[str, Any]] = {}
        for entry in commands:
            if not isinstance(entry, dict) or "name" not in entry or "command" not in entry:
                raise TestExecutorConfigurationError(
                    "each entry in 'validation.commands' must have 'name' and 'command' fields"
                )
            name = entry["name"]
            if name in self._allowlist:
                raise TestExecutorConfigurationError(
                    f"duplicate command name '{name}' in 'validation.commands'"
                )
            self._allowlist[name] = entry
        self._run_id = run_id
        self._store = store
        self._cwd = cwd
        self._attempt: int = 0
        self._instance_id: str = uuid.uuid4().hex

    def run(self, command_names: Sequence[str] | None = None) -> TestExecutorResult:
        """Execute declared validation.commands.

        If command_names is provided, execute only those (all must be in the allowlist).
        Returns TestExecutorResult with status, duration, and log_ref per command.
        """
        if command_names is None:
            to_run = list(self._allowlist.values())
        else:
            for name in command_names:
                if name not in self._allowlist:
                    raise UndeclaredCommandError(
                        f"command '{name}' is not declared in validation.commands"
                    )
            to_run = [self._allowlist[name] for name in command_names]

        self._attempt += 1
        results: list[CommandResult] = []
        for entry in to_run:
            name = entry["name"]
            command = entry["command"]
            timeout = entry.get("timeout_seconds") or 300

            log_text = ""
            start = time.monotonic()
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                cwd=self._cwd,
                start_new_session=os.name != "nt",
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                duration_seconds = time.monotonic() - start
                status = "PASSED" if proc.returncode == 0 else "FAILED"
                log_text = stdout + stderr
            except subprocess.TimeoutExpired:
                duration_seconds = float(timeout)
                status = "TIMEOUT"
                log_text = f"Command timed out after {timeout} seconds.\n"
                if os.name == "nt":
                    proc.kill()
                else:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

            log_ref: str | None = None
            if self._store is not None:
                reference = f"{name}-log-{self._instance_id}-{self._attempt}"
                stored = self._store.put(self._run_id, reference, _redact_fn(log_text).encode())
                if not stored:
                    error_message = (
                        f"artifact store rejected reference '{reference}' for run "
                        f"'{self._run_id}': reference already exists"
                    )
                    raise RuntimeError(error_message)
                log_ref = reference

            results.append(
                CommandResult(
                    name=name,
                    status=status,
                    duration_seconds=duration_seconds,
                    log_ref=log_ref,
                )
            )

        if not results:
            validation_status = "INCOMPLETE"
        elif any(r.status in ("FAILED", "TIMEOUT") for r in results):
            validation_status = "HAS_FAILURES"
        else:
            validation_status = "ALL_PASSED"

        return TestExecutorResult(
            run_id=self._run_id, results=results, validation_status=validation_status
        )

    def run_adapter_commands(
        self,
        adapter: ValidationCommandProposer,
        logical_command_names: Sequence[str],
    ) -> TestExecutorResult:
        """Run adapter-proposed logical commands resolved by the config allowlist.

        Adapters declare only logical names (for example, ``build``).  The
        executable string always comes from the project's
        ``validation.commands`` configuration, via :meth:`run`.
        """
        proposed_commands = set(adapter.propose_validation_commands())
        unsupported = [
            command_name
            for command_name in logical_command_names
            if command_name not in proposed_commands
        ]
        if unsupported:
            raise UnsupportedAdapterCommandError(
                "command(s) not proposed by adapter: " + ", ".join(unsupported)
            )

        return self.run(logical_command_names)
