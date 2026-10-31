"""Behavior tests for the registered remediation file-access hook."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
HOOKS_CONFIG_PATH = PLUGIN_ROOT / "hooks" / "hooks.json"
GENERIC_PRESET_PATH = PLUGIN_ROOT / "config" / "presets" / "generic.yaml"
UV_EXECUTABLE = shutil.which("uv")


def _registered_hook() -> dict[str, Any]:
    configuration = json.loads(HOOKS_CONFIG_PATH.read_text(encoding="utf-8"))
    registrations = configuration["hooks"]["PreToolUse"]
    matching = [
        registration
        for registration in registrations
        if registration["matcher"] == "Write|Edit|Read"
    ]
    assert len(matching) == 1

    hooks = matching[0]["hooks"]
    assert len(hooks) == 1
    assert hooks[0] == {
        "type": "command",
        "command": "uv",
        "args": ["run", "$" + "{CLAUDE_PLUGIN_ROOT}/hooks/enforce_write_paths.py"],
        "timeout": 30,
        "onFailure": "block",
    }
    return hooks[0]


def _project_root(
    tmp_path: Path,
    *,
    editable_paths: list[str],
    editable_extensions: list[str] | None = None,
    sensitive_paths: list[str] | None = None,
) -> Path:
    project_root = tmp_path / "workspace"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)

    config = yaml.safe_load(GENERIC_PRESET_PATH.read_text(encoding="utf-8"))
    config["safety"]["editable_paths"] = editable_paths
    config["safety"]["editable_extensions"] = editable_extensions or [".py"]
    config["safety"]["sensitive_paths"] = (
        ["models/seeds/**"] if sensitive_paths is None else sensitive_paths
    )
    (config_dir / "pipeline-guard.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    return project_root


def _event(
    *, workspace: Path, tool_name: str, target: Path, **tool_input: str
) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "agent_type": "pipeline-guard:remediation-agent",
        "cwd": str(workspace),
        "tool_input": {"file_path": str(target), **tool_input},
    }


def _run_hook(
    event: dict[str, Any],
    *,
    workspace: Path,
    approved_read_paths: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if UV_EXECUTABLE is None:
        pytest.fail("uv is required to exercise the registered file-access hook")

    hook = _registered_hook()
    environment = os.environ.copy()
    environment["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    environment.pop("PIPELINE_GUARD_APPROVED_READ_PATHS", None)
    if approved_read_paths is not None:
        environment["PIPELINE_GUARD_APPROVED_READ_PATHS"] = json.dumps(
            approved_read_paths
        )
    command = [
        UV_EXECUTABLE,
        *[
            argument.replace("$" + "{CLAUDE_PLUGIN_ROOT}", PLUGIN_ROOT.as_posix())
            for argument in hook["args"]
        ],
    ]
    return subprocess.run(
        command,
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=False,
        cwd=workspace,
        env=environment,
        timeout=30,
    )


def _dispatch_file_tool(
    event: dict[str, Any], *, workspace: Path
) -> dict[str, Any] | None:
    completed = _run_hook(event, workspace=workspace)
    assert completed.returncode == 0, completed.stderr

    response = json.loads(completed.stdout) if completed.stdout.strip() else None
    if response is not None:
        decision = response["hookSpecificOutput"]["permissionDecision"]
        if decision == "deny":
            return response
        assert decision == "allow"

    tool_input = event["tool_input"]
    target = Path(tool_input["file_path"])
    if event["tool_name"] == "Write":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(tool_input["content"], encoding="utf-8")
    else:
        current = target.read_text(encoding="utf-8")
        assert tool_input["old_string"] in current
        target.write_text(
            current.replace(tool_input["old_string"], tool_input["new_string"], 1),
            encoding="utf-8",
        )
    return response


def _dispatch_read_tool(
    event: dict[str, Any],
    *,
    workspace: Path,
    target: Path,
    approved_read_paths: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    completed = _run_hook(
        event, workspace=workspace, approved_read_paths=approved_read_paths
    )
    assert completed.returncode == 0, completed.stderr

    response = json.loads(completed.stdout) if completed.stdout.strip() else None
    if response is not None:
        decision = response["hookSpecificOutput"]["permissionDecision"]
        if decision == "deny":
            return response, None
        assert decision == "allow"

    return response, target.read_text(encoding="utf-8")


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
def test_registered_hook_blocks_out_of_allowlist_write_before_disk_change(
    tmp_path: Path,
    tool_name: str,
) -> None:
    workspace = _project_root(tmp_path, editable_paths=["src/**/*.py"])
    target = workspace / "README.md"
    target.write_text("preserve this content\n", encoding="utf-8")
    before = target.read_bytes()
    tool_input = (
        {"content": "unauthorized replacement\n"}
        if tool_name == "Write"
        else {"old_string": "preserve", "new_string": "unauthorized"}
    )

    response = _dispatch_file_tool(
        _event(workspace=workspace, tool_name=tool_name, target=target, **tool_input),
        workspace=workspace,
    )

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "safety.editable_paths"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert target.read_bytes() == before


def test_registered_hook_allows_an_editable_write(tmp_path: Path) -> None:
    workspace = _project_root(tmp_path, editable_paths=["src/**/*.py"])
    target = workspace / "src" / "models" / "repair.py"
    event = _event(
        workspace=workspace,
        tool_name="Write",
        target=target,
        content="# approved remediation\n",
    )

    response = _dispatch_file_tool(event, workspace=workspace)

    assert response is None
    assert target.read_text(encoding="utf-8") == "# approved remediation\n"


def test_registered_hook_blocks_sensitive_path_even_when_editable(
    tmp_path: Path,
) -> None:
    workspace = _project_root(tmp_path, editable_paths=["models/**/*.py"])
    target = workspace / "models" / "seeds" / "seed.py"
    target.parent.mkdir(parents=True)
    target.write_text("preserve seed\n", encoding="utf-8")
    before = target.read_bytes()
    event = _event(
        workspace=workspace,
        tool_name="Write",
        target=target,
        content="unauthorized seed change\n",
    )

    response = _dispatch_file_tool(event, workspace=workspace)

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "safety.sensitive_paths"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert target.read_bytes() == before


@pytest.mark.parametrize("sensitive_path", ["models/seeds", "models/seeds/"])
@pytest.mark.parametrize(
    "relative_path",
    [
        Path("models/seeds/seed.py"),
        Path("models/seeds/nested/seed.py"),
    ],
)
def test_registered_hook_blocks_bare_sensitive_directories(
    tmp_path: Path,
    sensitive_path: str,
    relative_path: Path,
) -> None:
    workspace = _project_root(
        tmp_path,
        editable_paths=["models/**/*.py"],
        sensitive_paths=[sensitive_path],
    )
    target = workspace / relative_path
    event = _event(
        workspace=workspace,
        tool_name="Write",
        target=target,
        content="must not be written\n",
    )

    response = _dispatch_file_tool(event, workspace=workspace)

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "safety.sensitive_paths"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert not target.exists()


@pytest.mark.parametrize(
    ("sensitive_path", "relative_path"),
    [
        ("**/seeds", Path("models/seeds/seed.py")),
        ("*/seeds", Path("models/seeds/seed.py")),
        ("models/seeds/*", Path("models/seeds/nested/seed.py")),
    ],
)
def test_registered_hook_blocks_descendants_of_sensitive_globs(
    tmp_path: Path,
    sensitive_path: str,
    relative_path: Path,
) -> None:
    workspace = _project_root(
        tmp_path,
        editable_paths=["models/**/*.py"],
        sensitive_paths=[sensitive_path],
    )
    target = workspace / relative_path
    event = _event(
        workspace=workspace,
        tool_name="Write",
        target=target,
        content="must not be written\n",
    )

    response = _dispatch_file_tool(event, workspace=workspace)

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "safety.sensitive_paths"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert not target.exists()


def test_registered_hook_blocks_an_extension_not_in_the_allowlist(
    tmp_path: Path,
) -> None:
    workspace = _project_root(tmp_path, editable_paths=["src/**"])
    target = workspace / "src" / "readme.md"
    event = _event(
        workspace=workspace,
        tool_name="Write",
        target=target,
        content="unsupported extension\n",
    )

    response = _dispatch_file_tool(event, workspace=workspace)

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "extension is not editable"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert not target.exists()


def test_registered_hook_blocks_targets_outside_the_workspace(tmp_path: Path) -> None:
    workspace = _project_root(tmp_path, editable_paths=["**/*.py"])
    target = tmp_path / "outside.py"
    event = _event(
        workspace=workspace,
        tool_name="Write",
        target=target,
        content="must not be created\n",
    )

    response = _dispatch_file_tool(event, workspace=workspace)

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not target.exists()


def test_registered_hook_fails_closed_when_project_config_is_missing(
    tmp_path: Path,
) -> None:
    workspace = _project_root(tmp_path, editable_paths=["src/**/*.py"])
    (workspace / "config" / "pipeline-guard.yaml").unlink()
    target = workspace / "src" / "repair.py"
    event = _event(
        workspace=workspace,
        tool_name="Write",
        target=target,
        content="must not be created\n",
    )

    response = _dispatch_file_tool(event, workspace=workspace)

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "policy could not be verified"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert not target.exists()


def test_registered_hook_protects_the_policy_config_it_reads(tmp_path: Path) -> None:
    workspace = _project_root(tmp_path, editable_paths=["config/**"])
    target = workspace / "config" / "pipeline-guard.yaml"
    before = target.read_bytes()
    event = _event(
        workspace=workspace,
        tool_name="Write",
        target=target,
        content="safety:\n  editable_paths: ['**/*']\n",
    )

    response = _dispatch_file_tool(event, workspace=workspace)

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "configuration is protected"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert target.read_bytes() == before


def test_registered_hook_blocks_case_variant_sensitive_write(tmp_path: Path) -> None:
    workspace = _project_root(tmp_path, editable_paths=["**/*.py"])
    canonical_target = workspace / "models" / "seeds" / "seed.py"
    canonical_target.parent.mkdir(parents=True)
    canonical_target.write_text("preserve seed\n", encoding="utf-8")
    before = canonical_target.read_bytes()
    target = workspace / "Models" / "Seeds" / "seed.py"
    event = _event(
        workspace=workspace,
        tool_name="Write",
        target=target,
        content="unauthorized case-variant write\n",
    )

    response = _dispatch_file_tool(event, workspace=workspace)

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "safety.sensitive_paths"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert canonical_target.read_bytes() == before
    if target.exists():
        assert target.samefile(canonical_target)


def test_registered_hook_protects_case_variant_policy_config(tmp_path: Path) -> None:
    workspace = _project_root(
        tmp_path,
        editable_paths=["**"],
        editable_extensions=[".yaml"],
    )
    canonical_target = workspace / "config" / "pipeline-guard.yaml"
    before = canonical_target.read_bytes()
    target = workspace / "Config" / "pipeline-guard.yaml"
    event = _event(
        workspace=workspace,
        tool_name="Write",
        target=target,
        content="safety:\n  editable_paths: ['**']\n",
    )

    response = _dispatch_file_tool(event, workspace=workspace)

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "configuration is protected"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert canonical_target.read_bytes() == before
    if target.exists():
        assert target.samefile(canonical_target)


def test_registered_hook_blocks_reads_outside_the_workspace(tmp_path: Path) -> None:
    workspace = _project_root(tmp_path, editable_paths=["src/**/*.py"])
    target = tmp_path / "outside.txt"
    target.write_text(
        "outside content must not enter the agent context\n", encoding="utf-8"
    )
    event = _event(workspace=workspace, tool_name="Read", target=target)

    response, result = _dispatch_read_tool(
        event,
        workspace=workspace,
        target=target,
        approved_read_paths=["outside.txt"],
    )

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result is None


def test_registered_hook_blocks_case_variant_sensitive_reads(tmp_path: Path) -> None:
    workspace = _project_root(tmp_path, editable_paths=["src/**/*.py"])
    target = workspace / "Models" / "Seeds" / "seed.sql"
    event = _event(workspace=workspace, tool_name="Read", target=target)

    response, result = _dispatch_read_tool(
        event,
        workspace=workspace,
        target=target,
        approved_read_paths=["models/seeds/seed.sql"],
    )

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result is None


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        "config/cloud-credentials.json",
        "credentials/service.json",
        ".aws/credentials",
        "secrets/prod.yaml",
        "config/API-Token.json",
        "tokens/production.json",
        ".npmrc",
        ".pypirc",
        ".databrickscfg",
        ".git/config",
        ".kube/config",
        ".docker/config.json",
        "infra/terraform.tfvars",
        "infra/terraform.tfvars.json",
        "certs/client.p12",
        "certs/client.pfx",
        "certs/app.keystore",
        "certs/app.jks",
        "config/db.password",
        "nested/billing-service-account-prod.json",
        "auth.json",
        "kubeconfig-prod",
        "kubeconfigs/production.yaml",
        "infra/terraform.tfstate",
        "infra/terraform.tfstate.backup",
        "dbt/profiles.yml",
        "profiles.yml",
        ".dbt/profiles.yml",
        ".pgpass",
        ".m2/settings.xml",
        ".htpasswd",
        "config/prod.env",
        "prod.env",
        ".s3cfg",
        ".boto",
        ".yarnrc.yml",
        ".config/gh/hosts.yml",
        "airflow.cfg",
        "config/airflow.cfg",
        ".gnupg/secring.gpg",
        "conf/vault.kdbx",
    ],
)
def test_registered_hook_blocks_common_secret_carrier_reads(
    tmp_path: Path,
    relative_path: str,
) -> None:
    workspace = _project_root(tmp_path, editable_paths=["**"])
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("API_TOKEN=do-not-read\n", encoding="utf-8")
    event = _event(workspace=workspace, tool_name="Read", target=target)

    response, result = _dispatch_read_tool(
        event,
        workspace=workspace,
        target=target,
        approved_read_paths=[relative_path],
    )

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result is None


def test_registered_hook_blocks_read_without_a_caller_approved_scope(
    tmp_path: Path,
) -> None:
    workspace = _project_root(tmp_path, editable_paths=["src/**/*.py"])
    target = workspace / "src" / "repair.py"
    target.parent.mkdir(parents=True)
    target.write_text("def repair():\n    pass\n", encoding="utf-8")
    event = _event(workspace=workspace, tool_name="Read", target=target)

    response, result = _dispatch_read_tool(event, workspace=workspace, target=target)

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "caller-approved read scope"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert result is None


@pytest.mark.parametrize("relative_path", ["README.md", "docs/runbook.md"])
def test_registered_hook_blocks_workspace_read_outside_the_caller_scope(
    tmp_path: Path,
    relative_path: str,
) -> None:
    workspace = _project_root(tmp_path, editable_paths=["**"])
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unapproved context\n", encoding="utf-8")
    event = _event(workspace=workspace, tool_name="Read", target=target)

    response, result = _dispatch_read_tool(
        event,
        workspace=workspace,
        target=target,
        approved_read_paths=["src/repair.py"],
    )

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "caller-approved read scope"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert result is None


@pytest.mark.parametrize("approved_read_paths", [["src/**/*.py"], ["../outside.py"]])
def test_registered_hook_rejects_non_exact_or_unsafe_caller_read_scope(
    tmp_path: Path,
    approved_read_paths: list[str],
) -> None:
    workspace = _project_root(tmp_path, editable_paths=["src/**/*.py"])
    target = workspace / "src" / "repair.py"
    target.parent.mkdir(parents=True)
    target.write_text("def repair():\n    pass\n", encoding="utf-8")
    event = _event(workspace=workspace, tool_name="Read", target=target)

    response, result = _dispatch_read_tool(
        event,
        workspace=workspace,
        target=target,
        approved_read_paths=approved_read_paths,
    )

    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "caller-approved read scope"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert result is None


def test_registered_hook_allows_a_non_sensitive_caller_approved_workspace_read(
    tmp_path: Path,
) -> None:
    workspace = _project_root(tmp_path, editable_paths=["src/**/*.py"])
    target = workspace / "src" / "repair.py"
    target.parent.mkdir(parents=True)
    target.write_text("def repair():\n    pass\n", encoding="utf-8")
    event = _event(workspace=workspace, tool_name="Read", target=target)

    response, result = _dispatch_read_tool(
        event,
        workspace=workspace,
        target=target,
        approved_read_paths=["src/repair.py"],
    )

    assert response is None
    assert result == "def repair():\n    pass\n"


def test_registered_hook_does_not_restrict_other_agents(tmp_path: Path) -> None:
    workspace = _project_root(tmp_path, editable_paths=["src/**/*.py"])
    target = workspace / "README.md"
    event = _event(
        workspace=workspace,
        tool_name="Write",
        target=target,
        content="ordinary agent write\n",
    )
    event["agent_type"] = "main"

    response = _dispatch_file_tool(event, workspace=workspace)

    assert response is None
    assert target.read_text(encoding="utf-8") == "ordinary agent write\n"
