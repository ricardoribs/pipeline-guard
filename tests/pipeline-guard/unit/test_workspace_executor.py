from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

import scripts.workspace_executor as workspace_executor  # noqa: E402
from scripts.workspace_executor import (  # noqa: E402
    ClonedWorkspace,
    WorkspaceExecutionError,
    WorkspaceInput,
    WorkspaceValidationError,
    clone_workspace,
    discard_prepared_workspace,
    prepare_incident_branch,
    prepare_workspace,
)

sys.path.remove(str(PLUGIN_ROOT))


def test_default_temp_directory_inside_a_worktree_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The default temp directory must not make the maintainer checkout dirty."""
    checkout = tmp_path / "maintainer-checkout"
    subprocess.run(
        ["git", "init", str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setattr(
        "scripts.workspace_executor.tempfile.gettempdir", lambda: str(checkout)
    )

    with pytest.raises(WorkspaceValidationError, match="parent_dir"):
        clone_workspace(
            tmp_path / "missing-config.yaml",
            base_commit="a" * 40,
            incident_id="INC-PARENT-001",
        )

    status = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


def test_parent_directory_check_fails_closed_when_git_cannot_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed Git probe must not permit a workspace in the primary checkout."""
    monkeypatch.setattr(
        workspace_executor.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )

    with pytest.raises(WorkspaceValidationError, match="could not verify") as captured:
        workspace_executor._reject_parent_inside_worktree(tmp_path)

    assert captured.value.field == "parent_dir"


def test_clone_timeout_preserves_cleanup_failure_as_a_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cleanup error supplements the clone timeout instead of being hidden by it."""
    inputs = WorkspaceInput(
        "https://example.com/repository.git",
        "main",
        "a" * 40,
        "pipeline-guard/incident-timeout",
    )
    original_close = ClonedWorkspace.close

    def fail_after_cleanup(self: ClonedWorkspace) -> None:
        original_close(self)
        raise WorkspaceExecutionError("cleanup failed")

    monkeypatch.setattr(
        workspace_executor, "_reject_parent_inside_worktree", lambda _parent: None
    )
    monkeypatch.setattr(
        workspace_executor, "validate_workspace_input", lambda *_args, **_kwargs: inputs
    )
    monkeypatch.setattr(
        workspace_executor, "_run_git_command", lambda *_args, **_kwargs: _timeout()
    )
    monkeypatch.setattr(ClonedWorkspace, "close", fail_after_cleanup)

    with pytest.raises(WorkspaceExecutionError, match="execution limit") as captured:
        clone_workspace(
            tmp_path / "config.yaml",
            base_commit="a" * 40,
            incident_id="INC-TIMEOUT-001",
            parent_dir=tmp_path,
        )

    assert captured.value.__notes__ == ["cleanup also failed: cleanup failed"]


def test_prepare_workspace_rejects_a_stale_approval(tmp_path: Path) -> None:
    """The configured branch moving after approval requires a new approval."""
    repository, approved_commit, config_path = _repository_with_config(tmp_path)
    (repository / "new-change.txt").write_text("new base", encoding="utf-8")
    _git(repository, "add", "new-change.txt")
    _git(repository, "commit", "-m", "advance base branch")

    with pytest.raises(
        WorkspaceValidationError, match="differs from the approved commit"
    ) as captured:
        prepare_workspace(
            config_path,
            base_commit=approved_commit,
            incident_id="INC-STALE-001",
            parent_dir=tmp_path,
        )

    assert captured.value.field == "base_commit"


def test_prepare_workspace_rejects_an_unknown_approved_commit(tmp_path: Path) -> None:
    """An approval cannot name a commit that the configured repository does not contain."""
    _, _, config_path = _repository_with_config(tmp_path)

    with pytest.raises(
        WorkspaceValidationError, match="Git could not verify"
    ) as captured:
        prepare_workspace(
            config_path,
            base_commit="f" * 40,
            incident_id="INC-UNKNOWN-001",
            parent_dir=tmp_path,
        )

    assert captured.value.field == "base_commit"


def test_prepare_workspace_creates_an_incident_branch_from_the_configured_prefix(
    tmp_path: Path,
) -> None:
    _, approved_commit, config_path = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id="INC-PIPELINE-001",
        parent_dir=tmp_path,
    ) as workspace:
        assert workspace.incident_branch == "pipeline-guard/INC-PIPELINE-001"
        assert _git(workspace.path, "branch", "--show-current").stdout.strip() == (
            "pipeline-guard/INC-PIPELINE-001"
        )
        assert (
            _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == approved_commit
        )


def test_prepare_workspace_uses_the_incident_source_branch(tmp_path: Path) -> None:
    repository, _, config_path = _repository_with_config(tmp_path)
    _git(repository, "switch", "-c", "feature/orders")
    (repository / "feature.txt").write_text("feature commit\n", encoding="utf-8")
    _git(repository, "add", "feature.txt")
    _git(repository, "commit", "-m", "test: add feature commit")
    feature_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()
    _git(repository, "switch", "main")

    with prepare_workspace(
        config_path,
        base_commit=feature_commit,
        base_branch="feature/orders",
        incident_id="INC-FEATURE-001",
        parent_dir=tmp_path,
    ) as workspace:
        assert workspace.inputs.base_branch == "feature/orders"
        assert (
            _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == feature_commit
        )


def test_prepare_workspace_preserves_work_in_an_incomplete_retained_clone(
    tmp_path: Path,
) -> None:
    """An incomplete retained clone is preserved until an operator discards it."""
    _, approved_commit, config_path = _repository_with_config(tmp_path)
    inputs = workspace_executor.validate_workspace_input(
        config_path,
        base_commit=approved_commit,
        incident_id="INC-INCOMPLETE-001",
    )
    retained_directory = workspace_executor._retained_workspace_directory(
        inputs, tmp_path
    )
    repository_path = retained_directory / "repository"
    (repository_path / ".git").mkdir(parents=True)
    uncommitted_file = repository_path / "uncommitted.txt"
    uncommitted_file.write_text("keep me\n", encoding="utf-8")

    with pytest.raises(
        WorkspaceValidationError,
        match="retained Git repository metadata is damaged",
    ) as captured:
        prepare_workspace(
            config_path,
            base_commit=approved_commit,
            incident_id="INC-INCOMPLETE-001",
            parent_dir=tmp_path,
        )

    assert "discard_prepared_workspace" in captured.value.correction
    assert uncommitted_file.read_text(encoding="utf-8") == "keep me\n"
    assert (repository_path / ".git").is_dir()
    assert discard_prepared_workspace(
        config_path,
        incident_id="INC-INCOMPLETE-001",
        parent_dir=tmp_path,
        confirm_unpushed_commits=True,
    )


def test_prepare_workspace_preserves_work_in_a_retained_clone_with_an_invalid_head(
    tmp_path: Path,
) -> None:
    """A corrupt HEAD refuses retry without deleting committed or uncommitted work."""
    _, approved_commit, config_path = _repository_with_config(tmp_path)
    incident_id = "INC-CORRUPT-HEAD-001"

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id=incident_id,
        parent_dir=tmp_path,
    ) as first_workspace:
        retained_path = first_workspace.path
        _git(first_workspace.path, "config", "user.email", "pipeline-guard@example.com")
        _git(first_workspace.path, "config", "user.name", "Pipeline Guard Tests")
        (first_workspace.path / "remediation.txt").write_text(
            "committed\n", encoding="utf-8"
        )
        _git(first_workspace.path, "add", "remediation.txt")
        _git(first_workspace.path, "commit", "-m", "add remediation")
        remediation_commit = _git(
            first_workspace.path, "rev-parse", "HEAD"
        ).stdout.strip()
        (first_workspace.path / "uncommitted.txt").write_text(
            "keep me\n", encoding="utf-8"
        )

    (retained_path / ".git" / "HEAD").write_text("garbage\n", encoding="utf-8")

    with pytest.raises(
        WorkspaceValidationError,
        match="retained Git repository metadata is damaged or cannot be verified",
    ) as captured:
        prepare_workspace(
            config_path,
            base_commit=approved_commit,
            incident_id=incident_id,
            parent_dir=tmp_path,
        )

    assert "discard_prepared_workspace" in captured.value.correction
    assert retained_path.is_dir()
    assert (retained_path / "uncommitted.txt").read_text(
        encoding="utf-8"
    ) == "keep me\n"
    branch_ref = (
        retained_path
        / ".git"
        / "refs"
        / "heads"
        / "pipeline-guard"
        / "INC-CORRUPT-HEAD-001"
    )
    assert branch_ref.read_text(encoding="utf-8").strip() == remediation_commit
    (retained_path / ".git" / "HEAD").write_text(
        "ref: refs/heads/pipeline-guard/INC-CORRUPT-HEAD-001\n",
        encoding="utf-8",
    )
    assert (
        _git(
            retained_path, "cat-file", "-e", f"{remediation_commit}^{{commit}}"
        ).returncode
        == 0
    )
    assert discard_prepared_workspace(
        config_path,
        incident_id=incident_id,
        parent_dir=tmp_path,
        confirm_unpushed_commits=True,
    )


def test_prepare_workspace_preserves_uncommitted_work_when_retained_head_is_missing(
    tmp_path: Path,
) -> None:
    """A missing HEAD does not authorize automatic deletion of retained files."""
    _, approved_commit, config_path = _repository_with_config(tmp_path)
    incident_id = "INC-MISSING-HEAD-001"

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id=incident_id,
        parent_dir=tmp_path,
    ) as workspace:
        retained_path = workspace.path
        pending_file = retained_path / "uncommitted.txt"
        pending_file.write_text("preserve this work\n", encoding="utf-8")

    (retained_path / ".git" / "HEAD").unlink()

    with pytest.raises(
        WorkspaceValidationError,
        match="retained Git repository metadata is damaged",
    ) as captured:
        prepare_workspace(
            config_path,
            base_commit=approved_commit,
            incident_id=incident_id,
            parent_dir=tmp_path,
        )

    assert "discard_prepared_workspace" in captured.value.correction
    assert pending_file.read_text(encoding="utf-8") == "preserve this work\n"
    assert discard_prepared_workspace(
        config_path,
        incident_id=incident_id,
        parent_dir=tmp_path,
        confirm_unpushed_commits=True,
    )


def test_prepare_workspace_reuses_an_incident_branch_across_calls(
    tmp_path: Path,
) -> None:
    """A retry restores the branch after failure via the public preparation entry point."""
    _, approved_commit, config_path = _repository_with_config(tmp_path)
    first_path: Path | None = None
    branch: str | None = None
    first_workspace_obj: ClonedWorkspace | None = None

    def fail_after_branch_creation() -> None:
        nonlocal first_path, branch, first_workspace_obj
        with prepare_workspace(
            config_path,
            base_commit=approved_commit,
            incident_id="INC-REUSE-001",
            parent_dir=tmp_path,
        ) as first_workspace:
            first_path = first_workspace.path
            branch = first_workspace.incident_branch
            first_workspace_obj = first_workspace
            raise RuntimeError("simulated failure after branch creation")

    with pytest.raises(RuntimeError, match="simulated failure after branch creation"):
        fail_after_branch_creation()

    assert first_path is not None
    assert branch == "pipeline-guard/INC-REUSE-001"
    assert first_workspace_obj is not None
    assert first_path.is_dir()

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id="INC-REUSE-001",
        parent_dir=tmp_path,
    ) as retried_workspace:
        assert retried_workspace.path == first_path
        assert retried_workspace.incident_branch == branch
        assert (
            _git(retried_workspace.path, "branch", "--show-current").stdout.strip()
            == branch
        )
        assert (
            _git(retried_workspace.path, "rev-parse", "HEAD").stdout.strip()
            == approved_commit
        )
        assert _git(
            retried_workspace.path,
            "branch",
            "--format=%(refname:short)",
            "--list",
            "pipeline-guard/*",
        ).stdout.split() == [branch]

    first_workspace_obj.discard()


def test_prepare_workspace_resumes_one_clean_unpushed_remediation_commit(
    tmp_path: Path,
) -> None:
    """A fresh process may retry a single validated commit that has not been pushed yet."""
    _, approved_commit, config_path = _repository_with_config(tmp_path)
    incident_id = "INC-RESUME-COMMIT"

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id=incident_id,
        parent_dir=tmp_path,
    ) as workspace:
        _git(workspace.path, "config", "user.email", "pipeline-guard@example.com")
        _git(workspace.path, "config", "user.name", "Pipeline Guard Tests")
        (workspace.path / "remediation.txt").write_text("retry me\n", encoding="utf-8")
        _git(workspace.path, "add", "remediation.txt")
        _git(
            workspace.path,
            "commit",
            "-m",
            "fix(pipeline-guard): remediate pipeline-guard/INC-RESUME-COMMIT",
        )
        remediation_commit = _git(workspace.path, "rev-parse", "HEAD").stdout.strip()

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id=incident_id,
        parent_dir=tmp_path,
    ) as resumed:
        assert resumed.verified_base_commit == approved_commit
        assert resumed.incident_branch == "pipeline-guard/INC-RESUME-COMMIT"
        assert (
            _git(resumed.path, "rev-parse", "HEAD").stdout.strip() == remediation_commit
        )

    resumed.discard(confirm_unpushed_commits=True)


def test_prepare_workspace_refuses_concurrent_preparation_for_the_same_incident(
    tmp_path: Path,
) -> None:
    """An open workspace claim prevents overlapping preparation or discard."""
    _, approved_commit, config_path = _repository_with_config(tmp_path)
    incident_id = "INC-CONCURRENT-001"

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id=incident_id,
        parent_dir=tmp_path,
    ) as active_workspace:
        retained_path = active_workspace.path
        with pytest.raises(WorkspaceExecutionError, match="already active"):
            prepare_workspace(
                config_path,
                base_commit=approved_commit,
                incident_id=incident_id,
                parent_dir=tmp_path,
            )
        with pytest.raises(WorkspaceExecutionError, match="already active"):
            discard_prepared_workspace(
                config_path,
                incident_id=incident_id,
                parent_dir=tmp_path,
            )
        assert retained_path.is_dir()
        assert (
            _git(retained_path, "rev-parse", "HEAD").stdout.strip() == approved_commit
        )

    assert discard_prepared_workspace(
        config_path,
        incident_id=incident_id,
        parent_dir=tmp_path,
    )


def test_prepare_workspace_rejects_a_retained_diverged_branch(tmp_path: Path) -> None:
    """The public entry point refuses a retained branch that moved off its approved base."""
    repository, approved_commit, config_path = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id="INC-REUSE-DIVERGED",
        parent_dir=tmp_path,
    ) as workspace:
        _git(workspace.path, "config", "user.email", "pipeline-guard@example.com")
        _git(workspace.path, "config", "user.name", "Pipeline Guard Tests")
        (workspace.path / "diverged.txt").write_text("diverged", encoding="utf-8")
        _git(workspace.path, "add", "diverged.txt")
        _git(workspace.path, "commit", "-m", "diverge incident branch")
        retained_path = workspace.path

    with pytest.raises(
        WorkspaceValidationError,
        match="older approved base",
    ) as captured:
        prepare_workspace(
            config_path,
            base_commit=approved_commit,
            incident_id="INC-REUSE-DIVERGED",
            parent_dir=tmp_path,
        )

    assert captured.value.field == "incident_id"
    assert retained_path.is_dir()
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == approved_commit
    workspace.discard(confirm_unpushed_commits=True)


def test_prepare_workspace_rejects_a_retained_dirty_checkout(tmp_path: Path) -> None:
    """The public entry point preserves and rejects an uncommitted retained checkout."""
    _, approved_commit, config_path = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id="INC-REUSE-DIRTY",
        parent_dir=tmp_path,
    ) as workspace:
        (workspace.path / "uncommitted-change.txt").write_text(
            "pending", encoding="utf-8"
        )
        retained_path = workspace.path

    with pytest.raises(
        WorkspaceValidationError,
        match="retained incident workspace has uncommitted changes",
    ) as captured:
        prepare_workspace(
            config_path,
            base_commit=approved_commit,
            incident_id="INC-REUSE-DIRTY",
            parent_dir=tmp_path,
        )

    assert captured.value.field == "workspace"
    assert (retained_path / "uncommitted-change.txt").is_file()
    assert "discard_prepared_workspace" in captured.value.correction
    assert discard_prepared_workspace(
        config_path,
        incident_id="INC-REUSE-DIRTY",
        parent_dir=tmp_path,
    )
    assert not retained_path.exists()
    assert not discard_prepared_workspace(
        config_path,
        incident_id="INC-REUSE-DIRTY",
        parent_dir=tmp_path,
    )

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id="INC-REUSE-DIRTY",
        parent_dir=tmp_path,
    ) as retried_workspace:
        assert _git(retried_workspace.path, "status", "--porcelain").stdout == ""

    retried_workspace.discard()


def test_discard_prepared_workspace_requires_confirmation_for_unpushed_commits(
    tmp_path: Path,
) -> None:
    """Unpushed remediation commits remain available until deletion is explicitly confirmed."""
    _, approved_commit, config_path = _repository_with_config(tmp_path)
    incident_id = "INC-DISCARD-UNPUSHED"

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id=incident_id,
        parent_dir=tmp_path,
    ) as workspace:
        retained_directory = workspace._directory
        _git(workspace.path, "config", "user.email", "pipeline-guard@example.com")
        _git(workspace.path, "config", "user.name", "Pipeline Guard Tests")
        (workspace.path / "remediation.txt").write_text(
            "keep this commit\n", encoding="utf-8"
        )
        _git(workspace.path, "add", "remediation.txt")
        _git(workspace.path, "commit", "-m", "add remediation")
        remediation_commit = _git(workspace.path, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(
        WorkspaceExecutionError, match="not present on its approved base"
    ):
        discard_prepared_workspace(
            config_path,
            incident_id=incident_id,
            parent_dir=tmp_path,
        )

    assert retained_directory.is_dir()
    assert _git(
        retained_directory / "repository", "rev-parse", "HEAD"
    ).stdout.strip() == (remediation_commit)
    assert discard_prepared_workspace(
        config_path,
        incident_id=incident_id,
        parent_dir=tmp_path,
        confirm_unpushed_commits=True,
    )
    assert not retained_directory.exists()


def test_discard_prepared_workspace_allows_commits_pushed_to_origin(
    tmp_path: Path,
) -> None:
    """A commit fetched from any origin branch is safe to discard from the local cache."""
    _, approved_commit, config_path = _repository_with_config(tmp_path)
    incident_id = "INC-DISCARD-PUSHED"

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id=incident_id,
        parent_dir=tmp_path,
    ) as workspace:
        branch = workspace.incident_branch
        assert branch is not None
        _git(workspace.path, "config", "user.email", "pipeline-guard@example.com")
        _git(workspace.path, "config", "user.name", "Pipeline Guard Tests")
        (workspace.path / "remediation.txt").write_text(
            "pushed commit\n", encoding="utf-8"
        )
        _git(workspace.path, "add", "remediation.txt")
        _git(workspace.path, "commit", "-m", "add remediation")
        _git(workspace.path, "push", "origin", f"{branch}:refs/heads/{branch}")

    assert discard_prepared_workspace(
        config_path,
        incident_id=incident_id,
        parent_dir=tmp_path,
    )


def test_prepare_workspace_fetches_base_and_audits_an_explicit_safe_rebind(
    tmp_path: Path,
) -> None:
    """A fresh approval follows the live base and safely rebinds an unchanged branch."""
    repository, approved_a, config_path = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=approved_a,
        incident_id="INC-MOVE-001",
        parent_dir=tmp_path,
    ) as first_workspace:
        retained_path = first_workspace.path

    (repository / "base-b.txt").write_text("new approved base", encoding="utf-8")
    _git(repository, "add", "base-b.txt")
    _git(repository, "commit", "-m", "advance base branch")
    approved_b = _git(repository, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(
        WorkspaceValidationError, match="base branch differs from the approved"
    ):
        prepare_workspace(
            config_path,
            base_commit=approved_a,
            incident_id="INC-MOVE-001",
            parent_dir=tmp_path,
        )

    with pytest.raises(
        WorkspaceValidationError, match="older approved base"
    ) as captured:
        prepare_workspace(
            config_path,
            base_commit=approved_b,
            incident_id="INC-MOVE-001",
            parent_dir=tmp_path,
        )

    assert "allow_branch_rebind=True" in captured.value.correction

    with prepare_workspace(
        config_path,
        base_commit=approved_b,
        incident_id="INC-MOVE-001",
        parent_dir=tmp_path,
        allow_branch_rebind=True,
    ) as rebound_workspace:
        assert rebound_workspace.path == retained_path
        assert (
            _git(rebound_workspace.path, "rev-parse", "HEAD").stdout.strip()
            == approved_b
        )
        audit = rebound_workspace.branch_rebind_audit
        assert audit is not None
        assert audit.previous_approved_base == approved_a
        assert audit.new_approved_base == approved_b
        audit_directory = workspace_executor._branch_rebind_audit_directory(
            rebound_workspace
        )
        assert audit_directory.parent == rebound_workspace._directory.parent
        assert audit_directory != rebound_workspace._directory
        audit_files = list(audit_directory.glob("*.json"))
        assert len(audit_files) == 2
        assert {
            json.loads(path.read_text(encoding="utf-8"))["event"]
            for path in audit_files
        } == {
            "requested",
            "completed",
        }

    first_workspace.discard()
    assert not retained_path.parent.exists()
    assert audit_directory.is_dir()
    assert len(list(audit_directory.glob("*.json"))) == 2


def test_prepare_workspace_refuses_to_rebind_a_branch_with_extra_commits(
    tmp_path: Path,
) -> None:
    """Explicit rebind cannot discard commits made after the prior approved base."""
    repository, approved_a, config_path = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=approved_a,
        incident_id="INC-MOVE-DIVERGED",
        parent_dir=tmp_path,
    ) as workspace:
        _git(workspace.path, "config", "user.email", "pipeline-guard@example.com")
        _git(workspace.path, "config", "user.name", "Pipeline Guard Tests")
        (workspace.path / "extra-commit.txt").write_text(
            "keep this work", encoding="utf-8"
        )
        _git(workspace.path, "add", "extra-commit.txt")
        _git(workspace.path, "commit", "-m", "add incident work")

    (repository / "base-b.txt").write_text("new approved base", encoding="utf-8")
    _git(repository, "add", "base-b.txt")
    _git(repository, "commit", "-m", "advance base branch")
    approved_b = _git(repository, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(
        WorkspaceValidationError,
        match="commits beyond its previous approved base",
    ):
        prepare_workspace(
            config_path,
            base_commit=approved_b,
            incident_id="INC-MOVE-DIVERGED",
            parent_dir=tmp_path,
            allow_branch_rebind=True,
        )

    workspace.discard(confirm_unpushed_commits=True)


def test_prepare_incident_branch_reuses_the_existing_branch_for_the_same_incident(
    tmp_path: Path,
) -> None:
    _, approved_commit, config_path = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id="INC-PIPELINE-002",
        parent_dir=tmp_path,
    ) as workspace:
        branch = workspace.incident_branch
        assert branch is not None
        _git(workspace.path, "switch", "--detach", approved_commit)

        assert prepare_incident_branch(workspace) == branch
        assert _git(
            workspace.path,
            "branch",
            "--format=%(refname:short)",
            "--list",
            "pipeline-guard/*",
        ).stdout.split() == [branch]
        assert (
            _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == approved_commit
        )


def test_prepare_incident_branch_rejects_an_existing_branch_at_a_different_base_commit(
    tmp_path: Path,
) -> None:
    repository, _, config_path = _repository_with_config(tmp_path)
    (repository / "second-base.txt").write_text("second base", encoding="utf-8")
    _git(repository, "add", "second-base.txt")
    _git(repository, "commit", "-m", "second approved base")
    approved_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id="INC-PIPELINE-003",
        parent_dir=tmp_path,
    ) as workspace:
        branch = workspace.incident_branch
        assert branch is not None
        _git(workspace.path, "switch", "--detach", approved_commit)
        _git(workspace.path, "branch", "-f", branch, f"{approved_commit}^")

        with pytest.raises(
            WorkspaceValidationError,
            match="older approved base",
        ) as captured:
            prepare_incident_branch(workspace)

    assert captured.value.field == "incident_id"


def test_prepare_incident_branch_rejects_an_existing_dirty_branch(
    tmp_path: Path,
) -> None:
    _, approved_commit, config_path = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id="INC-PIPELINE-004",
        parent_dir=tmp_path,
    ) as workspace:
        (workspace.path / "uncommitted-change.txt").write_text(
            "pending", encoding="utf-8"
        )

        with pytest.raises(
            WorkspaceValidationError,
            match="existing incident branch has uncommitted changes",
        ) as captured:
            prepare_incident_branch(workspace)

    assert captured.value.field == "workspace"


def test_prepare_workspace_encodes_an_incident_id_that_is_not_git_safe(
    tmp_path: Path,
) -> None:
    _, approved_commit, config_path = _repository_with_config(tmp_path)

    with prepare_workspace(
        config_path,
        base_commit=approved_commit,
        incident_id="INC/PIPE:01",
        parent_dir=tmp_path,
    ) as workspace:
        assert workspace.incident_branch == "pipeline-guard/INC%2FPIPE%3A01"


def test_prepare_workspace_requires_a_configured_remediation_branch_prefix(
    tmp_path: Path,
) -> None:
    _, approved_commit, config_path = _repository_with_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            '  remediation_branch_prefix: "pipeline-guard"\n',
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        WorkspaceValidationError,
        match="remediation_branch_prefix",
    ) as captured:
        prepare_workspace(
            config_path,
            base_commit=approved_commit,
            incident_id="INC-PREFIX-001",
            parent_dir=tmp_path,
        )

    assert captured.value.field == "$.repository.remediation_branch_prefix"


def test_authorship_environment_excludes_credentials_and_disables_global_git_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The author receives only an allowlisted environment and inert Git configuration."""
    monkeypatch.setenv("PIPELINE_GUARD_TEST_SECRET", "do-not-expose")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-expose")
    workspace = ClonedWorkspace(
        WorkspaceInput(
            "https://example.com/repository.git",
            "main",
            "a" * 40,
            "pipeline-guard/incident-environment",
        ),
        parent_dir=tmp_path,
    )
    workspace._verified_base_commit = "a" * 40

    try:
        environment = workspace.authorship_environment()
    finally:
        workspace.close()

    assert "PIPELINE_GUARD_TEST_SECRET" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_isolated_supervisor_does_not_import_subprocess_from_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The supervisor must not load a malicious subprocess.py from the caller's checkout."""
    marker = tmp_path / "subprocess-imported"
    (tmp_path / "subprocess.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    environment = {**os.environ, "PYTHONPATH": str(tmp_path)}

    completed = workspace_executor._run_git_command(
        ["git", "--version"],
        environment=environment,
        timeout=10,
        capture_stdout=True,
    )

    assert completed.returncode == 0
    assert not marker.exists()


def _repository_with_config(tmp_path: Path) -> tuple[Path, str, Path]:
    repository = tmp_path / "source-repository"
    _git(tmp_path, "init", "--initial-branch=main", str(repository))
    _git(repository, "config", "user.email", "pipeline-guard@example.com")
    _git(repository, "config", "user.name", "Pipeline Guard Tests")
    (repository / "base.txt").write_text("approved base", encoding="utf-8")
    _git(repository, "add", "base.txt")
    _git(repository, "commit", "-m", "approved base")
    approved_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()
    config_path = tmp_path / "project.yaml"
    config_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "pipeline:",
                '  adapter: "mock"',
                "repository:",
                f'  url: "{repository.as_posix()}"',
                '  default_branch: "main"',
                '  remediation_branch_prefix: "pipeline-guard"',
                "runtime:",
                "  execution_budget:",
                "    max_tool_calls: 1",
                "    max_seconds: 1",
                "    max_tokens: 1",
                "",
            )
        ),
        encoding="utf-8",
    )
    return repository, approved_commit, config_path


def _git(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _timeout() -> None:
    raise subprocess.TimeoutExpired(["git", "clone"], timeout=120)
