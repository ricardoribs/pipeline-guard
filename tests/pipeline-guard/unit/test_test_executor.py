from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

TEST_EXECUTOR_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "test_executor.py"
)
PLUGIN_ROOT = TEST_EXECUTOR_PATH.parents[1]

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

FAKE_CONFIG = {
    "validation": {
        "commands": [
            {"name": "fake-check", "command": "echo ok", "timeout_seconds": 10}
        ]
    }
}


def _load_test_executor_module() -> ModuleType:
    scripts_dir = TEST_EXECUTOR_PATH.parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    spec = importlib.util.spec_from_file_location(
        "pipeline_guard_test_executor",
        TEST_EXECUTOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"could not load test executor module from {TEST_EXECUTOR_PATH}"
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_test_executor_module = _load_test_executor_module()
TestExecutor = _test_executor_module.TestExecutor
TestExecutorResult = _test_executor_module.TestExecutorResult
CommandResult = _test_executor_module.CommandResult
TestExecutorConfigurationError = _test_executor_module.TestExecutorConfigurationError
UndeclaredCommandError = _test_executor_module.UndeclaredCommandError
UnsupportedAdapterCommandError = _test_executor_module.UnsupportedAdapterCommandError
DbtAdapter = __import__("adapters.dbt", fromlist=["DbtAdapter"]).DbtAdapter


class _FakeStore:
    def __init__(self):
        self.artifacts: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[tuple[str, str, bytes]] = []

    def put(self, run_id: str, reference: str, artifact: bytes) -> bool:
        self.put_calls.append((run_id, reference, artifact))
        key = (run_id, reference)
        if key in self.artifacts:
            return False
        self.artifacts[key] = artifact
        return True

    def get(self, run_id: str, reference: str) -> bytes:
        return self.artifacts[(run_id, reference)]

    def exists(self, run_id: str, reference: str) -> bool:
        return (run_id, reference) in self.artifacts


def _popen_returning(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Return a fake Popen class whose communicate() returns the given values."""

    class _FakePopen:
        pid = 99999

        def __init__(self, *_args, **_kwargs):
            self.returncode = returncode

        def communicate(self, **_kwargs):
            return stdout, stderr

        def kill(self):
            pass

        def wait(self):
            pass

    return _FakePopen


def test_undeclared_command_never_executes(monkeypatch):
    called = []

    class _TrackingPopen:
        pid = 99999

        def __init__(self, *_args, **_kwargs):
            called.append(True)

        def communicate(self, **_kwargs):
            return "", ""

        def kill(self):
            pass

        def wait(self):
            pass

    monkeypatch.setattr(_test_executor_module.subprocess, "Popen", _TrackingPopen)

    executor = TestExecutor(FAKE_CONFIG, run_id="run-001")
    with pytest.raises(UndeclaredCommandError):
        executor.run(["not-in-allowlist"])

    assert called == []


def test_dbt_logical_commands_execute_only_configured_commands(monkeypatch):
    logical_commands = ("deps", "parse", "compile", "test", "build")
    configured_commands = [
        f"configured-validation --command {name}" for name in logical_commands
    ]
    executed_commands = []

    class _TrackingPopen:
        pid = 99999

        def __init__(self, command, *_args, **_kwargs):
            executed_commands.append(command)
            self.returncode = 0

        def communicate(self, **_kwargs):
            return "ok\n", ""

        def kill(self):
            pass

        def wait(self):
            pass

    monkeypatch.setattr(_test_executor_module.subprocess, "Popen", _TrackingPopen)
    config = {
        "validation": {
            "commands": [
                {"name": name, "command": command, "timeout_seconds": 10}
                for name, command in zip(
                    logical_commands, configured_commands, strict=True
                )
            ]
        }
    }

    result = TestExecutor(config, run_id="run-dbt-logical").run_adapter_commands(
        DbtAdapter(),
        logical_commands,
    )

    assert [command.name for command in result.results] == list(logical_commands)
    assert executed_commands == configured_commands


def test_dbt_command_without_config_declaration_never_executes(monkeypatch):
    called = []

    class _TrackingPopen:
        pid = 99999

        def __init__(self, *_args, **_kwargs):
            called.append(True)

    monkeypatch.setattr(_test_executor_module.subprocess, "Popen", _TrackingPopen)
    config = {
        "validation": {"commands": [{"name": "build", "command": "configured-build"}]}
    }

    executor = TestExecutor(config, run_id="run-dbt-missing")
    with pytest.raises(UndeclaredCommandError, match="test"):
        executor.run_adapter_commands(DbtAdapter(), ["test"])

    assert called == []


def test_dbt_adapter_rejects_command_it_does_not_propose(monkeypatch):
    called = []

    class _TrackingPopen:
        pid = 99999

        def __init__(self, *_args, **_kwargs):
            called.append(True)

    monkeypatch.setattr(_test_executor_module.subprocess, "Popen", _TrackingPopen)
    config = {
        "validation": {"commands": [{"name": "custom", "command": "configured-custom"}]}
    }

    executor = TestExecutor(config, run_id="run-dbt-unsupported")
    with pytest.raises(UnsupportedAdapterCommandError, match="custom"):
        executor.run_adapter_commands(DbtAdapter(), ["custom"])

    assert called == []


def test_declared_command_records_status_pass(monkeypatch):
    monkeypatch.setattr(
        _test_executor_module.subprocess,
        "Popen",
        _popen_returning(returncode=0, stdout="ok\n"),
    )

    executor = TestExecutor(FAKE_CONFIG, run_id="run-002")
    result = executor.run(["fake-check"])

    assert len(result.results) == 1
    cmd = result.results[0]
    assert cmd.status == "PASSED"
    assert cmd.duration_seconds >= 0


def test_validation_command_receives_only_the_explicit_environment(monkeypatch):
    captured_environment = None

    class _TrackingPopen:
        pid = 99999
        returncode = 0

        def __init__(self, *_args, **kwargs):
            nonlocal captured_environment
            captured_environment = kwargs["env"]

        def communicate(self, **_kwargs):
            return "ok\n", ""

        def kill(self):
            pass

        def wait(self):
            pass

    monkeypatch.setenv("PIPELINE_GUARD_HOST_SECRET", "must-not-leak")
    monkeypatch.setattr(_test_executor_module.subprocess, "Popen", _TrackingPopen)

    result = TestExecutor(
        FAKE_CONFIG,
        run_id="run-isolated-environment",
        environment={"PATH": "safe-path", "HOME": "safe-home"},
    ).run(["fake-check"])

    assert result.validation_status == "ALL_PASSED"
    assert captured_environment == {"PATH": "safe-path", "HOME": "safe-home"}


def test_declared_command_records_status_fail(monkeypatch):
    monkeypatch.setattr(
        _test_executor_module.subprocess,
        "Popen",
        _popen_returning(returncode=1, stderr="error"),
    )

    executor = TestExecutor(FAKE_CONFIG, run_id="run-003")
    result = executor.run(["fake-check"])

    assert result.results[0].status == "FAILED"


def test_declared_command_records_log_ref_with_store(monkeypatch):
    monkeypatch.setattr(
        _test_executor_module.subprocess,
        "Popen",
        _popen_returning(returncode=0, stdout="ok\n"),
    )

    store = _FakeStore()
    executor = TestExecutor(FAKE_CONFIG, run_id="run-004", store=store)
    result = executor.run(["fake-check"])

    cmd = result.results[0]
    assert cmd.log_ref is not None
    assert len(store.put_calls) == 1
    put_run_id, put_reference, _ = store.put_calls[0]
    assert put_run_id == "run-004"
    assert put_reference == cmd.log_ref


def test_declared_command_records_no_log_ref_without_store(monkeypatch):
    monkeypatch.setattr(
        _test_executor_module.subprocess,
        "Popen",
        _popen_returning(returncode=0, stdout="ok\n"),
    )

    executor = TestExecutor(FAKE_CONFIG, run_id="run-005")
    result = executor.run(["fake-check"])

    assert result.results[0].log_ref is None


def test_timeout_records_timeout_status(monkeypatch):
    class _TimeoutPopen:
        pid = 99999

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="echo ok", timeout=10)

        def kill(self):
            pass

        def wait(self):
            pass

    monkeypatch.setattr(_test_executor_module.subprocess, "Popen", _TimeoutPopen)
    if os.name != "nt":
        monkeypatch.setattr(
            _test_executor_module.os, "killpg", lambda _pgid, _sig: None
        )

    executor = TestExecutor(FAKE_CONFIG, run_id="run-006")
    result = executor.run(["fake-check"])

    assert result.results[0].status == "TIMEOUT"


def test_missing_validation_config_raises():
    config_without_validation = {"project": {"name": "test"}}

    with pytest.raises(TestExecutorConfigurationError):
        TestExecutor(config_without_validation, run_id="run-007")


def test_second_run_with_same_run_id_gets_distinct_log_ref(monkeypatch):
    monkeypatch.setattr(
        _test_executor_module.subprocess,
        "Popen",
        _popen_returning(returncode=0, stdout="ok\n"),
    )

    store = _FakeStore()
    executor = TestExecutor(FAKE_CONFIG, run_id="run-008", store=store)

    result1 = executor.run(["fake-check"])
    result2 = executor.run(["fake-check"])

    ref1 = result1.results[0].log_ref
    ref2 = result2.results[0].log_ref

    assert ref1 != ref2, "log_ref must differ between runs"
    assert len(store.artifacts) == 2, (
        "both artefacts must be stored, not one overwriting the other"
    )


def test_timeout_terminates_child_processes(monkeypatch):
    kill_calls = []
    wait_calls = []

    class _TimeoutPopen:
        pid = 12345

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, **_kwargs):
            raise subprocess.TimeoutExpired("cmd", 1)

        def kill(self):
            kill_calls.append("kill")

        def wait(self):
            wait_calls.append("wait")

    monkeypatch.setattr(_test_executor_module.subprocess, "Popen", _TimeoutPopen)
    if os.name != "nt":
        monkeypatch.setattr(
            _test_executor_module.os,
            "killpg",
            lambda pgid, sig: kill_calls.append(("killpg", pgid, sig)),
        )

    executor = TestExecutor(FAKE_CONFIG, run_id="run-009")
    result = executor.run(["fake-check"])

    assert result.results[0].status == "TIMEOUT"
    assert len(wait_calls) >= 1, "proc.wait() must be called after termination"
    if os.name == "nt":
        assert "kill" in kill_calls, "proc.kill() must be called on Windows"
    else:
        assert any(isinstance(k, tuple) and k[0] == "killpg" for k in kill_calls), (
            "os.killpg must be called on POSIX"
        )


def test_duplicate_command_names_raise_configuration_error():
    config = {
        "validation": {
            "commands": [
                {"name": "unit", "command": "exit 1", "timeout_seconds": 10},
                {"name": "unit", "command": "exit 0", "timeout_seconds": 10},
            ]
        }
    }
    with pytest.raises(TestExecutorConfigurationError):
        TestExecutor(config, run_id="run-010")


def test_command_entry_missing_required_field_raises_configuration_error():
    config = {"validation": {"commands": [{"name": "unit"}]}}
    with pytest.raises(TestExecutorConfigurationError):
        TestExecutor(config, run_id="run-011")


def test_two_executor_instances_same_run_id_get_distinct_log_refs(monkeypatch):
    monkeypatch.setattr(
        _test_executor_module.subprocess,
        "Popen",
        _popen_returning(returncode=0, stdout="output\n"),
    )

    store = _FakeStore()
    config = {
        "validation": {
            "commands": [
                {"name": "check", "command": "echo output", "timeout_seconds": 10}
            ]
        }
    }

    executor1 = TestExecutor(config, run_id="run-shared", store=store)
    executor2 = TestExecutor(config, run_id="run-shared", store=store)

    result1 = executor1.run(["check"])
    result2 = executor2.run(["check"])

    ref1 = result1.results[0].log_ref
    ref2 = result2.results[0].log_ref

    assert ref1 != ref2, (
        "log_ref must differ between executor instances sharing a run_id"
    )
    assert store.exists("run-shared", ref1), "first log must be retrievable"
    assert store.exists("run-shared", ref2), "second log must be retrievable"


def test_non_utf8_output_is_tolerated_and_next_command_still_runs():
    bad_bytes_command = " ".join(
        (
            'python3 -c "import sys;',
            "sys.stdout.buffer.write(b'\\xff\\xfe bad')\"",
        )
    )
    config = {
        "validation": {
            "commands": [
                {
                    "name": "bad-bytes",
                    "command": bad_bytes_command,
                    "timeout_seconds": 10,
                },
                {"name": "good", "command": "echo ok", "timeout_seconds": 10},
            ]
        }
    }
    executor = TestExecutor(config, run_id="run-012")
    result = executor.run()

    assert len(result.results) == 2, "both commands must produce a result"
    names = [r.name for r in result.results]
    assert "bad-bytes" in names
    assert "good" in names
    assert all(r.status in {"PASSED", "FAILED"} for r in result.results), (
        "no result may be TIMEOUT or missing"
    )


def test_validation_status_all_passed(monkeypatch):
    monkeypatch.setattr(
        _test_executor_module.subprocess,
        "Popen",
        _popen_returning(returncode=0, stdout="ok\n"),
    )

    executor = TestExecutor(FAKE_CONFIG, run_id="run-status-01")
    result = executor.run()

    assert result.validation_status == "ALL_PASSED"


def test_validation_status_has_failures(monkeypatch):
    monkeypatch.setattr(
        _test_executor_module.subprocess,
        "Popen",
        _popen_returning(returncode=1, stderr="error"),
    )

    executor = TestExecutor(FAKE_CONFIG, run_id="run-status-02")
    result = executor.run()

    assert result.validation_status == "HAS_FAILURES"


def test_validation_status_incomplete_when_no_commands_run(monkeypatch):
    monkeypatch.setattr(
        _test_executor_module.subprocess,
        "Popen",
        _popen_returning(returncode=0, stdout="ok\n"),
    )

    config_empty = {"validation": {"commands": []}}
    executor = TestExecutor(config_empty, run_id="run-status-03")
    result = executor.run()

    assert result.validation_status == "INCOMPLETE"


def test_validation_status_has_failures_on_timeout(monkeypatch):
    class _TimeoutPopen:
        pid = 99999

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="echo ok", timeout=10)

        def kill(self):
            pass

        def wait(self):
            pass

    monkeypatch.setattr(_test_executor_module.subprocess, "Popen", _TimeoutPopen)
    if os.name != "nt":
        monkeypatch.setattr(
            _test_executor_module.os, "killpg", lambda _pgid, _sig: None
        )

    executor = TestExecutor(FAKE_CONFIG, run_id="run-status-04")
    result = executor.run()

    assert result.validation_status == "HAS_FAILURES"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX-only: process group kill not applicable on Windows"
)
def test_timeout_kills_child_processes_for_real(tmp_path):
    marker = tmp_path / "child_marker.txt"
    # child_command must not contain unescaped double quotes, because it is
    # embedded inside the outer shell "..." argument.  Escape them explicitly
    # so the shell passes the source intact to the child python3 -c invocation.
    child_command = f'import time; time.sleep(3); open(r"{marker}", "w").write("x")'
    escaped_child = child_command.replace('"', '\\"')
    command = (
        'python3 -c "import subprocess, time; '
        f"p = subprocess.Popen(['python3', '-c', '{escaped_child}']); "
        'time.sleep(5)"'
    )

    config = {
        "validation": {
            "commands": [
                {
                    "name": "long-running",
                    "command": command,
                    "timeout_seconds": 1,
                }
            ]
        }
    }

    executor = TestExecutor(config, run_id="run-013", cwd=tmp_path)
    result = executor.run()

    import time

    time.sleep(3.5)

    assert result.results[0].status == "TIMEOUT"
    assert not marker.exists(), (
        "child process must have been killed before writing the marker"
    )
