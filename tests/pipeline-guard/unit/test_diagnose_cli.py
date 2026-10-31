from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
DIAGNOSE_CLI_PATH = PLUGIN_ROOT / "scripts" / "diagnose_cli.py"
CLI_TIMEOUT_SECONDS = 90  # Includes startup margin over the internal 60-second budget.
CLEANUP_TIMEOUT_SECONDS = 5
ENDPOINT_TIMEOUT_SECONDS = 2


class _JobBasicLimits(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _JobBasicLimits),
        ("IoInfo", ctypes.c_ulonglong * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


class _JobAccounting(ctypes.Structure):
    _fields_ = (
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    )


class _WindowsProcessJob:
    """Own descendants even after uv or an intermediate Python process exits."""

    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "QueryInformationJobObject": (
                [
                    wintypes.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                ],
                wintypes.BOOL,
            ),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = arguments
            function.restype = result
        self.handle = self.api.CreateJobObjectW(None, None)
        self._check(self.handle)
        limits = _JobExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            self._check(
                self.api.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
            )
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, pid):
        handle = self.api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        self._check(handle)
        try:
            self._check(self.api.AssignProcessToJobObject(self.handle, handle))
        finally:
            self._check(self.api.CloseHandle(handle))

    def active_processes(self):
        accounting = _JobAccounting()
        self._check(
            self.api.QueryInformationJobObject(
                self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            )
        )
        return accounting.ActiveProcesses

    def assert_empty(self):
        deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
        while self.active_processes() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.active_processes() == 0, "The subprocess tree did not exit"

    def terminate(self):
        self._check(self.api.TerminateJobObject(self.handle, 1))

    def close(self):
        self._check(self.api.CloseHandle(self.handle))


def _run_process_tree(command, *, input_text=None, environment=None, timeout=CLI_TIMEOUT_SECONDS):
    # Wait for stdin before spawning the command: the Windows job must be assigned first.
    supervisor = (
        "import subprocess, sys\n"
        "payload = sys.stdin.read()\n"
        "result = subprocess.run(sys.argv[1:], input=payload, text=True, check=False)\n"
        "raise SystemExit(result.returncode)\n"
    )
    job = _WindowsProcessJob() if os.name == "nt" else None
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", supervisor, *command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=os.name != "nt",
        )
        if job is not None:
            job.assign(process.pid)
        stdout, stderr = process.communicate(input_text or "", timeout=timeout)
        if job is not None:
            # Success must be natural, not a result of cleanup killing leaked children.
            job.assert_empty()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        try:
            if process is not None:
                if job is not None:
                    job.terminate()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        # The entire process group has already exited.
                        assert process.poll() is not None
                if process.poll() is None:
                    process.kill()  # Also covers failure to assign the waiting supervisor.
                process.communicate(timeout=CLEANUP_TIMEOUT_SECONDS)
                if job is not None:
                    job.assert_empty()
        finally:
            if job is not None:
                job.close()  # Kill-on-close remains a fallback if cleanup itself fails.
            if process is not None:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()


def _valid_incident() -> dict:
    return {
        "schema_version": "1.0",
        "incident_id": "incident-123",
        "source": "ci",
        "environment": "test",
        "pipeline_id": "daily-build",
        "run_id": "run-456",
        "failed_step": "compile",
        "occurred_at": "2026-10-01T12:00:00Z",
        "error_summary": "Compilation failed",
        "project_profile": "generic",
    }


sys.path.insert(0, str(PLUGIN_ROOT))

import scripts.diagnose_cli as diagnose_cli  # noqa: E402
from scripts.artifact_store import LocalArtifactStore  # noqa: E402
from scripts.run_manifest import derive_manifest_run_id, load_run_manifest  # noqa: E402
from scripts.validate_config import validate_config  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))

SECOND_CONFIG = PLUGIN_ROOT / "config/projects/second-project.yaml"


def test_diagnosis_flow_with_local_sdk_transport(tmp_path, diagnostic_endpoint):
    result = _run_cli(input_text=json.dumps(_valid_incident()), plugin_data_dir=tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["diagnosis"]["cause_status"] == "CONFIRMED"
    assert len(diagnostic_endpoint) == 2


@pytest.fixture(autouse=True)
def diagnostic_endpoint(monkeypatch, tmp_path):
    """Exercise the real SDK transport with a deterministic local model double."""
    requests: list[str] = []
    workers: list[threading.Thread] = []

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(ENDPOINT_TIMEOUT_SECONDS)
            workers.append(threading.current_thread())

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(length))
            requests.append(json.dumps({"path": self.path, "body": body}, sort_keys=True))
            if self.path.endswith("/count_tokens"):
                response = {"input_tokens": 200}
            else:
                payload = json.loads(body["messages"][0]["content"])
                diagnosis = {
                    "schema_version": "1.0",
                    "incident_id": payload["incident"]["incident_id"],
                    "failure_family": "CODE",
                    "confidence": "HIGH",
                    "cause_status": "CONFIRMED",
                    "cause": "The supplied evidence reports a failure.",
                    "evidence_citations": [
                        {
                            "evidence_id": payload["evidences"][0]["evidence_id"],
                            "claim": "The execution failed.",
                        }
                    ],
                }
                response = {
                    "id": "msg-test",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-diagnosis",
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 200, "output_tokens": 100},
                    "content": [{"type": "text", "text": json.dumps(diagnosis)}],
                }
            serialized = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(serialized)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(serialized)
            self.close_connection = True

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name="diagnostic-test-endpoint",
    )
    thread.start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-diagnosis")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=CLEANUP_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "The local endpoint did not stop"
        assert all(not worker.is_alive() for worker in workers), "Endpoint workers did not stop"
        assert server.socket.fileno() == -1, "The local endpoint socket was not closed"


def _run_cli(
    *arguments: str,
    input_text: str | None = None,
    plugin_data_dir: Path | None = None,
) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment.pop("CLAUDE_PLUGIN_DATA", None)
    if plugin_data_dir is not None:
        environment["CLAUDE_PLUGIN_DATA"] = str(plugin_data_dir)

    # Reuse pytest's environment so dependency setup cannot consume the CLI timeout.
    return _run_process_tree(
        [
            sys.executable,
            str(DIAGNOSE_CLI_PATH),
            "--config",
            str(SECOND_CONFIG),
            *arguments,
        ],
        input_text=input_text,
        environment=environment,
    )


@pytest.mark.skipif(os.name != "nt", reason="Regression for Windows descendant cleanup")
def test_process_tree_cleanup_after_timeout(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
        "stdin=subprocess.DEVNULL)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
    )
    # The parent exits, but its child holds the inherited stdout/stderr pipes open.
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_process_tree([sys.executable, "-c", script, str(pid_file)], timeout=5)
    assert time.monotonic() - started < 5 + 2 * CLEANUP_TIMEOUT_SECONDS + 5
    child_pid = pid_file.read_text()
    processes = subprocess.run(
        ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],  # codespell:ignore fo
        capture_output=True,
        text=True,
        check=True,
        timeout=CLEANUP_TIMEOUT_SECONDS,
    )
    assert all(
        len(row) < 2 or row[1] != child_pid for row in csv.reader(processes.stdout.splitlines())
    ), "A descendant Python process survived cleanup"


def _manifest_run_id(incident: dict, plugin_data_dir: Path) -> str:
    attempt = (
        incident
        if "source" not in incident
        else {
            "incident": incident,
            "config": validate_config(SECOND_CONFIG),
            "project_root": str(Path.cwd().resolve()),
            "model": "test-diagnosis",
            "agent_hash": hashlib.sha256(
                (PLUGIN_ROOT / "agents/diagnosis-agent.md").read_bytes()
            ).hexdigest(),
        }
    )
    return derive_manifest_run_id(
        incident_id=incident["incident_id"],
        source_run_id=incident["run_id"],
        attempt_payload=attempt,
        plugin_data_dir=plugin_data_dir,
    )


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [(_valid_incident(), 0), ({"schema_version": "1.0"}, 2)],
)
def test_headless_cli_returns_documented_codes_without_prompts(payload, expected_code, tmp_path):
    result = _run_cli(
        input_text=json.dumps(payload),
        plugin_data_dir=tmp_path,
    )

    assert result.returncode == expected_code
    if expected_code == 0:
        assert json.loads(result.stdout)["exit_code"] == 0
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert result.stderr.startswith("Invalid incident_input_v1:")


@pytest.mark.parametrize(
    ("model_arguments", "expected_code"),
    [((), 2), (("--model", "explicit-test-model"), 0)],
    ids=["missing-model", "explicit-model"],
)
def test_headless_cli_requires_model_configuration(
    model_arguments, expected_code, tmp_path, monkeypatch, diagnostic_endpoint
):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    result = _run_cli(
        *model_arguments,
        input_text=json.dumps(_valid_incident()),
        plugin_data_dir=tmp_path,
    )

    assert result.returncode == expected_code, result.stderr
    if expected_code == 2:
        assert result.stdout == ""
        assert result.stderr.startswith("Invalid project configuration:")
        assert "ANTHROPIC_MODEL or --model" in result.stderr
        assert diagnostic_endpoint == []
        assert not (tmp_path / "runs").exists()
    else:
        assert result.stderr == ""
        assert json.loads(result.stdout)["diagnosis"]["cause_status"] == "CONFIRMED"
        assert len(diagnostic_endpoint) == 2


def test_headless_cli_accepts_file_input(tmp_path):
    input_file = tmp_path / "incident.json"
    input_file.write_text(json.dumps(_valid_incident()), encoding="utf-8")

    result = _run_cli(str(input_file), plugin_data_dir=tmp_path)

    assert result.returncode == 0
    assert json.loads(result.stdout)["diagnosis"]["cause_status"] == "CONFIRMED"


def test_headless_cli_returns_operational_code_when_manifest_cannot_be_persisted() -> None:
    result = _run_cli(input_text=json.dumps(_valid_incident()))

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Diagnosis failed:")


def test_headless_cli_reports_safe_failure_location(tmp_path, monkeypatch, capsys):
    def failing_flow(*_args, **_kwargs):
        raise RuntimeError("synthetic-provider-secret; synthetic-evidence")

    monkeypatch.setattr(diagnose_cli, "run_diagnosis", failing_flow)
    input_file = tmp_path / "incident.json"
    input_file.write_text(json.dumps(_valid_incident()), encoding="utf-8")

    exit_code = diagnose_cli.main([str(input_file), "--config", str(SECOND_CONFIG)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("Diagnosis failed: RuntimeError\n")
    assert f"{__file__}:{failing_flow.__code__.co_firstlineno + 1}" in captured.err
    assert "synthetic-provider-secret" not in captured.err
    assert "synthetic-evidence" not in captured.err
    assert "raise RuntimeError" not in captured.err


def test_headless_cli_returns_invalid_code_for_non_utf8_file(tmp_path):
    input_file = tmp_path / "incident.json"
    input_file.write_bytes(b"\xff")

    result = _run_cli(str(input_file), plugin_data_dir=tmp_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("Invalid incident_input_v1:")


def test_headless_cli_persists_completed_manifest_for_valid_run(tmp_path: Path) -> None:
    incident = _valid_incident()

    result = _run_cli(
        input_text=json.dumps(incident),
        plugin_data_dir=tmp_path,
    )

    assert result.returncode == 0
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    manifest_run_id = _manifest_run_id(incident, tmp_path)
    assert store.exists(manifest_run_id, "manifest.json") is True
    assert (tmp_path / "runs" / manifest_run_id).is_dir()

    manifest = load_run_manifest(run_id=manifest_run_id, artifact_store=store)
    assert manifest["status"] == "COMPLETED"
    assert manifest["incident_id"] == incident["incident_id"]
    assert len(manifest["evidence_hashes"]) == 2
    assert manifest["limitations"] == []
    result_document = json.loads(result.stdout)
    assert result_document["eligibility_decision"]["decision"] == "ELIGIBLE_FOR_REMEDIATION"
    for reference in ("diagnosis.json", "eligibility.json", "report.json", "report.md"):
        assert store.exists(manifest_run_id, reference)


def test_headless_cli_sanitizes_model_input_and_every_persisted_artifact(
    tmp_path: Path, diagnostic_endpoint
) -> None:
    incident = _valid_incident()
    email = "pipeline.owner@example.com"
    cookie_secret = "literal-session-cookie-value"
    portuguese_password = "literal-senha-portuguesa"
    portuguese_api_key = "literal-chave-api-portuguesa"
    sas_secret = "literal-shared-access-signature"
    token_secrets = (
        "DATABRICKS_TOKEN=placeholder",
        "ATATT3" + ("A1_b-" * 37) + "Z",
        "dop_v1_" + ("a1" * 32),
        "Ab3/" * 10,
    )
    secrets = (
        *token_secrets,
        cookie_secret,
        portuguese_password,
        portuguese_api_key,
        sas_secret,
    )
    incident["failed_step"] = f"notify {email}; credentials: {'; '.join(token_secrets)}"
    incident["error_summary"] = (
        f"Upstream response headers:\nCookie: sessionid={cookie_secret}; theme=dark\n"
        f"SharedAccessSignature=sv=1&sig={sas_secret}&se=2\n"
        f"senha={portuguese_password}\nchave de API: {portuguese_api_key}"
    )

    result = _run_cli(input_text=json.dumps(incident), plugin_data_dir=tmp_path)

    assert result.returncode == 0
    transport_payload = "\n".join(diagnostic_endpoint)
    assert email not in transport_payload
    assert all(secret not in transport_payload for secret in secrets)
    assert "[REDACTED_EMAIL]" in transport_payload
    assert "[REDACTED_SECRET]" in transport_payload

    persisted = b"\n".join(
        artifact.read_bytes() for artifact in (tmp_path / "runs").rglob("*") if artifact.is_file()
    ).decode("utf-8")
    assert email not in persisted
    assert all(secret not in persisted for secret in secrets)
    assert "[REDACTED_EMAIL]" in persisted
    assert "[REDACTED_SECRET]" in persisted


@pytest.mark.parametrize(
    ("sensitive_incident_id", "redaction_marker"),
    [
        ("owner@example.com", "[REDACTED_EMAIL]"),
        ("DATABRICKS_TOKEN=placeholder", "[REDACTED_SECRET]"),
    ],
)
def test_headless_cli_rejects_sensitive_incident_id_without_leaking(
    sensitive_incident_id: str,
    redaction_marker: str,
    tmp_path: Path,
    diagnostic_endpoint,
) -> None:
    incident = _valid_incident()
    incident["incident_id"] = sensitive_incident_id

    result = _run_cli(input_text=json.dumps(incident), plugin_data_dir=tmp_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "Invalid incident_input_v1: IncidentValidationError\n"
    assert sensitive_incident_id not in result.stderr
    assert diagnostic_endpoint == []

    persisted_files = [
        artifact for artifact in (tmp_path / "runs").rglob("*") if artifact.is_file()
    ]
    assert persisted_files
    persisted = b"\n".join(artifact.read_bytes() for artifact in persisted_files).decode("utf-8")
    assert sensitive_incident_id not in persisted
    assert redaction_marker in persisted


def test_headless_cli_persists_failed_manifest_for_identifiable_invalid_run(
    tmp_path: Path,
) -> None:
    incident = _valid_incident()
    incident.pop("source")

    result = _run_cli(
        input_text=json.dumps(incident),
        plugin_data_dir=tmp_path,
    )

    assert result.returncode == 2
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    manifest = load_run_manifest(run_id=_manifest_run_id(incident, tmp_path), artifact_store=store)
    assert manifest["status"] == "FAILED"
    assert manifest["incident_id"] == incident["incident_id"]
    assert manifest["limitations"] == ["Incident validation failed before evidence collection."]


@pytest.mark.parametrize(
    "source_run_id",
    ["scheduled__2026-10-07T10:00:00+00:00", None],
)
def test_headless_cli_persists_manifest_for_supported_external_run_ids(
    source_run_id: str | None,
    tmp_path: Path,
) -> None:
    incident = _valid_incident()
    incident["run_id"] = source_run_id
    if source_run_id is None:
        incident["unavailable_fields"] = {
            "run_id": "The orchestrator did not provide a run identifier."
        }

    result = _run_cli(
        input_text=json.dumps(incident),
        plugin_data_dir=tmp_path,
    )

    assert result.returncode == 0
    manifest_run_id = _manifest_run_id(incident, tmp_path)
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    manifest = load_run_manifest(run_id=manifest_run_id, artifact_store=store)
    assert manifest["run_id"] == manifest_run_id
    assert manifest["incident_id"] == incident["incident_id"]


def test_headless_cli_reuses_manifest_for_identical_retry(
    tmp_path: Path, diagnostic_endpoint
) -> None:
    incident = _valid_incident()
    arguments = {
        "input_text": json.dumps(incident),
        "plugin_data_dir": tmp_path,
    }

    first = _run_cli(**arguments)
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    manifest_run_id = _manifest_run_id(incident, tmp_path)
    original = store.get(manifest_run_id, "manifest.json")
    request_count = len(diagnostic_endpoint)
    second = _run_cli(**arguments)

    assert first.returncode == 0
    assert second.returncode == 0
    assert second.stderr == ""
    assert store.get(manifest_run_id, "manifest.json") == original
    assert len(diagnostic_endpoint) == request_count


def test_headless_cli_resumes_partial_write_without_repeating_model(tmp_path, monkeypatch, capsys):
    model_calls = 0

    def varying_diagnosis(_runner, payload, _budget, _prompt):
        nonlocal model_calls
        model_calls += 1
        return {
            "schema_version": "1.0",
            "incident_id": payload["incident"]["incident_id"],
            "failure_family": "CODE",
            "confidence": "HIGH",
            "cause_status": "CONFIRMED",
            "cause": f"Synthetic diagnosis response {model_calls}.",
            "evidence_citations": [
                {
                    "evidence_id": payload["evidences"][0]["evidence_id"],
                    "claim": "The execution failed.",
                }
            ],
        }

    original_put = LocalArtifactStore.put
    write_failed = False

    def fail_once_after_diagnosis(store, run_id, reference, artifact):
        nonlocal write_failed
        if reference == "eligibility.json" and not write_failed:
            assert store.exists(run_id, "diagnosis.json")
            write_failed = True
            raise OSError("Synthetic persistence failure after diagnosis.json")
        return original_put(store, run_id, reference, artifact)

    monkeypatch.setattr(diagnose_cli.DiagnosisRunner, "__call__", varying_diagnosis)
    monkeypatch.setattr(LocalArtifactStore, "put", fail_once_after_diagnosis)
    incident = _valid_incident()
    input_file = tmp_path / "incident.json"
    input_file.write_text(json.dumps(incident), encoding="utf-8")
    arguments = [str(input_file), "--config", str(SECOND_CONFIG)]

    assert diagnose_cli.main(arguments) == 1
    first = capsys.readouterr()
    assert first.out == ""
    assert first.err.startswith("Diagnosis failed: OSError")
    assert write_failed
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    run_id = _manifest_run_id(incident, tmp_path)
    original_diagnosis = store.get(run_id, "diagnosis.json")
    assert not store.exists(run_id, "eligibility.json")
    assert not store.exists(run_id, "manifest.json")

    exit_code = diagnose_cli.main(arguments)
    second = capsys.readouterr()
    assert exit_code == 0, second.err
    assert second.err == ""
    assert model_calls == 1
    document = json.loads(second.out)
    assert document["exit_code"] == 0
    assert document["diagnosis"] == json.loads(original_diagnosis)
    assert store.get(run_id, "diagnosis.json") == original_diagnosis
    for reference, expected in (
        ("result.json", document),
        ("eligibility.json", document["eligibility_decision"]),
        ("report.json", document["report"]),
    ):
        assert json.loads(store.get(run_id, reference)) == expected
    assert store.get(run_id, "report.md").decode("utf-8") == document["report_markdown"]
    assert load_run_manifest(run_id=run_id, artifact_store=store)["status"] == "COMPLETED"


def test_no_evidence_returns_unknown_report_without_model_call(tmp_path, diagnostic_endpoint):
    incident = _valid_incident()
    incident["project_profile"] = "dbt"
    incident["artifacts"] = []
    result = _run_cli(
        "--config",
        str(PLUGIN_ROOT / "config/presets/dbt.yaml"),
        "--project-root",
        str(tmp_path),
        input_text=json.dumps(incident),
        plugin_data_dir=tmp_path / "host-data",
    )
    assert result.returncode == 3
    document = json.loads(result.stdout)
    assert document["diagnosis"]["cause_status"] == "UNKNOWN"
    assert document["eligibility_decision"]["decision"] == "ESCALATE"
    assert document["report"]["diagnosis"] == document["diagnosis"]
    assert "UNKNOWN" in document["report_markdown"]
    assert diagnostic_endpoint == []


def test_corrected_retry_gets_a_new_terminal_manifest(tmp_path: Path) -> None:
    corrected = _valid_incident()
    invalid = corrected.copy()
    invalid.pop("source")

    first = _run_cli(input_text=json.dumps(invalid), plugin_data_dir=tmp_path)
    second = _run_cli(input_text=json.dumps(corrected), plugin_data_dir=tmp_path)

    assert first.returncode == 2
    assert second.returncode == 0
    assert _manifest_run_id(invalid, tmp_path) != _manifest_run_id(corrected, tmp_path)

    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    failed = load_run_manifest(run_id=_manifest_run_id(invalid, tmp_path), artifact_store=store)
    completed = load_run_manifest(
        run_id=_manifest_run_id(corrected, tmp_path), artifact_store=store
    )
    assert failed["status"] == "FAILED"
    assert completed["status"] == "COMPLETED"
