from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.patch_validator import (
    PatchValidationError,
)
from scripts.patch_validator import (
    validate_patch as _validate_patch,
)

sys.path.remove(str(PLUGIN_ROOT))


_BASE_COMMIT = "a" * 40
_MISSING = object()


def _applied_patch_for(patch_files: list[dict[str, Any]]) -> bytes:
    """Build a text diff whose contents agree with the declared fixture metadata."""
    merged: dict[str, dict[str, Any]] = {}
    for file_data in patch_files:
        path = file_data.get("path")
        lines_changed = file_data.get("lines_changed")
        changed_content = file_data.get("changed_content", _MISSING)
        if (
            not isinstance(path, str)
            or type(lines_changed) is not int
            or lines_changed < 0
            or (
                changed_content is not _MISSING and not isinstance(changed_content, str)
            )
        ):
            continue
        key = path.casefold()
        if key not in merged:
            merged[key] = {"path": path, "lines_changed": 0, "changed_lines": []}
        merged[key]["lines_changed"] += lines_changed
        if isinstance(changed_content, str):
            merged[key]["changed_lines"].extend(changed_content.splitlines())

    chunks: list[bytes] = []
    for file_data in merged.values():
        path = file_data["path"]
        changed_lines = file_data["changed_lines"]
        lines_changed = file_data["lines_changed"]
        chunk = bytearray(f"diff --git a/{path} b/{path}\n".encode())
        if lines_changed >= len(changed_lines):
            chunk.extend(b"@@ -1,1 +1,1 @@\n")
            for line in changed_lines:
                chunk.extend(f"+{line}\n".encode())
            for index in range(lines_changed - len(changed_lines)):
                chunk.extend(f"-fixture removed line {index}\n".encode())
        chunks.append(bytes(chunk))
    return b"".join(chunks) or b"diff --git a/.empty b/.empty\n"


def validate_patch(
    patch_files: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    verified_base_commit: object = _BASE_COMMIT,
    applied_patch: object = _MISSING,
) -> dict[str, Any]:
    """Validate fixtures against the approved base used by this test suite."""
    if applied_patch is _MISSING:
        applied_patch = _applied_patch_for(patch_files)
    return _validate_patch(
        patch_files,
        config,
        verified_base_commit=verified_base_commit,
        applied_patch=applied_patch,
    )


def _config(**overrides: object) -> dict[str, Any]:
    """Return a minimal valid config with all safety fields populated."""
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "pipeline": {"adapter": "dbt"},
        "runtime": {
            "execution_budget": {
                "max_tool_calls": 10,
                "max_seconds": 60,
                "max_tokens": 1000,
            }
        },
        "safety": {
            "editable_paths": ["models/**", "analyses/**"],
            "editable_extensions": [".sql", ".py"],
            "sensitive_paths": ["seeds/**", "migrations/**"],
            "limits": {
                "max_files": 5,
                "max_lines_per_file": 50,
                "max_total_lines": 100,
                "blocked_patterns": [],
            },
        },
    }
    if overrides:
        base["safety"] = {**base["safety"], **overrides}  # type: ignore[assignment]
    return base


def _file(
    path: str,
    lines_changed: object = 5,
    *,
    changed_content: object = "",
) -> dict[str, Any]:
    file_data: dict[str, Any] = {"path": path, "lines_changed": lines_changed}
    if changed_content is not _MISSING:
        file_data["changed_content"] = changed_content
    return file_data


def _git_diff_with_large_blocked_hunk(tmp_path: Path) -> bytes:
    """Create actual Git diff output containing 101 changed lines and blocked SQL."""
    repository = tmp_path / "repository"
    source = repository / "models" / "a.sql"
    source.parent.mkdir(parents=True)
    source.write_text("select baseline;\n" * 50, encoding="utf-8")

    for arguments in (
        ("init", "-q"),
        ("add", "models/a.sql"),
        ("commit", "-qm", "baseline"),
    ):
        completed = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Tests",
                "-c",
                "user.email=tests@example.invalid",
                *arguments,
            ],
            cwd=repository,
            check=False,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr.decode()

    source.write_text("drop table audit_log;\n" * 51, encoding="utf-8")
    completed = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--", "models/a.sql"],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    return completed.stdout


def _git_diff_with_cr_added_content(tmp_path: Path, content: bytes) -> bytes:
    """Create actual Git diff output whose one added record contains bare CRs."""
    repository = tmp_path / "repository"
    source = repository / "models" / "a.sql"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"select baseline;\n")

    for arguments in (
        ("init", "-q"),
        ("add", "models/a.sql"),
        ("commit", "-qm", "baseline"),
    ):
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "-c",
                "user.name=Tests",
                "-c",
                "user.email=tests@example.invalid",
                *arguments,
            ],
            cwd=repository,
            check=False,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr.decode()

    source.write_bytes(content + b"\n")
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "diff",
            "--no-ext-diff",
            "--",
            "models/a.sql",
        ],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert b"\r" in completed.stdout
    return completed.stdout


def _git_diff_with_staged_symlink(tmp_path: Path) -> bytes:
    """Create real staged Git diff bytes for a symlink without OS symlink privileges."""
    repository = tmp_path / "repository"
    repository.mkdir()
    commands = (
        ("init", "-q"),
        ("hash-object", "-w", "--stdin"),
    )
    completed = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *commands[0]],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode()

    completed = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *commands[1]],
        cwd=repository,
        check=False,
        capture_output=True,
        input=b"../migrations/0001_init.sql",
    )
    assert completed.returncode == 0, completed.stderr.decode()
    blob_id = completed.stdout.strip().decode()

    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{blob_id},models/link.sql",
        ],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode()

    completed = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "diff", "--cached", "--no-ext-diff"],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert b"new file mode 120000" in completed.stdout
    return completed.stdout


# --- mandatory scenarios ---


def test_valid_patch_within_limits_is_accepted() -> None:
    files = [_file("models/staging/stg_orders.sql", lines_changed=10)]
    result = validate_patch(files, _config())
    assert result["accepted"] is True
    assert result["violations"] == []
    assert result["requires_human_review"] is False


def test_accepted_patch_is_bound_to_its_base_commit_and_content_hash() -> None:
    files = [
        _file(
            "models/staging/stg_orders.sql",
            lines_changed=10,
            changed_content="select 1\n",
        )
    ]

    applied_patch = _applied_patch_for(files)
    result = validate_patch(
        files,
        _config(),
        verified_base_commit="A" * 40,
        applied_patch=applied_patch,
    )

    assert result["accepted"] is True
    assert result["base_commit"] == _BASE_COMMIT
    assert result["patch_hash"] == "sha256:" + hashlib.sha256(applied_patch).hexdigest()


def test_patch_hash_is_stable_for_identical_applied_patch_bytes() -> None:
    first = _file("models/a.sql", lines_changed=1, changed_content="select 1\n")
    second = _file("models/b.sql", lines_changed=1, changed_content="select 2\n")
    applied_patch = (
        b"diff --git a/models/a.sql b/models/a.sql\n@@ -1,1 +1,1 @@\n+select 1\n"
        b"diff --git a/models/b.sql b/models/b.sql\n@@ -1,1 +1,1 @@\n+select 2\n"
    )

    forward = validate_patch([first, second], _config(), applied_patch=applied_patch)
    retry = validate_patch([first, second], _config(), applied_patch=applied_patch)

    assert forward["patch_hash"] == retry["patch_hash"]


def test_patch_hash_changes_when_applied_patch_bytes_change() -> None:
    files = [_file("models/a.sql", changed_content="select 1\n")]
    original = validate_patch(
        files,
        _config(),
        applied_patch=(
            b"diff --git a/models/a.sql b/models/a.sql\n@@ -1,1 +1,1 @@\n+select 1\n"
            b"-fixture removed line 0\n-fixture removed line 1\n-fixture removed line 2\n"
            b"-fixture removed line 3\n"
        ),
    )
    revised = validate_patch(
        files,
        _config(),
        applied_patch=(
            b"diff --git a/models/a.sql b/models/a.sql\n@@ -2,1 +2,1 @@\n+select 1\n"
            b"-fixture removed line 0\n-fixture removed line 1\n-fixture removed line 2\n"
            b"-fixture removed line 3\n"
        ),
    )

    assert original["patch_hash"] != revised["patch_hash"]


@pytest.mark.parametrize("verified_base_commit", ["a" * 39, "g" * 40, None])
def test_patch_requires_a_complete_verified_hexadecimal_base_commit(
    verified_base_commit: object,
) -> None:
    with pytest.raises(PatchValidationError, match="base_commit"):
        validate_patch(
            [_file("models/a.sql")],
            _config(),
            verified_base_commit=verified_base_commit,
        )


@pytest.mark.parametrize("applied_patch", [None, b"", "diff --git a/a.sql b/a.sql"])
def test_patch_requires_exact_nonempty_applied_patch_bytes(
    applied_patch: object,
) -> None:
    with pytest.raises(PatchValidationError, match="applied_patch"):
        validate_patch([_file("models/a.sql")], _config(), applied_patch=applied_patch)


def test_applied_patch_paths_must_match_the_files_validated_by_safety_controls() -> (
    None
):
    result = validate_patch(
        [_file("models/a.sql", changed_content="select 1\n")],
        _config(),
        applied_patch=(
            b"diff --git a/migrations/0001_init.sql b/migrations/0001_init.sql\n"
            b"+create table audit_log ();\n"
        ),
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any(
        "applied_patch paths do not match" in violation
        for violation in result["violations"]
    )
    assert "base_commit" not in result
    assert "patch_hash" not in result


def test_applied_hunks_are_the_source_for_content_and_volume_controls(
    tmp_path: Path,
) -> None:
    applied_patch = _git_diff_with_large_blocked_hunk(tmp_path)
    result = validate_patch(
        [_file("models/a.sql", lines_changed=1, changed_content="select 1\n")],
        _config(
            limits={
                "max_files": 5,
                "max_lines_per_file": 100,
                "max_total_lines": 200,
                "blocked_patterns": [r"(?i)drop\s+table"],
            }
        ),
        applied_patch=applied_patch,
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any(
        "lines_changed does not match applied_patch" in v for v in result["violations"]
    )
    assert any(
        "changed_content does not match applied_patch" in v
        for v in result["violations"]
    )
    assert any("blocked_patterns" in v for v in result["violations"])
    assert any("max_lines_per_file" in v for v in result["violations"])
    assert "base_commit" not in result
    assert "patch_hash" not in result


def test_bare_cr_in_an_added_git_record_is_checked_against_blocked_patterns(
    tmp_path: Path,
) -> None:
    changed_content = "select 1\rdrop table audit_log;"
    applied_patch = _git_diff_with_cr_added_content(tmp_path, changed_content.encode())

    result = validate_patch(
        [_file("models/a.sql", lines_changed=2, changed_content=changed_content)],
        _config(
            limits={
                "max_files": 5,
                "max_lines_per_file": 50,
                "max_total_lines": 100,
                "blocked_patterns": [r"(?i)drop\s+table"],
            }
        ),
        applied_patch=applied_patch,
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any("blocked_patterns" in violation for violation in result["violations"])
    assert "base_commit" not in result
    assert "patch_hash" not in result


def test_bare_cr_segments_cannot_bypass_max_lines_per_file(tmp_path: Path) -> None:
    changed_content = "\r".join(f"select {index};" for index in range(101))
    applied_patch = _git_diff_with_cr_added_content(tmp_path, changed_content.encode())

    result = validate_patch(
        [_file("models/a.sql", lines_changed=101, changed_content=changed_content)],
        _config(
            limits={
                "max_files": 5,
                "max_lines_per_file": 100,
                "max_total_lines": 200,
                "blocked_patterns": [],
            }
        ),
        applied_patch=applied_patch,
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any("max_lines_per_file" in violation for violation in result["violations"])
    assert "base_commit" not in result
    assert "patch_hash" not in result


def test_bare_cr_does_not_turn_content_into_a_diff_header() -> None:
    result = validate_patch(
        [_file("models/a.sql", lines_changed=1, changed_content="select 1")],
        _config(),
        applied_patch=(
            b"untrusted content\rdiff --git a/models/a.sql b/models/a.sql\n"
            b"@@ -1,1 +1,1 @@\n+select 1\n"
        ),
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any(
        "must contain at least one diff --git header" in violation
        for violation in result["violations"]
    )
    assert "base_commit" not in result
    assert "patch_hash" not in result


def test_staged_symlink_to_sensitive_path_is_rejected_without_provenance(
    tmp_path: Path,
) -> None:
    applied_patch = _git_diff_with_staged_symlink(tmp_path)
    result = validate_patch(
        [
            _file(
                "models/link.sql",
                lines_changed=1,
                changed_content="../migrations/0001_init.sql\n",
            )
        ],
        _config(
            editable_paths=["models/**", "migrations/**"],
            sensitive_paths=["migrations"],
        ),
        applied_patch=applied_patch,
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any("symbolic link" in violation for violation in result["violations"])
    assert "base_commit" not in result
    assert "patch_hash" not in result


def test_regular_file_mode_change_is_rejected_without_provenance() -> None:
    result = validate_patch(
        [_file("models/a.sql", lines_changed=0, changed_content="")],
        _config(),
        applied_patch=(
            b"diff --git a/models/a.sql b/models/a.sql\nold mode 100644\nnew mode 100755\n"
        ),
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any(
        "mode changes are not allowed" in violation
        for violation in result["violations"]
    )
    assert "base_commit" not in result
    assert "patch_hash" not in result


def test_applied_patch_without_git_headers_is_rejected_without_provenance() -> None:
    result = validate_patch(
        [_file("models/a.sql", changed_content="select 1\n")],
        _config(),
        applied_patch=b"+select 1\n",
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any(
        "must contain at least one diff --git header" in v for v in result["violations"]
    )
    assert "base_commit" not in result
    assert "patch_hash" not in result


def test_quoted_diff_header_paths_are_reconciled_with_validated_files() -> None:
    result = validate_patch(
        [_file("models/customer orders.sql", changed_content="select 1\n")],
        _config(),
        applied_patch=(
            b'diff --git "a/models/customer orders.sql" "b/models/customer orders.sql"\n'
            b"@@ -1,1 +1,1 @@\n+select 1\n"
            b"-fixture removed line 0\n-fixture removed line 1\n-fixture removed line 2\n"
            b"-fixture removed line 3\n"
        ),
    )

    assert result["accepted"] is True
    assert result["requires_human_review"] is False


def test_patch_exceeding_file_limit_is_rejected() -> None:
    files = [_file(f"models/m{i}.sql") for i in range(10)]
    result = validate_patch(files, _config())
    assert result["accepted"] is False
    assert any("max_files" in v or "files" in v.lower() for v in result["violations"])
    assert result["requires_human_review"] is True


def test_patch_touching_sensitive_path_is_always_rejected() -> None:
    files = [
        _file("models/staging/stg_orders.sql", lines_changed=1),
        _file("seeds/raw_customers.csv", lines_changed=1),
    ]
    result = validate_patch(files, _config())
    assert result["accepted"] is False
    assert result["requires_human_review"] is True


# --- additional scenarios ---


def test_disallowed_extension_is_rejected() -> None:
    files = [_file("models/staging/stg_orders.csv", lines_changed=5)]
    result = validate_patch(files, _config())
    assert result["accepted"] is False
    assert any(".csv" in v for v in result["violations"])
    assert result["requires_human_review"] is True


def test_path_outside_editable_paths_is_rejected() -> None:
    files = [_file("macros/utils.sql", lines_changed=5)]
    result = validate_patch(files, _config())
    assert result["accepted"] is False
    assert any("macros/utils.sql" in v for v in result["violations"])
    assert result["requires_human_review"] is True


def test_empty_editable_paths_blocks_all_files() -> None:
    files = [_file("models/staging/stg_orders.sql", lines_changed=5)]
    result = validate_patch(files, _config(editable_paths=[]))
    assert result["accepted"] is False
    assert result["requires_human_review"] is True


def test_empty_editable_extensions_blocks_all_files() -> None:
    files = [_file("models/staging/stg_orders.sql", lines_changed=5)]
    result = validate_patch(files, _config(editable_extensions=[]))
    assert result["accepted"] is False
    assert result["requires_human_review"] is True


def test_empty_sensitive_paths_does_not_block() -> None:
    files = [_file("models/staging/stg_orders.sql", lines_changed=5)]
    result = validate_patch(files, _config(sensitive_paths=[]))
    assert result["accepted"] is True
    assert result["requires_human_review"] is False


def test_multiple_violations_all_appear_in_result() -> None:
    files = [
        _file("models/m0.sql", lines_changed=60),
        _file("models/m1.sql", lines_changed=60),
        _file("models/m2.sql", lines_changed=60),
    ]
    result = validate_patch(files, _config())
    assert result["accepted"] is False
    violation_text = " ".join(result["violations"])
    assert "max_lines_per_file" in violation_text or "50" in violation_text
    assert "max_total_lines" in violation_text or "100" in violation_text
    assert len(result["violations"]) >= 2
    assert result["requires_human_review"] is True


@pytest.mark.parametrize(
    ("lines_changed", "expected_accepted", "expected_review"),
    [
        (50, True, False),
        (51, False, True),
    ],
    ids=["at-limit", "over-limit"],
)
def test_max_lines_per_file_boundary(
    lines_changed: int, expected_accepted: bool, expected_review: bool
) -> None:
    files = [_file("models/staging/stg_orders.sql", lines_changed=lines_changed)]
    result = validate_patch(files, _config())
    assert result["accepted"] is expected_accepted
    assert result["requires_human_review"] is expected_review


def test_blocked_pattern_in_changed_content_is_rejected() -> None:
    files = [
        _file(
            "models/staging/stg_orders.sql",
            lines_changed=5,
            changed_content="password = 'do-not-commit'",
        )
    ]
    result = validate_patch(
        files,
        _config(
            limits={
                "max_files": 5,
                "max_lines_per_file": 50,
                "max_total_lines": 100,
                "blocked_patterns": [r"password\s*="],
            }
        ),
    )
    assert result["accepted"] is False
    assert any(
        "blocked_patterns" in v or "stg_orders" in v for v in result["violations"]
    )
    assert result["requires_human_review"] is True


def test_sensitive_path_forces_human_review_even_with_small_volume() -> None:
    files = [_file("migrations/0001_init.sql", lines_changed=1)]
    result = validate_patch(files, _config())
    assert result["accepted"] is False
    assert result["requires_human_review"] is True


def test_dot_prefix_path_matching_sensitive_is_rejected() -> None:
    files = [_file("./migrations/0001_init.sql", lines_changed=1)]
    result = validate_patch(files, _config())
    assert result["accepted"] is False
    assert result["requires_human_review"] is True


def test_dotdot_traversal_into_sensitive_path_is_rejected() -> None:
    with pytest.raises(PatchValidationError):
        validate_patch(
            [_file("models/../migrations/0001_init.sql", lines_changed=1)], _config()
        )


def test_absolute_path_is_rejected() -> None:
    with pytest.raises(PatchValidationError):
        validate_patch([_file("/migrations/0001_init.sql", lines_changed=1)], _config())


def test_case_variant_of_sensitive_path_is_rejected() -> None:
    files = [_file("Migrations/0001_init.sql", lines_changed=1)]
    result = validate_patch(files, _config())
    assert result["accepted"] is False
    assert result["requires_human_review"] is True


def test_case_insensitive_blocked_content_pattern_is_rejected() -> None:
    files = [
        _file(
            "models/staging/stg_orders.sql",
            lines_changed=5,
            changed_content="PASSWORD = 'do-not-commit'",
        )
    ]
    result = validate_patch(
        files,
        _config(
            limits={
                "max_files": 5,
                "max_lines_per_file": 50,
                "max_total_lines": 100,
                "blocked_patterns": [r"(?i)password\s*="],
            }
        ),
    )
    assert result["accepted"] is False
    assert any(
        "blocked_patterns" in v or "stg_orders" in v for v in result["violations"]
    )
    assert result["requires_human_review"] is True


def test_backslash_path_is_rejected() -> None:
    with pytest.raises(PatchValidationError):
        validate_patch([_file("C:\\migrations\\0001.sql", lines_changed=1)], _config())


def test_sensitive_path_blocked_by_control3_migrations() -> None:
    files = [_file("migrations/0001_init.sql", lines_changed=1)]
    result = validate_patch(
        files,
        _config(editable_paths=["**/*"], editable_extensions=[".sql", ".py", ".csv"]),
    )
    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any("sensitive_paths" in v for v in result["violations"])


def test_sensitive_path_case_insensitive_blocked_by_control3() -> None:
    files = [_file("Migrations/0001_init.sql", lines_changed=1)]
    result = validate_patch(
        files,
        _config(editable_paths=["**/*"], editable_extensions=[".sql", ".py", ".csv"]),
    )
    assert result["accepted"] is False
    assert result["requires_human_review"] is True


def test_sensitive_path_seeds_blocked_by_control3() -> None:
    files = [_file("seeds/raw_customers.csv", lines_changed=1)]
    result = validate_patch(
        files,
        _config(editable_paths=["**/*"], editable_extensions=[".sql", ".py", ".csv"]),
    )
    assert result["accepted"] is False
    assert result["requires_human_review"] is True


@pytest.mark.parametrize("sensitive_path", ["migrations", "migrations/"])
@pytest.mark.parametrize(
    "path",
    [
        "migrations/0001_init.sql",
        "migrations/sub/0001_init.sql",
    ],
)
def test_bare_sensitive_directory_blocks_all_descendants(
    sensitive_path: str,
    path: str,
) -> None:
    result = validate_patch(
        [_file(path, lines_changed=1)],
        _config(
            editable_paths=["**/*.sql"],
            editable_extensions=[".sql"],
            sensitive_paths=[sensitive_path],
        ),
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any("sensitive_paths" in violation for violation in result["violations"])


@pytest.mark.parametrize(
    ("sensitive_path", "path"),
    [
        ("**/seeds", "seeds/raw.csv"),
        ("*/seeds", "a/seeds/raw.csv"),
        ("models/seeds/*", "models/seeds/nested/raw.csv"),
    ],
)
def test_sensitive_glob_blocks_descendants(sensitive_path: str, path: str) -> None:
    result = validate_patch(
        [_file(path, lines_changed=1)],
        _config(
            editable_paths=["**/*.csv"],
            editable_extensions=[".csv"],
            sensitive_paths=[sensitive_path],
        ),
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any("sensitive_paths" in violation for violation in result["violations"])


def test_double_slash_absolute_path_is_rejected() -> None:
    with pytest.raises(PatchValidationError):
        validate_patch(
            [_file("//migrations/0001_init.sql", lines_changed=1)],
            _config(
                editable_paths=["**/*.sql"],
                editable_extensions=[".sql"],
                sensitive_paths=["migrations/**"],
            ),
        )


def test_negative_lines_changed_does_not_reduce_total() -> None:
    files = [_file(f"models/m{i}.sql", lines_changed=50) for i in range(4)] + [
        _file("models/m4.sql", lines_changed=-100)
    ]
    result = validate_patch(files, _config())
    assert result["accepted"] is False
    assert result["requires_human_review"] is True


def test_duplicate_paths_are_merged_before_volume_check() -> None:
    files = [
        _file("models/staging/stg_orders.sql", lines_changed=30),
        _file("models/staging/stg_orders.sql", lines_changed=30),
    ]
    result = validate_patch(files, _config())
    assert result["accepted"] is False
    assert any(
        "max_lines_per_file" in v or "stg_orders" in v for v in result["violations"]
    )


def test_case_variant_paths_are_merged_before_volume_check() -> None:
    files = [
        _file("models/a.sql", lines_changed=40),
        _file("Models/a.sql", lines_changed=40),
    ]

    result = validate_patch(files, _config())

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any(
        "file 'models/a.sql' has 80 changed lines" in violation
        for violation in result["violations"]
    )


def test_negative_duplicate_entry_cannot_cancel_a_positive_change() -> None:
    files = [
        _file("models/a.sql", lines_changed=200),
        _file("models/a.sql", lines_changed=-200),
    ]

    result = validate_patch(files, _config())

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any(
        "invalid lines_changed" in violation for violation in result["violations"]
    )
    assert any("max_lines_per_file" in violation for violation in result["violations"])


@pytest.mark.parametrize("lines_changed", ["5", True])
def test_invalid_lines_changed_is_a_violation_not_a_runtime_error(
    lines_changed: object,
) -> None:
    result = validate_patch(
        [_file("models/a.sql", lines_changed=lines_changed), _file("models/a.sql", 5)],
        _config(),
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any(
        "invalid lines_changed" in violation for violation in result["violations"]
    )


def test_single_segment_editable_glob_does_not_allow_nested_paths() -> None:
    result = validate_patch(
        [_file("models/private/credentials.sql", lines_changed=1)],
        _config(editable_paths=["models/*"]),
    )

    assert result["accepted"] is False
    assert any("editable_paths" in violation for violation in result["violations"])


@pytest.mark.parametrize(
    "path",
    [
        "migrations./0001_init.sql",
        "migrations /0001_init.sql",
    ],
)
def test_windows_ambiguous_sensitive_paths_are_rejected(path: str) -> None:
    result = validate_patch(
        [_file(path, lines_changed=1)],
        _config(
            editable_paths=["**/*.sql"],
            editable_extensions=[".sql"],
            sensitive_paths=["migrations/**"],
        ),
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any(
        "ambiguous on Windows" in violation for violation in result["violations"]
    )
    assert any("sensitive_paths" in violation for violation in result["violations"])


def test_blocked_patterns_require_changed_content() -> None:
    result = validate_patch(
        [_file("models/a.sql", lines_changed=1, changed_content=_MISSING)],
        _config(
            limits={
                "max_files": 5,
                "max_lines_per_file": 50,
                "max_total_lines": 100,
                "blocked_patterns": [r"password\s*="],
            }
        ),
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any("no changed_content" in violation for violation in result["violations"])


def test_missing_volume_limit_fails_closed() -> None:
    result = validate_patch(
        [_file("models/a.sql", lines_changed=1)],
        _config(
            limits={
                "max_files": 5,
                "max_lines_per_file": 50,
                "blocked_patterns": [],
            }
        ),
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any("max_total_lines" in violation for violation in result["violations"])


def test_invalid_blocked_pattern_fails_closed() -> None:
    result = validate_patch(
        [_file("models/a.sql", lines_changed=1, changed_content="select 1")],
        _config(
            limits={
                "max_files": 5,
                "max_lines_per_file": 50,
                "max_total_lines": 100,
                "blocked_patterns": ["["],
            }
        ),
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any("not a valid regex" in violation for violation in result["violations"])


def test_non_string_changed_content_fails_closed() -> None:
    result = validate_patch(
        [_file("models/a.sql", lines_changed=1, changed_content=object())],
        _config(),
    )

    assert result["accepted"] is False
    assert result["requires_human_review"] is True
    assert any(
        "invalid changed_content" in violation for violation in result["violations"]
    )


def test_rejected_patch_does_not_receive_provenance() -> None:
    result = validate_patch(
        [_file("migrations/0001.sql", changed_content="select 1\n")], _config()
    )

    assert result["accepted"] is False
    assert "base_commit" not in result
    assert "patch_hash" not in result
