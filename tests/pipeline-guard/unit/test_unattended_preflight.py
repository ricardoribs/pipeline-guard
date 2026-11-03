from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

import scripts.unattended_preflight as unattended_preflight  # noqa: E402
from scripts.unattended_preflight import (  # noqa: E402
    UnattendedPreflightError,
    run_preflight,
)

sys.path.remove(str(PLUGIN_ROOT))


def _write_config(
    tmp_path: Path,
    *,
    remediation_enabled: bool = True,
    provider: str = "bitbucket",
    push_credential_ref: str = "env:PUSH_TOKEN",
    authorization_secret_ref: str = "env:POLICY_SECRET",
) -> Path:
    config: dict[str, Any] = {
        "schema_version": "1.0",
        "pipeline": {"adapter": "mock", "root": "."},
        "unattended": {
            "remediation_enabled": remediation_enabled,
            "service_identity": "sp:pipeline-guard",
        },
        "runtime": {
            "artifact_store": "local",
            "execution_budget": {
                "max_tool_calls": 10,
                "max_seconds": 600,
                "max_tokens": 32000,
            },
        },
    }
    if remediation_enabled:
        config["repository"] = {
            "provider": provider,
            "url": "https://example.com/team/repository.git",
            "default_branch": "main",
            "remediation_branch_prefix": "pipeline-guard",
            "push_credential_ref": push_credential_ref,
            "allowed_git_operations": ["push_incident_branch"],
        }
        config["unattended"].update(
            {
                "authorization_secret_ref": authorization_secret_ref,
                "max_model_tokens": 1000,
            }
        )

    config_path = tmp_path / "pipeline-guard.yaml"
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    return config_path


def _directories(tmp_path: Path) -> dict[str, Path]:
    directories = {
        name: tmp_path / name
        for name in ("project", "output", "workspace-parent", "state", "plugin-data")
    }
    for directory in directories.values():
        directory.mkdir()
    return directories


def _environment(
    directories: Mapping[str, Path],
    *,
    provider: str = "bitbucket",
) -> dict[str, str]:
    environment = {
        "PATH": "safe-path",
        "ANTHROPIC_BASE_URL": "https://model.example.com",
        "ANTHROPIC_AUTH_TOKEN": "model-secret-value",
        "ANTHROPIC_MODEL": "gateway-model",
        "CLAUDE_PLUGIN_DATA": str(directories["plugin-data"]),
        "PUSH_TOKEN": "push-secret-value",
        "POLICY_SECRET": "policy-secret-value",
    }
    environment[
        "BITBUCKET_API_TOKEN" if provider == "bitbucket" else "GITHUB_API_TOKEN"
    ] = "pr-secret-value"
    return environment


def _finder(*, missing: str | None = None) -> Callable[[str], str | None]:
    def find(name: str) -> str | None:
        logical_name = "claude" if name.startswith("claude") else name
        if logical_name == missing:
            return None
        return str(Path("tools") / f"{name}.exe")

    return find


def _runner(
    version_output: str = "2.1.295 (Claude Code)",
    *,
    returncode: int = 0,
    captured_environments: list[Mapping[str, str]] | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(
        arguments: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if captured_environments is not None:
            environment = kwargs.get("env")
            assert isinstance(environment, Mapping)
            captured_environments.append(environment)
        return subprocess.CompletedProcess(
            arguments,
            returncode,
            stdout=version_output,
            stderr="",
        )

    return run


def _run(
    config_path: Path,
    directories: Mapping[str, Path],
    environment: Mapping[str, str],
    **overrides: object,
) -> unattended_preflight.UnattendedPreflightResult:
    arguments: dict[str, object] = {
        "project_root": directories["project"],
        "output_dir": directories["output"],
        "workspace_parent": directories["workspace-parent"],
        "authorization_state_dir": directories["state"],
        "environment": environment,
        "executable_finder": _finder(),
        "command_runner": _runner(),
    }
    arguments.update(overrides)
    return run_preflight(config_path, **arguments)  # type: ignore[arg-type]


def _assert_prerequisite_error(
    captured: pytest.ExceptionInfo[UnattendedPreflightError],
    prerequisite: str,
) -> None:
    error = captured.value
    assert error.outcome == "ERROR"
    assert error.exit_code == 2
    assert error.reason_code == "PREREQUISITE_MISSING"
    assert error.prerequisite == prerequisite


def test_remediation_preflight_validates_every_prerequisite_without_residue(
    tmp_path: Path,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path)
    environment = _environment(directories)

    result = _run(config_path, directories, environment)

    assert result.config["unattended"]["remediation_enabled"] is True
    assert result.project_root == directories["project"].resolve()
    assert result.output_dir == directories["output"].resolve()
    assert result.workspace_parent == directories["workspace-parent"].resolve()
    assert result.authorization_state_dir == directories["state"].resolve()
    assert result.claude_version == (2, 1, 295)
    assert result.model == "gateway-model"
    assert list(directories["workspace-parent"].iterdir()) == []
    assert list(directories["state"].iterdir()) == []


def test_report_only_does_not_require_repository_credentials_or_state_directory(
    tmp_path: Path,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path, remediation_enabled=False)
    environment = _environment(directories)
    for name in ("PUSH_TOKEN", "BITBUCKET_API_TOKEN", "POLICY_SECRET"):
        environment.pop(name)

    result = _run(
        config_path,
        directories,
        environment,
        authorization_state_dir=None,
    )

    assert result.authorization_state_dir is None
    assert result.config["unattended"]["remediation_enabled"] is False


def test_openai_compatible_backend_does_not_require_claude_code(
    tmp_path: Path,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path, remediation_enabled=False)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["runtime"]["model"] = {
        "provider": "ollama",
        "name": "llama-test",
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    environment = _environment(directories)
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "PUSH_TOKEN",
        "BITBUCKET_API_TOKEN",
        "POLICY_SECRET",
    ):
        environment.pop(name, None)

    result = _run(
        config_path,
        directories,
        environment,
        authorization_state_dir=None,
        executable_finder=_finder(missing="claude"),
    )

    assert result.model == "llama-test"
    assert result.model_settings is not None
    assert result.model_settings.profile == "ollama"
    assert result.claude_executable is None
    assert result.claude_version is None


def test_databricks_m2m_preflight_uses_unity_gateway_without_claude_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path, remediation_enabled=False)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["runtime"]["model"] = {"provider": "databricks"}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    environment = _environment(directories)
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"):
        environment.pop(name)
    environment.update(
        {
            "DATABRICKS_HOST": "https://workspace.example",
            "DATABRICKS_MODEL_CLIENT_ID": "model-client-id",
            "DATABRICKS_MODEL_CLIENT_SECRET": "model-client-secret",
        }
    )
    original_resolver = unattended_preflight.resolve_model_settings

    class OAuthClient:
        def post(self, _url: str, **_kwargs: object) -> object:
            class Response:
                status_code = 200
                text = ""

                @staticmethod
                def json() -> object:
                    return {"access_token": "short-lived-token"}

            return Response()

    def resolve_with_fake_oauth(*args: object, **kwargs: object) -> object:
        kwargs["oauth_client"] = OAuthClient()
        return original_resolver(*args, **kwargs)

    monkeypatch.setattr(
        unattended_preflight,
        "resolve_model_settings",
        resolve_with_fake_oauth,
    )

    result = _run(
        config_path,
        directories,
        environment,
        authorization_state_dir=None,
        executable_finder=_finder(missing="claude"),
    )

    assert result.model == "system.ai.meta-llama-3-3-70b-instruct"
    assert result.model_settings is not None
    assert result.model_settings.base_url == (
        "https://workspace.example/ai-gateway/mlflow/v1"
    )
    assert result.claude_executable is None


def test_config_without_unattended_section_is_rejected(tmp_path: Path) -> None:
    directories = _directories(tmp_path)
    config_path = tmp_path / "pipeline-guard.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "pipeline": {"adapter": "mock"},
                "runtime": {
                    "execution_budget": {
                        "max_tool_calls": 1,
                        "max_seconds": 60,
                        "max_tokens": 100,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(UnattendedPreflightError) as captured:
        _run(config_path, directories, _environment(directories))

    _assert_prerequisite_error(captured, "--config")
    assert "unattended section" in str(captured.value)


@pytest.mark.parametrize("missing", ["uv", "git", "claude"])
def test_missing_runtime_tool_is_actionable(tmp_path: Path, missing: str) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path)

    with pytest.raises(UnattendedPreflightError) as captured:
        _run(
            config_path,
            directories,
            _environment(directories),
            executable_finder=_finder(missing=missing),
        )

    _assert_prerequisite_error(captured, missing)
    assert "PATH" in str(captured.value)


@pytest.mark.parametrize(
    "version_output",
    ["2.1.294 (Claude Code)", "Claude Code development build"],
)
def test_unsupported_or_unrecognized_claude_version_is_rejected(
    tmp_path: Path,
    version_output: str,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path)

    with pytest.raises(UnattendedPreflightError) as captured:
        _run(
            config_path,
            directories,
            _environment(directories),
            command_runner=_runner(version_output),
        )

    _assert_prerequisite_error(captured, "claude")


def test_version_probe_receives_no_secret_environment(tmp_path: Path) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path)
    environments: list[Mapping[str, str]] = []

    _run(
        config_path,
        directories,
        _environment(directories),
        command_runner=_runner(captured_environments=environments),
    )

    assert environments == [{"PATH": "safe-path"}]


def test_unsupported_private_publisher_provider_is_rejected(tmp_path: Path) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path, provider="gitlab")

    with pytest.raises(UnattendedPreflightError) as captured:
        _run(config_path, directories, _environment(directories))

    _assert_prerequisite_error(captured, "repository.provider")
    assert "bitbucket or github" in str(captured.value)


@pytest.mark.parametrize(
    ("missing_names", "prerequisite"),
    [
        (("ANTHROPIC_BASE_URL",), "ANTHROPIC_BASE_URL"),
        (("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"), "model credentials"),
        (("ANTHROPIC_MODEL",), "model"),
        (("PUSH_TOKEN",), "repository.push_credential_ref"),
        (("BITBUCKET_API_TOKEN",), "BITBUCKET_API_TOKEN"),
        (("POLICY_SECRET",), "unattended.authorization_secret_ref"),
    ],
)
def test_missing_environment_prerequisite_never_leaks_values(
    tmp_path: Path,
    missing_names: tuple[str, ...],
    prerequisite: str,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path)
    environment = _environment(directories)
    secret_values = tuple(environment.values())
    for name in missing_names:
        environment.pop(name, None)

    with pytest.raises(UnattendedPreflightError) as captured:
        _run(config_path, directories, environment)

    _assert_prerequisite_error(captured, prerequisite)
    error_text = str(captured.value)
    assert all(secret not in error_text for secret in secret_values)


def test_explicit_model_replaces_missing_model_environment_variable(
    tmp_path: Path,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path)
    environment = _environment(directories)
    environment.pop("ANTHROPIC_MODEL")

    result = _run(config_path, directories, environment, model="cli-model")

    assert result.model == "cli-model"


def test_github_credentials_are_selected_from_repository_provider(
    tmp_path: Path,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path, provider="github")

    result = _run(
        config_path,
        directories,
        _environment(directories, provider="github"),
    )

    assert result.config["repository"]["provider"] == "github"


def test_secret_references_use_host_resolved_environment_names(tmp_path: Path) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(
        tmp_path,
        push_credential_ref="secret:git/push-token",
        authorization_secret_ref="secret:policy-signing-key",
    )
    environment = _environment(directories)
    environment.pop("PUSH_TOKEN")
    environment.pop("POLICY_SECRET")
    environment["SECRET_GIT_PUSH_TOKEN"] = "resolved-push-secret"
    environment["SECRET_POLICY_SIGNING_KEY"] = "resolved-policy-secret"

    result = _run(config_path, directories, environment)

    assert result.authorization_state_dir == directories["state"].resolve()


def test_remediation_requires_authorization_state_directory(tmp_path: Path) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path)

    with pytest.raises(UnattendedPreflightError) as captured:
        _run(
            config_path,
            directories,
            _environment(directories),
            authorization_state_dir=None,
        )

    _assert_prerequisite_error(captured, "--authorization-state-dir")
    assert list(directories["workspace-parent"].iterdir()) == []


@pytest.mark.parametrize("containing_directory", ["project", "workspace-parent"])
def test_authorization_state_directory_must_be_outside_execution_directories(
    tmp_path: Path,
    containing_directory: str,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path)
    unsafe_state = directories[containing_directory] / "authorization-state"
    unsafe_state.mkdir()

    with pytest.raises(UnattendedPreflightError) as captured:
        _run(
            config_path,
            directories,
            _environment(directories),
            authorization_state_dir=unsafe_state,
        )

    _assert_prerequisite_error(captured, "--authorization-state-dir")
    assert "separate" in str(captured.value)


def test_create_once_failure_is_rejected_without_creating_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path)

    def unsupported_put(
        _store: object, _run_id: str, _reference: str, _data: bytes
    ) -> bool:
        raise OSError("filesystem-link-secret-detail")

    monkeypatch.setattr(unattended_preflight.LocalArtifactStore, "put", unsupported_put)

    with pytest.raises(UnattendedPreflightError) as captured:
        _run(config_path, directories, _environment(directories))

    _assert_prerequisite_error(captured, "--authorization-state-dir")
    assert "filesystem-link-secret-detail" not in str(captured.value)
    assert list(directories["workspace-parent"].iterdir()) == []


def test_non_writable_output_directory_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path)
    original_named_temporary_file = tempfile.NamedTemporaryFile

    def fail_for_output(*args: Any, **kwargs: Any) -> Any:
        if Path(str(kwargs.get("dir"))).resolve() == directories["output"].resolve():
            raise OSError("write-secret-detail")
        return original_named_temporary_file(*args, **kwargs)

    monkeypatch.setattr(
        unattended_preflight.tempfile, "NamedTemporaryFile", fail_for_output
    )

    with pytest.raises(UnattendedPreflightError) as captured:
        _run(config_path, directories, _environment(directories))

    _assert_prerequisite_error(captured, "--output-dir")
    assert "write-secret-detail" not in str(captured.value)


def test_invalid_config_is_reported_as_structured_prerequisite_error(
    tmp_path: Path,
) -> None:
    directories = _directories(tmp_path)
    config_path = _write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    del config["pipeline"]["adapter"]
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(UnattendedPreflightError) as captured:
        _run(config_path, directories, _environment(directories))

    _assert_prerequisite_error(captured, "--config")
    assert "invalid" in str(captured.value)
