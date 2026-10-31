# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0", "pyyaml>=6.0.3"]
# ///
"""Fail-closed prerequisites for unattended Pipeline Guard execution."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Support both direct script execution and package-style imports.
    from .artifact_store import LocalArtifactStore
    from .git_executor import GitExecutorValidationError, validate_remediation_git_configuration
    from .validate_config import ConfigValidationError, validate_config
except ImportError:  # pragma: no cover - direct script execution
    from artifact_store import LocalArtifactStore
    from git_executor import GitExecutorValidationError, validate_remediation_git_configuration
    from validate_config import ConfigValidationError, validate_config

_MINIMUM_CLAUDE_VERSION = (2, 1, 295)
_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
_SUPPORTED_PROVIDERS = frozenset({"bitbucket", "github"})
_PROVIDER_PR_CREDENTIAL = {
    "bitbucket": "BITBUCKET_API_TOKEN",
    "github": "GITHUB_API_TOKEN",
}
_VERSION_ENVIRONMENT = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TZ",
        "WINDIR",
    }
)


class UnattendedPreflightError(RuntimeError):
    """Actionable, non-secret prerequisite failure for ``unattended_cli.py``."""

    outcome = "ERROR"
    exit_code = 2
    reason_code = "PREREQUISITE_MISSING"

    def __init__(self, prerequisite: str, message: str) -> None:
        self.prerequisite = prerequisite
        super().__init__(f"{prerequisite}: {message}")


@dataclass(frozen=True)
class UnattendedPreflightResult:
    """Validated inputs required before any workspace or external write exists."""

    config: dict[str, Any]
    project_root: Path
    output_dir: Path
    workspace_parent: Path
    authorization_state_dir: Path | None
    uv_executable: str
    git_executable: str
    claude_executable: str
    claude_version: tuple[int, int, int]
    model: str


def run_preflight(
    config_path: Path | str,
    *,
    project_root: Path | str,
    output_dir: Path | str,
    workspace_parent: Path | str | None = None,
    authorization_state_dir: Path | str | None = None,
    model: str | None = None,
    environment: Mapping[str, str] | None = None,
    executable_finder: Callable[[str], str | None] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> UnattendedPreflightResult:
    """Validate every unattended prerequisite without creating a repair workspace."""

    host_environment = _normalized_environment(os.environ if environment is None else environment)
    config = _validated_unattended_config(Path(config_path))
    unattended = config["unattended"]
    remediation_enabled = unattended["remediation_enabled"] is True

    finder = executable_finder or shutil.which
    executables = _runtime_executables(finder)
    runner = command_runner or subprocess.run
    claude_version = _claude_version(
        executables["claude"],
        environment=host_environment,
        command_runner=runner,
    )

    selected_model = _model_prerequisites(host_environment, model=model)
    resolved_project_root = _existing_directory(project_root, "--project-root")
    resolved_output_dir = _writable_directory(output_dir, "--output-dir")
    resolved_workspace_parent = _writable_directory(
        workspace_parent if workspace_parent is not None else tempfile.gettempdir(),
        "--workspace-parent",
    )
    plugin_data = _required_environment(host_environment, "CLAUDE_PLUGIN_DATA")
    _writable_directory(plugin_data, "CLAUDE_PLUGIN_DATA")

    resolved_state_dir: Path | None = None
    if remediation_enabled:
        _remediation_credentials(config, host_environment)
        if authorization_state_dir is None:
            raise UnattendedPreflightError(
                "--authorization-state-dir",
                "a durable authorization state directory is required when remediation is enabled",
            )
        resolved_state_dir = _writable_directory(
            authorization_state_dir,
            "--authorization-state-dir",
        )
        _assert_separate_directory(
            resolved_state_dir,
            resolved_project_root,
            prerequisite="--authorization-state-dir",
            other_name="--project-root",
        )
        _assert_separate_directory(
            resolved_state_dir,
            resolved_workspace_parent,
            prerequisite="--authorization-state-dir",
            other_name="--workspace-parent",
        )
        _assert_create_once_support(resolved_state_dir)

    return UnattendedPreflightResult(
        config=config,
        project_root=resolved_project_root,
        output_dir=resolved_output_dir,
        workspace_parent=resolved_workspace_parent,
        authorization_state_dir=resolved_state_dir,
        uv_executable=executables["uv"],
        git_executable=executables["git"],
        claude_executable=executables["claude"],
        claude_version=claude_version,
        model=selected_model,
    )


def _validated_unattended_config(config_path: Path) -> dict[str, Any]:
    try:
        config = validate_config(config_path)
    except ConfigValidationError as error:
        raise UnattendedPreflightError(
            "--config",
            f"the Pipeline Guard configuration is invalid: {error}",
        ) from error
    unattended = config.get("unattended")
    if not isinstance(unattended, dict):
        raise UnattendedPreflightError(
            "--config",
            "the validated configuration must define the unattended section",
        )
    return config


def _runtime_executables(finder: Callable[[str], str | None]) -> dict[str, str]:
    executables: dict[str, str] = {}
    for name in ("uv", "git"):
        executable = finder(name)
        if not executable:
            raise UnattendedPreflightError(
                name,
                f"{name} is not available on PATH",
            )
        executables[name] = executable

    claude_executable = _resolve_claude_executable(finder)
    if claude_executable is None:
        raise UnattendedPreflightError(
            "claude",
            "Claude Code CLI is not available on PATH",
        )
    executables["claude"] = claude_executable
    return executables


def _resolve_claude_executable(finder: Callable[[str], str | None]) -> str | None:
    if os.name == "nt":
        if native_executable := finder("claude.exe"):
            return native_executable
        if command_shim := finder("claude.cmd"):
            bundled_executable = (
                Path(command_shim).parent
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code"
                / "bin"
                / "claude.exe"
            )
            if bundled_executable.is_file():
                return str(bundled_executable)
    return finder("claude")


def _claude_version(
    executable: str,
    *,
    environment: Mapping[str, str],
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[int, int, int]:
    version_environment = {
        name: value for name, value in environment.items() if name in _VERSION_ENVIRONMENT
    }
    try:
        completed = command_runner(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=version_environment,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UnattendedPreflightError(
            "claude",
            "Claude Code CLI version could not be verified",
        ) from error
    if completed.returncode != 0:
        raise UnattendedPreflightError(
            "claude",
            "Claude Code CLI version command failed",
        )
    match = _VERSION_PATTERN.search(f"{completed.stdout}\n{completed.stderr}")
    if match is None:
        raise UnattendedPreflightError(
            "claude",
            "Claude Code CLI returned an unrecognized version",
        )
    version = (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
    )
    if version < _MINIMUM_CLAUDE_VERSION:
        required = ".".join(str(component) for component in _MINIMUM_CLAUDE_VERSION)
        actual = ".".join(str(component) for component in version)
        raise UnattendedPreflightError(
            "claude",
            f"Claude Code CLI {actual} is unsupported; install version {required} or newer",
        )
    return version


def _model_prerequisites(environment: Mapping[str, str], *, model: str | None) -> str:
    _required_environment(environment, "ANTHROPIC_BASE_URL")
    if not environment.get("ANTHROPIC_AUTH_TOKEN") and not environment.get("ANTHROPIC_API_KEY"):
        raise UnattendedPreflightError(
            "model credentials",
            "ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY must be present",
        )
    selected_model = model.strip() if isinstance(model, str) else ""
    if not selected_model:
        selected_model = environment.get("ANTHROPIC_MODEL", "").strip()
    if not selected_model:
        raise UnattendedPreflightError(
            "model",
            "provide --model or set ANTHROPIC_MODEL",
        )
    return selected_model


def _remediation_credentials(
    config: Mapping[str, Any],
    environment: Mapping[str, str],
) -> None:
    repository = config.get("repository")
    if not isinstance(repository, Mapping):
        raise UnattendedPreflightError(
            "repository",
            "repository configuration is required when remediation is enabled",
        )
    provider_value = repository.get("provider")
    provider = provider_value.lower() if isinstance(provider_value, str) else ""
    if provider not in _SUPPORTED_PROVIDERS:
        raise UnattendedPreflightError(
            "repository.provider",
            "the private repair publisher accepts only bitbucket or github",
        )
    try:
        git_configuration = validate_remediation_git_configuration(config)
    except GitExecutorValidationError as error:
        raise UnattendedPreflightError(
            "repository",
            f"remediation Git configuration is incomplete: {error}",
        ) from error

    push_reference = git_configuration["push_credential_ref"]
    _required_reference_environment(
        environment,
        push_reference,
        prerequisite="repository.push_credential_ref",
    )
    _required_environment(environment, _PROVIDER_PR_CREDENTIAL[provider])

    unattended = config["unattended"]
    authorization_reference = unattended.get("authorization_secret_ref")
    _required_reference_environment(
        environment,
        authorization_reference,
        prerequisite="unattended.authorization_secret_ref",
    )


def _required_reference_environment(
    environment: Mapping[str, str],
    reference: object,
    *,
    prerequisite: str,
) -> None:
    if not isinstance(reference, str):
        raise UnattendedPreflightError(
            prerequisite,
            "a credential reference is required",
        )
    candidates = _reference_environment_names(reference)
    if not any(environment.get(name) for name in candidates):
        if reference.startswith(("env:", "${")):
            expected = candidates[0]
            message = f"referenced environment variable {expected} is not present"
        else:
            message = "the host-resolved secret is not present in the environment"
        raise UnattendedPreflightError(prerequisite, message)


def _reference_environment_names(reference: str) -> tuple[str, ...]:
    if reference.startswith("env:"):
        return (reference[4:].upper(),)
    if reference.startswith("${") and reference.endswith("}"):
        return (reference[2:-1].upper(),)
    if reference.startswith("secret:"):
        secret_name = reference[7:].replace("/", "_").replace("-", "_").upper()
        return (f"SECRET_{secret_name}", reference)
    return (reference, reference.upper())


def _normalized_environment(environment: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in environment.items():
        if isinstance(name, str) and isinstance(value, str):
            normalized[name] = value
            normalized.setdefault(name.upper(), value)
    return normalized


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not value or not value.strip():
        raise UnattendedPreflightError(
            name,
            f"required environment variable {name} is not present",
        )
    return value


def _existing_directory(value: Path | str, prerequisite: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise UnattendedPreflightError(
            prerequisite,
            "directory does not exist or cannot be resolved",
        ) from error
    if not path.is_dir():
        raise UnattendedPreflightError(prerequisite, "path must identify a directory")
    return path


def _writable_directory(value: Path | str, prerequisite: str) -> Path:
    path = _existing_directory(value, prerequisite)
    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path,
            prefix=".pipeline-guard-preflight-",
            delete=False,
        ) as probe:
            probe_path = Path(probe.name)
            probe.write(b"preflight")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError as error:
        raise UnattendedPreflightError(
            prerequisite,
            "directory is not writable",
        ) from error
    finally:
        if probe_path is not None:
            with suppress(OSError):
                probe_path.unlink()
    return path


def _assert_separate_directory(
    path: Path,
    other: Path,
    *,
    prerequisite: str,
    other_name: str,
) -> None:
    if path == other or path.is_relative_to(other) or other.is_relative_to(path):
        raise UnattendedPreflightError(
            prerequisite,
            f"directory must be separate from {other_name}",
        )


def _assert_create_once_support(state_directory: Path) -> None:
    store = LocalArtifactStore(plugin_data_dir=state_directory)
    run_id = f"preflight-{uuid.uuid4().hex}"
    reference = "authorization-create-once-probe"
    runs_directory_existed = (state_directory / "runs").exists()
    created = False
    try:
        created = store.put(run_id, reference, b"first")
        duplicate_created = store.put(run_id, reference, b"second")
        preserved = store.get(run_id, reference)
    except (OSError, RuntimeError, ValueError) as error:
        raise UnattendedPreflightError(
            "--authorization-state-dir",
            "filesystem does not support the required atomic create-once operation",
        ) from error
    finally:
        if created:
            with suppress(OSError):
                store.delete(run_id, reference)
        if not runs_directory_existed:
            with suppress(OSError):
                (state_directory / "runs").rmdir()
    if not created or duplicate_created or preserved != b"first":
        raise UnattendedPreflightError(
            "--authorization-state-dir",
            "artifact store did not preserve create-once semantics",
        )
