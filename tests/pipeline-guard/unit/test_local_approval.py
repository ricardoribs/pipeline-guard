from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = PLUGIN_ROOT / "skills" / "remediate" / "scripts" / "local_approval.py"
sys.path.insert(0, str(PLUGIN_ROOT))

local_approval = import_module("skills.remediate.scripts.local_approval")
approval_validator = import_module("scripts.approval_validator")
LocalApprovalError = local_approval.LocalApprovalError
_record_local_approval = local_approval.record_local_approval
ApprovalValidationError = approval_validator.ApprovalValidationError
validate_approval = approval_validator.validate_approval

sys.path.remove(str(PLUGIN_ROOT))

INCIDENT_ID = "incident-local-001"
DIAGNOSIS_HASH = f"sha256:{'a' * 64}"
BASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
NONCE = "a" * 64
CONFIRMATION_REFERENCE = "file:///local-confirmation.json"
# Fixed fictional data makes the test independent of the execution date and person.
NOW = datetime(2000, 1, 1, 12, 0, tzinfo=UTC)
# This lifetime belongs only to the tests; production callers must supply one.
MAX_AGE = timedelta(minutes=5)


def record_local_approval(
    confirmation: object,
    *,
    output_path: Path,
    max_age: timedelta,
    plugin_data_dir: Path | str | None = None,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    """Call the production helper with a stable synthetic confirmation source."""
    return _record_local_approval(
        confirmation,
        output_path=output_path,
        max_age=max_age,
        confirmation_reference=CONFIRMATION_REFERENCE,
        plugin_data_dir=plugin_data_dir,
        current_time=current_time,
    )


@pytest.fixture(autouse=True)
def plugin_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "plugin-data"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
    return data_dir


def _confirmation(**overrides: object) -> dict[str, Any]:
    return {
        "confirmed": True,
        "confirmed_at": NOW.isoformat(),
        "nonce": NONCE,
        "actor": "test-approver",
        "incident_id": INCIDENT_ID,
        "diagnosis_hash": DIAGNOSIS_HASH,
        "base_commit": BASE_COMMIT,
        **overrides,
    }


def test_confirmation_records_an_approval_accepted_by_the_real_validator(tmp_path: Path) -> None:
    output = tmp_path / "approval.json"

    approval = record_local_approval(
        _confirmation(),
        output_path=output,
        max_age=MAX_AGE,
        current_time=NOW + timedelta(seconds=20),
    )
    saved = json.loads(output.read_text(encoding="utf-8"))

    assert saved == approval
    assert saved["decision"] == "APPROVED"
    assert saved["actor"] == "test-approver"
    assert datetime.fromisoformat(saved["decided_at"]) == NOW
    assert saved["channel_type"] == "local"
    assert "nonce" not in saved
    assert saved["external_reference"] == CONFIRMATION_REFERENCE
    assert (
        validate_approval(
            saved,
            expected_incident_id=INCIDENT_ID,
            expected_diagnosis_hash=DIAGNOSIS_HASH,
            expected_base_commit=BASE_COMMIT,
            max_age=MAX_AGE,
            current_time=NOW + timedelta(seconds=21),
        )
        == saved
    )


@pytest.mark.parametrize("confirmed", [False, None, "yes", 1])
def test_non_explicit_confirmation_does_not_create_an_approval(
    tmp_path: Path, confirmed: object
) -> None:
    output = tmp_path / "approval.json"
    with pytest.raises(LocalApprovalError, match="explicit confirmation"):
        record_local_approval(
            _confirmation(confirmed=confirmed),
            output_path=output,
            max_age=MAX_AGE,
            current_time=NOW,
        )
    assert not output.exists()


@pytest.mark.parametrize(
    "field", ["actor", "incident_id", "diagnosis_hash", "base_commit", "confirmed_at", "nonce"]
)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_identity_or_context_does_not_create_an_approval(
    tmp_path: Path, field: str, value: object
) -> None:
    output = tmp_path / "approval.json"
    with pytest.raises(LocalApprovalError, match=field):
        record_local_approval(
            _confirmation(**{field: value}),
            output_path=output,
            max_age=MAX_AGE,
            current_time=NOW,
        )
    assert not output.exists()


@pytest.mark.parametrize("confirmation_reference", ["", "   "])
def test_missing_confirmation_reference_does_not_create_an_approval(
    tmp_path: Path, confirmation_reference: str
) -> None:
    output = tmp_path / "approval.json"

    with pytest.raises(LocalApprovalError, match="confirmation_reference"):
        _record_local_approval(
            _confirmation(),
            output_path=output,
            max_age=MAX_AGE,
            confirmation_reference=confirmation_reference,
            current_time=NOW,
        )

    assert not output.exists()


@pytest.mark.parametrize("nonce", [1, "a" * 63, "a" * 65, "A" * 64, "g" * 64, "../used"])
def test_invalid_nonce_is_rejected(tmp_path: Path, nonce: object) -> None:
    output = tmp_path / "approval.json"

    with pytest.raises(LocalApprovalError, match="nonce"):
        record_local_approval(
            _confirmation(nonce=nonce), output_path=output, max_age=MAX_AGE, current_time=NOW
        )

    assert not output.exists()


@pytest.mark.parametrize("data_dir", [None, "", "relative-plugin-data"])
def test_missing_or_relative_nonce_store_blocks_issuance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_dir: str | None
) -> None:
    if data_dir is None:
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    else:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", data_dir)
    output = tmp_path / "approval.json"

    with pytest.raises(LocalApprovalError, match="CLAUDE_PLUGIN_DATA"):
        record_local_approval(
            _confirmation(), output_path=output, max_age=MAX_AGE, current_time=NOW
        )

    assert not output.exists()


def test_validator_failure_prevents_persistence(tmp_path: Path) -> None:
    output = tmp_path / "approval.json"
    with pytest.raises(ApprovalValidationError, match="max_age"):
        record_local_approval(
            _confirmation(), output_path=output, max_age=timedelta(0), current_time=NOW
        )
    assert not output.exists()
    # A refused input has not consumed the nonce; a valid attempt can still use it.
    record_local_approval(_confirmation(), output_path=output, max_age=MAX_AGE, current_time=NOW)


def test_existing_approval_is_preserved_without_refreshing_timestamp(tmp_path: Path) -> None:
    output = tmp_path / "approval.json"
    record_local_approval(_confirmation(), output_path=output, max_age=MAX_AGE, current_time=NOW)
    original = output.read_bytes()

    with pytest.raises(FileExistsError):
        record_local_approval(
            _confirmation(actor="another-person", nonce="b" * 64),
            output_path=output,
            max_age=MAX_AGE,
            current_time=NOW + timedelta(seconds=1),
        )
    assert output.read_bytes() == original


def test_reused_confirmation_at_another_path_is_rejected_while_still_valid(tmp_path: Path) -> None:
    confirmation = _confirmation()
    first_output = tmp_path / "first" / "approval.json"
    second_output = tmp_path / "second" / "approval.json"
    record_local_approval(confirmation, output_path=first_output, max_age=MAX_AGE, current_time=NOW)
    original = first_output.read_bytes()

    with pytest.raises(LocalApprovalError, match="nonce has already been used"):
        record_local_approval(
            confirmation,
            output_path=second_output,
            max_age=MAX_AGE,
            current_time=NOW + timedelta(seconds=1),
        )

    assert not second_output.exists()
    assert first_output.read_bytes() == original
    assert confirmation == _confirmation()


def test_used_nonce_cannot_be_rebound_to_a_new_timestamp_or_context(tmp_path: Path) -> None:
    record_local_approval(
        _confirmation(), output_path=tmp_path / "first.json", max_age=MAX_AGE, current_time=NOW
    )
    second_output = tmp_path / "second.json"

    with pytest.raises(LocalApprovalError, match="nonce has already been used"):
        record_local_approval(
            _confirmation(
                confirmed_at=(NOW + timedelta(seconds=1)).isoformat(),
                actor="another-person",
                incident_id="another-incident",
                diagnosis_hash=f"sha256:{'b' * 64}",
                base_commit="b" * 40,
            ),
            output_path=second_output,
            max_age=MAX_AGE,
            current_time=NOW + timedelta(seconds=1),
        )

    assert not second_output.exists()


def test_new_confirmation_with_another_nonce_can_authorize_the_same_context(tmp_path: Path) -> None:
    record_local_approval(
        _confirmation(), output_path=tmp_path / "first.json", max_age=MAX_AGE, current_time=NOW
    )
    approval = record_local_approval(
        _confirmation(nonce="b" * 64, confirmed_at=(NOW + timedelta(seconds=1)).isoformat()),
        output_path=tmp_path / "second.json",
        max_age=MAX_AGE,
        current_time=NOW + timedelta(seconds=1),
    )

    assert approval["decided_at"] == (NOW + timedelta(seconds=1)).isoformat()
    assert (tmp_path / "second.json").is_file()


def test_nonce_stays_consumed_after_output_failure(tmp_path: Path) -> None:
    occupied_output = tmp_path / "occupied.json"
    occupied_output.write_text("existing file", encoding="utf-8")
    with pytest.raises(FileExistsError):
        record_local_approval(
            _confirmation(), output_path=occupied_output, max_age=MAX_AGE, current_time=NOW
        )
    retry_output = tmp_path / "retry.json"

    with pytest.raises(LocalApprovalError, match="nonce has already been used"):
        record_local_approval(
            _confirmation(), output_path=retry_output, max_age=MAX_AGE, current_time=NOW
        )

    assert occupied_output.read_text(encoding="utf-8") == "existing file"
    assert not retry_output.exists()


def test_nonce_store_write_failure_prevents_issuance(tmp_path: Path, plugin_data_dir: Path) -> None:
    plugin_data_dir.mkdir()
    (plugin_data_dir / "local-approval-nonces").write_text("occupied", encoding="utf-8")
    output = tmp_path / "approval.json"

    with pytest.raises(FileExistsError):
        record_local_approval(
            _confirmation(), output_path=output, max_age=MAX_AGE, current_time=NOW
        )

    assert not output.exists()


def test_reused_confirmation_at_another_path_cannot_renew_expired_approval(tmp_path: Path) -> None:
    confirmed_at = NOW.replace(hour=9)
    confirmation = _confirmation(confirmed_at=confirmed_at.isoformat())
    first_output = tmp_path / "approval-morning.json"
    second_output = tmp_path / "approval-evening.json"
    record_local_approval(
        confirmation, output_path=first_output, max_age=MAX_AGE, current_time=confirmed_at
    )
    original = first_output.read_bytes()

    with pytest.raises(ApprovalValidationError, match="expired"):
        record_local_approval(
            confirmation,
            output_path=second_output,
            max_age=MAX_AGE,
            current_time=confirmed_at.replace(hour=18),
        )

    assert not second_output.exists()
    assert first_output.read_bytes() == original
    assert confirmation["confirmed_at"] == confirmed_at.isoformat()


def test_confirmation_without_timestamp_cannot_be_renewed_implicitly(tmp_path: Path) -> None:
    confirmation = _confirmation()
    confirmation.pop("confirmed_at")
    output = tmp_path / "approval.json"

    with pytest.raises(LocalApprovalError, match="confirmed_at"):
        record_local_approval(confirmation, output_path=output, max_age=MAX_AGE, current_time=NOW)

    assert not output.exists()


@pytest.mark.parametrize(
    ("confirmed_at", "error_match"),
    [
        ("not-a-date", "date-time"),
        ("2000-01-01T12:00:00", "date-time|timezone"),
        ("2000-01-01T12:00:01+00:00", "future"),
    ],
    ids=["invalid-format", "missing-timezone", "future-confirmation"],
)
def test_invalid_confirmation_timestamp_is_rejected_before_persistence(
    tmp_path: Path, confirmed_at: str, error_match: str
) -> None:
    output = tmp_path / "approval.json"

    with pytest.raises(ApprovalValidationError, match=error_match):
        record_local_approval(
            _confirmation(confirmed_at=confirmed_at),
            output_path=output,
            max_age=MAX_AGE,
            current_time=NOW,
        )

    assert not output.exists()


def _run_cli(
    tmp_path: Path, confirmation: object, *extra_arguments: str
) -> subprocess.CompletedProcess:
    tmp_path.mkdir(parents=True, exist_ok=True)
    request = tmp_path / "confirmation.json"
    request.write_text(json.dumps(confirmation), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(request),
            "--output",
            str(tmp_path / "approval.json"),
            *extra_arguments,
        ],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_documented_script_runs_outside_the_plugin_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    data_dir = tmp_path / "plugin data with spaces"
    request = tmp_path / "confirmation.json"
    output = tmp_path / "approval.json"
    confirmation = _confirmation(confirmed_at=datetime.now(UTC).isoformat())
    request.write_text(json.dumps(confirmation), encoding="utf-8")
    skill_path = PLUGIN_ROOT / "skills" / "remediate" / "SKILL.md"
    bash_blocks = re.findall(
        r"```bash\n(.*?)\n```", skill_path.read_text(encoding="utf-8"), re.DOTALL
    )
    helper_commands = [block for block in bash_blocks if "local_approval.py" in block]
    assert len(helper_commands) == 1
    command = helper_commands[0]
    replacements = {
        "${CLAUDE_PLUGIN_ROOT}": PLUGIN_ROOT.as_posix(),
        "${CLAUDE_PLUGIN_DATA}": data_dir.as_posix(),
        "<local-confirmation-json-path>": request.as_posix(),
        "<new-local-approval-json-path>": output.as_posix(),
        "<explicit-positive-lifetime-in-seconds>": "300",
    }
    for placeholder, value in replacements.items():
        command = command.replace(placeholder, value)
    completed = subprocess.run(
        shlex.split(command.replace("\\\n", "")),
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert output.is_file()
    assert (data_dir / "local-approval-nonces" / NONCE).is_file()
    approval = json.loads(completed.stdout)
    assert approval["decided_at"] == confirmation["confirmed_at"]
    assert approval["external_reference"] == request.resolve().as_uri()
    assert json.loads(output.read_text(encoding="utf-8")) == approval
    assert (
        validate_approval(
            approval,
            expected_incident_id=INCIDENT_ID,
            expected_diagnosis_hash=DIAGNOSIS_HASH,
            expected_base_commit=BASE_COMMIT,
            max_age=MAX_AGE,
        )
        == approval
    )


def test_cli_explicit_plugin_data_dir_takes_precedence_over_environment(
    tmp_path: Path, plugin_data_dir: Path
) -> None:
    data_dir = tmp_path / "explicit-plugin-data"
    confirmation = _confirmation(confirmed_at=datetime.now(UTC).isoformat())

    completed = _run_cli(
        tmp_path,
        confirmation,
        "--max-age-seconds",
        "300",
        "--plugin-data-dir",
        str(data_dir),
    )

    assert completed.returncode == 0, completed.stderr
    assert (data_dir / "local-approval-nonces" / NONCE).is_file()
    assert not plugin_data_dir.exists()


@pytest.mark.parametrize("data_dir", ["", "relative-plugin-data"])
def test_cli_explicit_plugin_data_dir_cannot_fall_back_when_relative(
    tmp_path: Path, plugin_data_dir: Path, data_dir: str
) -> None:
    confirmation = _confirmation(confirmed_at=datetime.now(UTC).isoformat())

    completed = _run_cli(
        tmp_path, confirmation, "--max-age-seconds", "300", "--plugin-data-dir", data_dir
    )

    assert completed.returncode == 2
    assert "must be an absolute path" in completed.stderr
    assert not (tmp_path / "approval.json").exists()
    assert not plugin_data_dir.exists()


@pytest.mark.parametrize("environment_present", [False, True])
def test_cli_requires_an_explicit_plugin_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment_present: bool
) -> None:
    if not environment_present:
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    confirmation = _confirmation(confirmed_at=datetime.now(UTC).isoformat())

    completed = _run_cli(tmp_path, confirmation, "--max-age-seconds", "300")

    assert completed.returncode == 2
    assert "--plugin-data-dir" in completed.stderr
    assert not (tmp_path / "approval.json").exists()


@pytest.mark.parametrize("arguments", [(), ("--max-age-seconds", "0")])
def test_cli_requires_an_explicit_positive_lifetime(
    tmp_path: Path, plugin_data_dir: Path, arguments: tuple[str, ...]
) -> None:
    completed = _run_cli(
        tmp_path, _confirmation(), "--plugin-data-dir", str(plugin_data_dir), *arguments
    )

    assert completed.returncode == 2
    assert "max-age-seconds" in completed.stderr
    assert not (tmp_path / "approval.json").exists()


def test_cli_refusal_finishes_without_creating_an_approval(
    tmp_path: Path, plugin_data_dir: Path
) -> None:
    completed = _run_cli(
        tmp_path,
        _confirmation(confirmed=False),
        "--max-age-seconds",
        "300",
        "--plugin-data-dir",
        str(plugin_data_dir),
    )

    assert completed.returncode == 2
    assert "explicit confirmation" in completed.stderr
    assert not (tmp_path / "approval.json").exists()


def test_cli_rejects_an_expired_confirmation(tmp_path: Path, plugin_data_dir: Path) -> None:
    confirmation = _confirmation(
        confirmed_at=(datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    )
    completed = _run_cli(
        tmp_path,
        confirmation,
        "--max-age-seconds",
        "300",
        "--plugin-data-dir",
        str(plugin_data_dir),
    )

    assert completed.returncode == 2
    assert "expired" in completed.stderr
    assert not (tmp_path / "approval.json").exists()


def test_cli_rejects_replay_in_another_process_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    data_dir = tmp_path / "persistent-plugin-data"
    confirmation = _confirmation(confirmed_at=datetime.now(UTC).isoformat())
    arguments = ("--max-age-seconds", "300", "--plugin-data-dir", str(data_dir))
    first = _run_cli(tmp_path / "first", confirmation, *arguments)
    assert first.returncode == 0, first.stderr
    first_output = tmp_path / "first" / "approval.json"
    original = first_output.read_bytes()

    second = _run_cli(tmp_path / "second", confirmation, *arguments)

    assert second.returncode == 2
    assert "nonce has already been used" in second.stderr
    assert not (tmp_path / "second" / "approval.json").exists()
    assert first_output.read_bytes() == original


def test_concurrent_cli_attempts_issue_only_one_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    data_dir = tmp_path / "persistent-plugin-data"
    confirmation = _confirmation(confirmed_at=datetime.now(UTC).isoformat())
    directories = [tmp_path / "first", tmp_path / "second"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [
            executor.submit(
                _run_cli,
                directory,
                confirmation,
                "--max-age-seconds",
                "300",
                "--plugin-data-dir",
                str(data_dir),
            )
            for directory in directories
        ]
        results = [attempt.result() for attempt in attempts]

    assert sorted(result.returncode for result in results) == [0, 2]
    rejected = next(result for result in results if result.returncode == 2)
    assert "nonce has already been used" in rejected.stderr
    assert sum((directory / "approval.json").exists() for directory in directories) == 1
