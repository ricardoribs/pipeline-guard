# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0", "pyyaml>=6.0.3"]
# ///
"""Create isolated clones and verify their approved base before preparing working files."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import quote_from_bytes, urlsplit

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from scripts.validate_config import validate_config

_CLONE_TIMEOUT_SECONDS = 120
_WORKSPACE_GIT_TIMEOUT_SECONDS = 30
_PROCESS_CLEANUP_SECONDS = 5
_RETAINED_DISCARD_CORRECTION = (
    "review or preserve retained changes, or call discard_prepared_workspace with the same "
    "config_path, incident_id, and parent_dir before retrying; committed work that is not on "
    "the approved base or a pushed origin branch requires confirm_unpushed_commits=True"
)
_UNPUSHED_DISCARD_CORRECTION = (
    "preserve or push the unpushed commits, or call discard_prepared_workspace with "
    "confirm_unpushed_commits=True after reviewing that their loss is acceptable"
)
_AUTHOR_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
    }
)


class WorkspaceValidationError(ValueError):
    """Workspace input error with a field and an actionable correction."""

    def __init__(self, field: str, message: str, correction: str) -> None:
        self.field = field
        self.correction = correction
        super().__init__(
            f"Invalid workspace input at {field}: {message}. Correction: {correction}"
        )


class WorkspaceExecutionError(RuntimeError):
    """Raised when cloning or cleanup cannot be completed safely."""


@dataclass(frozen=True)
class WorkspaceInput:
    """Validated input fields; repository and commit checks still need Git access."""

    repository_url: str
    base_branch: str
    base_commit: str
    incident_branch: str
    incident_id: str = ""


@dataclass(frozen=True)
class BranchRebindAudit:
    """Audit details stored outside the disposable incident-workspace cache."""

    incident_id: str
    branch: str
    previous_approved_base: str
    new_approved_base: str
    recorded_at_ns: int


_ACTIVE_WORKSPACE_CLAIMS: set[Path] = set()
_ACTIVE_WORKSPACE_CLAIMS_GUARD = threading.Lock()


class _RetainedWorkspaceClaim:
    """Hold an OS lock that gives one process exclusive access to an incident cache."""

    def __init__(self, workspace_dir: Path) -> None:
        self.path = workspace_dir.with_name(f"{workspace_dir.name}.lock")
        self._descriptor: int | None = None

    def acquire(self) -> None:
        normalized_path = self.path.resolve()
        with _ACTIVE_WORKSPACE_CLAIMS_GUARD:
            if normalized_path in _ACTIVE_WORKSPACE_CLAIMS:
                raise WorkspaceExecutionError(
                    "A preparation for this incident is already active"
                )
            _ACTIVE_WORKSPACE_CLAIMS.add(normalized_path)

        descriptor: int | None = None
        try:
            if self.path.is_symlink():
                raise WorkspaceExecutionError(
                    "Refusing to use a replaced workspace claim file"
                )
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            descriptor = os.open(self.path, flags, 0o600)
            information = os.fstat(descriptor)
            if not stat.S_ISREG(information.st_mode):
                raise WorkspaceExecutionError("Workspace claim is not a regular file")
            if os.name != "nt":
                if information.st_uid != os.getuid():
                    raise WorkspaceExecutionError(
                        "Workspace claim has a different owner"
                    )
                if information.st_mode & 0o077:
                    os.fchmod(descriptor, 0o600)
            if information.st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            with _ACTIVE_WORKSPACE_CLAIMS_GUARD:
                _ACTIVE_WORKSPACE_CLAIMS.discard(normalized_path)
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise WorkspaceExecutionError(
                    "A preparation for this incident is already active"
                ) from None
            raise WorkspaceExecutionError(
                "Could not acquire the exclusive claim for the retained workspace"
            ) from None
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            with _ACTIVE_WORKSPACE_CLAIMS_GUARD:
                _ACTIVE_WORKSPACE_CLAIMS.discard(normalized_path)
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        normalized_path = self.path.resolve()
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            raise WorkspaceExecutionError(
                "Could not release the retained workspace claim"
            ) from None
        finally:
            os.close(descriptor)
            with _ACTIVE_WORKSPACE_CLAIMS_GUARD:
                _ACTIVE_WORKSPACE_CLAIMS.discard(normalized_path)


class ClonedWorkspace:
    """Own an isolated clone until its caller closes or discards the workspace.

    Workspaces returned by ``prepare_workspace`` are retained by repository and
    incident so a retry can validate and reuse the same local branch. An
    exclusive process-held claim prevents overlapping access until ``close``
    or ``discard``. Call ``discard`` when that retained state is no longer
    needed. Clones returned by ``clone_workspace`` remain disposable.
    """

    def __init__(
        self,
        inputs: WorkspaceInput,
        *,
        parent_dir: Path | None = None,
        workspace_dir: Path | None = None,
        reuse_existing: bool = False,
    ) -> None:
        self.inputs = inputs
        self._retained = workspace_dir is not None
        if workspace_dir is None:
            self._directory = Path(
                tempfile.mkdtemp(prefix="pipeline-guard-workspace-", dir=parent_dir)
            ).resolve()
        else:
            self._directory = workspace_dir
            if self._directory.is_symlink():
                raise WorkspaceExecutionError(
                    "Refusing to use a replaced workspace directory"
                )
            try:
                self._directory.mkdir(
                    mode=0o700,
                    parents=False,
                    exist_ok=reuse_existing,
                )
            except OSError:
                raise WorkspaceExecutionError(
                    "Could not claim the retained workspace directory"
                ) from None
            if (
                not self._directory.is_dir()
                or self._directory.resolve() != self._directory
            ):
                raise WorkspaceExecutionError(
                    "Refusing to use a replaced workspace directory"
                )
        self._parent = self._directory.parent
        self._closed = False
        self._reused = reuse_existing
        self._verified_base_commit: str | None = None
        self._incident_branch: str | None = None
        self._previous_approved_base: str | None = None
        self._branch_rebind_audit: BranchRebindAudit | None = None
        self._retained_directory: Path | None = None
        self._claim: _RetainedWorkspaceClaim | None = None

    @property
    def path(self) -> Path:
        """Return the owned clone path; prepare_workspace verifies it before authorship."""
        return self._directory / "repository"

    @property
    def verified_base_commit(self) -> str | None:
        """Return the verified commit, or None for a clone not yet prepared."""
        return self._verified_base_commit

    @property
    def incident_branch(self) -> str | None:
        """Return the prepared incident branch, or None before workspace preparation."""
        return self._incident_branch

    @property
    def branch_rebind_audit(self) -> BranchRebindAudit | None:
        """Return the rebind record created by this preparation, if any."""
        return self._branch_rebind_audit

    def authorship_environment(self) -> dict[str, str]:
        """Return a minimal environment for authorship in a verified workspace.

        The caller must pass this mapping as the child process's complete ``env``;
        merging it with the executor's environment would restore its credentials.
        Git authentication remains available only to the executor's Git commands.
        This prevents environment inheritance and default personal Git configuration
        lookup; filesystem and tool access restrictions belong to the author runner.
        """
        if self._closed or self._verified_base_commit is None:
            raise WorkspaceExecutionError(
                "Authorship requires an open workspace with a verified base commit"
            )
        home = self._directory / "author-home"
        directories = {
            "HOME": home,
            "USERPROFILE": home,
            "XDG_CONFIG_HOME": home / "config",
            "APPDATA": home / "appdata",
            "LOCALAPPDATA": home / "local-appdata",
            "TMPDIR": home / "tmp",
            "TMP": home / "tmp",
            "TEMP": home / "tmp",
        }
        try:
            for directory in directories.values():
                if directory.resolve() != directory:
                    raise WorkspaceExecutionError("Authorship directory was replaced")
                directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise WorkspaceExecutionError(
                "Could not prepare the authorship environment"
            ) from None
        environment = {
            name: value
            for name, value in os.environ.items()
            if name.upper() in _AUTHOR_ENVIRONMENT_ALLOWLIST
        }
        environment.update({name: str(path) for name, path in directories.items()})
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                # An empty helper resets any helpers declared by the clone itself.
                "GIT_CONFIG_VALUE_0": "",
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
            }
        )
        return environment

    def __enter__(self) -> Self:
        if self._closed:
            raise WorkspaceExecutionError("The temporary workspace is already closed")
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del traceback
        if exception is None:
            self.close()
            return
        try:
            self.close()
        except Exception as cleanup_exc:  # noqa: BLE001
            exception.add_note(f"cleanup also failed: {cleanup_exc}")

    def close(self) -> None:
        """Close the workspace; retained preparation clones stay available for retries."""
        if self._closed:
            return
        if self._retained:
            self._remove_authorship_home()
            self._closed = True
            self._release_claim()
            return
        self._remove_directory()
        self._release_claim()

    def discard(self, *, confirm_unpushed_commits: bool = False) -> None:
        """Permanently remove this workspace, including committed and uncommitted work.

        Retained workspaces are checked for commits that are not reachable from
        the approved base or a fetched origin branch. Pass
        ``confirm_unpushed_commits=True`` only after reviewing those commits.
        """
        if self._closed and not self._retained:
            return
        if self._retained and self._claim is None:
            claim = _RetainedWorkspaceClaim(self._directory)
            claim.acquire()
            self._claim = claim
        try:
            if self._retained and not confirm_unpushed_commits:
                _guard_unpushed_workspace_commits(self)
            self._remove_directory()
        finally:
            self._release_claim()

    def _release_claim(self) -> None:
        claim = self._claim
        if claim is None:
            return
        self._claim = None
        claim.release()

    def _promote_to(self, directory: Path) -> None:
        """Atomically publish a fully prepared staging clone to its retry key."""
        if self._retained or directory.parent != self._directory.parent:
            raise WorkspaceExecutionError(
                "The prepared clone cannot be promoted safely"
            )
        if directory.exists() or directory.is_symlink():
            raise WorkspaceExecutionError(
                "A retained workspace already exists for this incident"
            )
        try:
            os.rename(self._directory, directory)
        except OSError:
            raise WorkspaceExecutionError(
                "Could not retain the prepared incident workspace"
            ) from None
        self._directory = directory
        self._parent = directory.parent
        self._retained = True

    def _remove_authorship_home(self) -> None:
        home = self._directory / "author-home"
        if not home.exists() and not home.is_symlink():
            return
        if home.is_symlink() or home.resolve().parent != self._directory:
            raise WorkspaceExecutionError(
                "Refusing to clean a replaced authorship directory"
            )
        try:
            shutil.rmtree(home, onexc=_remove_readonly)
        except OSError:
            raise WorkspaceExecutionError(
                "Could not remove the authorship environment"
            ) from None

    def _remove_directory(self) -> None:
        resolved = self._directory.resolve()
        if (
            self._directory.is_symlink()
            or resolved != self._directory
            or resolved.parent != self._parent
        ):
            raise WorkspaceExecutionError(
                "Refusing to clean a replaced temporary directory"
            )
        try:
            if self._directory.exists():
                shutil.rmtree(self._directory, onexc=_remove_readonly)
        except OSError:
            raise WorkspaceExecutionError(
                "Could not remove the temporary workspace"
            ) from None
        self._closed = True


def _reject_parent_inside_worktree(parent_dir: Path | None) -> None:
    if parent_dir is None:
        return
    # Ignore inherited routing so the check reflects the physical filesystem.
    env = {
        name: value
        for name, value in os.environ.items()
        if name
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_NAMESPACE",
        }
    }
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=parent_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise WorkspaceValidationError(
            "parent_dir",
            "Git could not verify that the workspace parent is outside a repository",
            "provide a parent directory outside any Git worktree and retry",
        ) from None
    if result.returncode == 0:
        raise WorkspaceValidationError(
            "parent_dir",
            "the workspace parent directory is inside a Git repository",
            "provide a parent directory outside any Git worktree",
        )


def _retained_workspace_directory(inputs: WorkspaceInput, parent_dir: Path) -> Path:
    """Return a private, deterministic location for retryable incident workspaces."""
    parent = parent_dir.resolve()
    identity = (
        f"uid:{os.getuid()}"
        if hasattr(os, "getuid")
        else "user:"
        + os.environ.get("USERDOMAIN", "")
        + "\\"
        + os.environ.get("USERNAME", "")
    )
    key_material = (
        f"{identity}\0{inputs.repository_url}\0"
        f"{inputs.base_branch}\0{inputs.incident_branch}"
    )
    key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:32]
    workspace_dir = parent / f".pipeline-guard-{key}"
    if workspace_dir.is_symlink():
        raise WorkspaceExecutionError(
            "Refusing to use a replaced workspace cache directory"
        )
    if workspace_dir.exists():
        if not workspace_dir.is_dir() or workspace_dir.resolve() != workspace_dir:
            raise WorkspaceExecutionError(
                "Refusing to use a replaced workspace cache directory"
            )
        if os.name != "nt":
            information = workspace_dir.stat()
            if information.st_uid != os.getuid():
                raise WorkspaceExecutionError(
                    "Workspace cache directory has a different owner"
                )
            if information.st_mode & 0o077:
                try:
                    workspace_dir.chmod(0o700)
                except OSError:
                    raise WorkspaceExecutionError(
                        "Workspace cache directory is not private"
                    ) from None
    return workspace_dir


def _clone_repository(workspace: ClonedWorkspace) -> None:
    clone_url = workspace.inputs.repository_url
    if (
        "://" not in clone_url
        and ":" not in clone_url
        and not Path(clone_url).is_absolute()
    ):
        clone_url = str(Path(clone_url).resolve())
    completed = _run_git_command(
        [
            "git",
            "clone",
            "--no-checkout",
            "--no-local",
            "--single-branch",
            "--branch",
            workspace.inputs.base_branch,
            "--origin",
            "origin",
            "--",
            clone_url,
            str(workspace.path),
        ],
        environment=_git_environment(),
        timeout=_CLONE_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise WorkspaceExecutionError(
            "Git clone failed; check repository access and the configured base branch"
        )


def _verify_retained_clone(workspace: ClonedWorkspace) -> None:
    """Validate retained repository metadata without deleting potentially useful work."""
    git_dir = workspace.path / ".git"
    if (
        not workspace.path.is_dir()
        or git_dir.is_symlink()
        or not git_dir.is_dir()
        or not (git_dir / "config").is_file()
        or not (git_dir / "HEAD").is_file()
        or not (git_dir / "objects").is_dir()
        or not (git_dir / "refs").is_dir()
    ):
        raise WorkspaceValidationError(
            "workspace",
            "the retained Git repository metadata is damaged",
            _RETAINED_DISCARD_CORRECTION,
        )
    try:
        _verify_repository(workspace)
        head = _reference_commit(
            workspace,
            "HEAD",
            field="workspace",
            correction=_RETAINED_DISCARD_CORRECTION,
        )
    except WorkspaceValidationError:
        raise WorkspaceValidationError(
            "workspace",
            "the retained Git repository metadata is damaged or cannot be verified",
            _RETAINED_DISCARD_CORRECTION,
        ) from None
    if head is None:
        raise WorkspaceValidationError(
            "workspace",
            "the retained Git HEAD does not resolve to a commit",
            _RETAINED_DISCARD_CORRECTION,
        )


def _reference_commit(
    workspace: ClonedWorkspace,
    reference: str,
    *,
    field: str,
    correction: str,
) -> str | None:
    try:
        completed = _run_git_command(
            [
                "git",
                "-C",
                str(workspace.path),
                "-c",
                "core.longpaths=true",
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{reference}^{{commit}}",
            ],
            environment=_git_environment(),
            timeout=_WORKSPACE_GIT_TIMEOUT_SECONDS,
            capture_stdout=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise WorkspaceExecutionError(
            "Git could not inspect workspace references"
        ) from None
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        raise WorkspaceValidationError(
            field, "Git could not inspect a workspace reference", correction
        )
    return completed.stdout.strip()


def _fetch_base_branch(workspace: ClonedWorkspace) -> str:
    base_branch = workspace.inputs.base_branch
    reference = f"refs/remotes/origin/{base_branch}"
    _workspace_git(
        workspace,
        "fetch",
        "--no-tags",
        "origin",
        f"+refs/heads/{base_branch}:{reference}",
        field="$.repository.default_branch",
        correction="restore access to the configured base branch and retry preparation",
    )
    commit = _reference_commit(
        workspace,
        reference,
        field="$.repository.default_branch",
        correction="configure an existing base branch rather than a tag",
    )
    if commit is None:
        raise WorkspaceValidationError(
            "$.repository.default_branch",
            "the configured base branch is missing from origin",
            "restore the configured base branch and retry preparation",
        )
    return commit


def _previous_approved_base(workspace: ClonedWorkspace) -> str | None:
    previous = _reference_commit(
        workspace,
        "refs/pipeline-guard/approved-base",
        field="workspace",
        correction="inspect the retained workspace approval metadata",
    )
    if previous is not None:
        return previous
    return _local_branch_commit(workspace, workspace.inputs.base_branch)


def _store_approved_base(workspace: ClonedWorkspace, commit: str) -> None:
    _workspace_git(
        workspace,
        "update-ref",
        "refs/pipeline-guard/approved-base",
        commit,
        field="workspace",
        correction="retry preparation in a complete retained workspace",
    )


def _write_branch_rebind_event(
    workspace: ClonedWorkspace,
    audit: BranchRebindAudit,
    *,
    event: str,
) -> None:
    audit_directory = _branch_rebind_audit_directory(workspace)
    if audit_directory.is_symlink():
        raise WorkspaceExecutionError("Refusing to write to a replaced audit directory")
    try:
        audit_directory.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        raise WorkspaceExecutionError(
            "Could not create the branch rebind audit directory"
        ) from None
    if not audit_directory.is_dir() or audit_directory.resolve() != audit_directory:
        raise WorkspaceExecutionError("Refusing to write to a replaced audit directory")
    if os.name != "nt":
        information = audit_directory.stat()
        if information.st_uid != os.getuid():
            raise WorkspaceExecutionError(
                "Branch rebind audit directory has a different owner"
            )
        if information.st_mode & 0o077:
            try:
                audit_directory.chmod(0o700)
            except OSError:
                raise WorkspaceExecutionError(
                    "Branch rebind audit directory is not private"
                ) from None

    payload = {
        "event": event,
        "incident_id": audit.incident_id,
        "branch": audit.branch,
        "previous_approved_base": audit.previous_approved_base,
        "new_approved_base": audit.new_approved_base,
        "recorded_at_ns": audit.recorded_at_ns,
    }
    filename = f"branch-rebind-{audit.recorded_at_ns}-{event}.json"
    destination = audit_directory / filename
    temporary = audit_directory / f".{filename}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise WorkspaceExecutionError(
            "Could not persist the branch rebind audit record"
        ) from None


def _branch_rebind_audit_directory(workspace: ClonedWorkspace) -> Path:
    """Return a stable sibling directory that survives deletion of the workspace cache."""
    return workspace._directory.with_name(f"{workspace._directory.name}.audit")


def _guard_unpushed_workspace_commits(workspace: ClonedWorkspace) -> None:
    """Refuse retained-workspace deletion when local commits are absent from approved history."""
    approved_base = _reference_commit(
        workspace,
        "refs/pipeline-guard/approved-base",
        field="workspace",
        correction="restore the approved-base metadata or explicitly confirm commit loss",
    )
    if approved_base is None:
        raise WorkspaceExecutionError(
            "Could not verify the approved base before discarding the retained workspace"
        )

    local_refs = _workspace_git(
        workspace,
        "for-each-ref",
        "--format=%(refname)",
        "refs/heads/",
        field="workspace",
        correction="inspect local branches before discarding the retained workspace",
    ).splitlines()
    revisions = ["HEAD", *local_refs, f"^{approved_base}"]
    local_only_commits = _workspace_git(
        workspace,
        "rev-list",
        *revisions,
        field="workspace",
        correction="review local commits before discarding the retained workspace",
    ).splitlines()
    if not local_only_commits:
        return

    _verify_remote(workspace)
    _workspace_git(
        workspace,
        "fetch",
        "--no-tags",
        "--prune",
        "origin",
        "+refs/heads/*:refs/remotes/origin/*",
        field="workspace",
        correction=(
            "restore access to origin, or explicitly confirm the loss of unverified commits"
        ),
    )
    remote_refs = _workspace_git(
        workspace,
        "for-each-ref",
        "--format=%(refname)",
        "refs/remotes/origin/",
        field="workspace",
        correction="inspect fetched origin branches before discarding the retained workspace",
    ).splitlines()
    revisions.extend(f"^{reference}" for reference in remote_refs)
    unpushed_commits = _workspace_git(
        workspace,
        "rev-list",
        *revisions,
        field="workspace",
        correction="review local commits before discarding the retained workspace",
    ).splitlines()
    if unpushed_commits:
        sample = ", ".join(commit[:12] for commit in unpushed_commits[:5])
        extra = "" if len(unpushed_commits) <= 5 else ", …"
        raise WorkspaceExecutionError(
            f"Refusing to discard a workspace with {len(unpushed_commits)} commit(s) "
            "not present on its approved base or a pushed origin branch "
            f"({sample}{extra}). {_UNPUSHED_DISCARD_CORRECTION}"
        )


def clone_workspace(
    config_path: Path,
    *,
    base_commit: object,
    incident_id: object,
    parent_dir: Path | None = None,
) -> ClonedWorkspace:
    """Create an independent clone without checking out files or creating an incident branch."""
    workspace_parent = (
        parent_dir if parent_dir is not None else Path(tempfile.gettempdir())
    ).resolve()
    _reject_parent_inside_worktree(workspace_parent)
    inputs = validate_workspace_input(
        config_path,
        base_commit=base_commit,
        incident_id=incident_id,
    )
    try:
        workspace = ClonedWorkspace(inputs, parent_dir=workspace_parent)
    except OSError:
        raise WorkspaceExecutionError(
            "Could not create the temporary workspace directory"
        ) from None
    try:
        _clone_repository(workspace)
    except (OSError, subprocess.TimeoutExpired):
        error = WorkspaceExecutionError(
            "Git clone could not complete within its execution limit"
        )
        try:
            workspace.close()
        except Exception as cleanup_exc:  # noqa: BLE001
            error.add_note(f"cleanup also failed: {cleanup_exc}")
        raise error from None
    except BaseException as exc:
        try:
            workspace.close()
        except Exception as cleanup_exc:  # noqa: BLE001
            exc.add_note(f"cleanup also failed: {cleanup_exc}")
        raise
    return workspace


def prepare_workspace(
    config_path: Path,
    *,
    base_commit: object,
    incident_id: object,
    parent_dir: Path | None = None,
    allow_branch_rebind: bool = False,
) -> ClonedWorkspace:
    """Verify an isolated clone and prepare its deterministic incident branch.

    The caller must supply a base commit from an already validated approval.
    A changed base-branch tip requires a new approval; this function never
    substitutes a newer commit for the approved one. Prepared clones are keyed
    by repository and incident so a later call can safely resume the local branch.
    ``allow_branch_rebind`` permits moving an unchanged incident branch to a new
    approved base after the configured base branch advances.
    """
    workspace_parent = (
        parent_dir if parent_dir is not None else Path(tempfile.gettempdir())
    ).resolve()
    _reject_parent_inside_worktree(workspace_parent)
    inputs = validate_workspace_input(
        config_path,
        base_commit=base_commit,
        incident_id=incident_id,
    )
    workspace_dir = _retained_workspace_directory(inputs, workspace_parent)
    claim = _RetainedWorkspaceClaim(workspace_dir)
    claim.acquire()
    reused = workspace_dir.exists()
    workspace: ClonedWorkspace
    try:
        if reused:
            try:
                workspace = ClonedWorkspace(
                    inputs,
                    workspace_dir=workspace_dir,
                    reuse_existing=True,
                )
            except OSError:
                raise WorkspaceExecutionError(
                    "Could not open the retained workspace directory"
                ) from None
            _verify_retained_clone(workspace)
        if not reused:
            try:
                workspace = ClonedWorkspace(inputs, parent_dir=workspace_parent)
            except OSError:
                raise WorkspaceExecutionError(
                    "Could not create the temporary workspace directory"
                ) from None
            workspace._retained_directory = workspace_dir
        workspace._claim = claim
    except BaseException as exc:
        try:
            claim.release()
        except Exception as cleanup_exc:  # noqa: BLE001
            exc.add_note(f"claim cleanup also failed: {cleanup_exc}")
        raise
    if not reused:
        try:
            _clone_repository(workspace)
        except (OSError, subprocess.TimeoutExpired):
            error = WorkspaceExecutionError(
                "Git clone could not complete within its execution limit"
            )
            try:
                workspace.discard()
            except Exception as cleanup_exc:  # noqa: BLE001
                error.add_note(f"cleanup also failed: {cleanup_exc}")
            raise error from None
        except BaseException as exc:
            try:
                workspace.discard()
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(f"cleanup also failed: {cleanup_exc}")
            raise
    try:
        _verify_repository(workspace)
        _verify_remote(workspace)
        if workspace._reused:
            prior_status = _workspace_git(
                workspace,
                "status",
                "--porcelain",
                "--untracked-files=all",
                field="workspace",
                correction=_RETAINED_DISCARD_CORRECTION,
            )
            if prior_status.strip():
                raise WorkspaceValidationError(
                    "workspace",
                    "the retained incident workspace has uncommitted changes",
                    _RETAINED_DISCARD_CORRECTION,
                )

        workspace._previous_approved_base = _previous_approved_base(workspace)
        branch_commit = _fetch_base_branch(workspace)
        approved = workspace.inputs.base_commit.lower()
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", approved) is None:
            raise WorkspaceValidationError(
                "base_commit",
                "a complete commit identifier is required",
                "provide the full commit hash from the validated approval",
            )
        resolved_commit = _workspace_git(
            workspace,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{approved}^{{commit}}",
            field="base_commit",
            correction="provide an approved commit present in the base branch",
        ).strip()
        if resolved_commit != approved:
            raise WorkspaceValidationError(
                "base_commit",
                "the approved object is not a commit",
                "provide the full commit hash from the validated approval",
            )
        if branch_commit != approved:
            raise WorkspaceValidationError(
                "base_commit",
                "the base branch differs from the approved commit",
                "obtain a new approval bound to the current base-branch commit",
            )
        _workspace_git(
            workspace,
            "checkout",
            "--detach",
            approved,
            "--",
            field="workspace",
            correction="retry preparation in a fresh disposable clone",
        )
        head = _workspace_git(
            workspace,
            "rev-parse",
            "--verify",
            "HEAD",
            field="base_commit",
            correction="retry preparation with the approved base commit",
        ).strip()
        if head != approved:
            raise WorkspaceValidationError(
                "base_commit",
                "the prepared checkout differs from the approved commit",
                "retry preparation in a fresh disposable clone",
            )
        status = _workspace_git(
            workspace,
            "status",
            "--porcelain",
            "--untracked-files=all",
            field="workspace",
            correction="retry preparation with a complete, clean checkout",
        )
        if status.strip():
            raise WorkspaceValidationError(
                "workspace",
                "the prepared checkout has missing or changed files",
                "retry preparation with a complete, clean checkout",
            )
        local_base_commit = _local_branch_commit(
            workspace, workspace.inputs.base_branch
        )
        if local_base_commit != approved:
            base_ref = f"refs/heads/{workspace.inputs.base_branch}"
            if local_base_commit is None:
                _workspace_git(
                    workspace,
                    "update-ref",
                    base_ref,
                    approved,
                    field="$.repository.default_branch",
                    correction="restore the local base branch and retry preparation",
                )
            else:
                _workspace_git(
                    workspace,
                    "update-ref",
                    base_ref,
                    approved,
                    local_base_commit,
                    field="$.repository.default_branch",
                    correction="restore the local base branch and retry preparation",
                )
        workspace._verified_base_commit = approved
        prepare_incident_branch(workspace, allow_rebind=allow_branch_rebind)
        if not workspace._reused and workspace._retained_directory is not None:
            workspace._promote_to(workspace._retained_directory)
    except BaseException as exc:
        try:
            if workspace._reused:
                workspace.close()
            else:
                workspace.discard()
        except Exception as cleanup_exc:  # noqa: BLE001
            exc.add_note(f"cleanup also failed: {cleanup_exc}")
        finally:
            try:
                workspace._release_claim()
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(f"claim cleanup also failed: {cleanup_exc}")
        raise
    return workspace


def discard_prepared_workspace(
    config_path: Path,
    *,
    incident_id: object,
    parent_dir: Path | None = None,
    confirm_unpushed_commits: bool = False,
) -> bool:
    """Remove one cached incident workspace, including committed and uncommitted work.

    Before deletion, fetch origin and refuse to remove commits absent from the
    approved base and all pushed origin branches. Set ``confirm_unpushed_commits``
    only after reviewing and accepting the loss of any such commits. This also
    removes uncommitted work. Return whether a retained workspace was removed.
    """
    workspace_parent = (
        parent_dir if parent_dir is not None else Path(tempfile.gettempdir())
    ).resolve()
    _reject_parent_inside_worktree(workspace_parent)
    repository_url, base_branch, branch_prefix = _workspace_repository_settings(
        config_path
    )
    normalized_incident_id = _required_text(incident_id, "incident_id")
    _validate_repository_url(repository_url)
    _validate_branch(base_branch)
    incident_branch = _incident_branch_name(branch_prefix, normalized_incident_id)
    # Cache identity excludes the approved base commit, so discard needs no approval hash.
    inputs = WorkspaceInput(
        repository_url,
        base_branch,
        "",
        incident_branch,
        normalized_incident_id,
    )
    workspace_dir = _retained_workspace_directory(inputs, workspace_parent)
    claim = _RetainedWorkspaceClaim(workspace_dir)
    claim.acquire()
    workspace: ClonedWorkspace | None = None
    try:
        if not workspace_dir.exists():
            return False
        workspace = ClonedWorkspace(
            inputs,
            workspace_dir=workspace_dir,
            reuse_existing=True,
        )
        workspace._claim = claim
        workspace.discard(confirm_unpushed_commits=confirm_unpushed_commits)
        return True
    finally:
        if workspace is None:
            claim.release()
        else:
            workspace._release_claim()


def prepare_incident_branch(
    workspace: ClonedWorkspace,
    *,
    allow_rebind: bool = False,
) -> str:
    """Create or safely reuse the branch deterministically assigned to an incident.

    Reuse is permitted when the existing local branch still points to the
    verified approved base or contains one clean executor-created remediation
    commit. That committed retry is subsequently bound to its validated patch
    hash by the Git executor. An explicit rebind can
    move a branch from its previous approved base only when it has no additional
    commits or uncommitted changes.
    """
    if workspace._closed or workspace.verified_base_commit is None:
        raise WorkspaceExecutionError(
            "Preparing an incident branch requires an open workspace with a verified base commit"
        )

    branch = workspace.inputs.incident_branch
    approved = workspace.verified_base_commit
    existing_commit = _local_branch_commit(workspace, branch)
    if existing_commit is None:
        _workspace_git(
            workspace,
            "switch",
            "--no-guess",
            "-c",
            branch,
            approved,
            field="incident_id",
            correction="retry preparation with a valid incident identifier",
        )
    else:
        if existing_commit != approved:
            status = _workspace_git(
                workspace,
                "status",
                "--porcelain",
                "--untracked-files=all",
                field="workspace",
                correction=_RETAINED_DISCARD_CORRECTION,
            )
            if status.strip():
                raise WorkspaceValidationError(
                    "workspace",
                    "the existing incident branch has uncommitted changes",
                    _RETAINED_DISCARD_CORRECTION,
                )
            if _is_single_executor_retry(
                workspace,
                base_commit=approved,
                commit=existing_commit,
                branch=branch,
            ):
                _workspace_git(
                    workspace,
                    "switch",
                    "--no-guess",
                    branch,
                    field="incident_id",
                    correction="retry preparation with a valid incident identifier",
                )
                workspace._incident_branch = branch
                _store_approved_base(workspace, approved)
                return branch

            previous = workspace._previous_approved_base
            if not allow_rebind:
                raise WorkspaceValidationError(
                    "incident_id",
                    "the existing incident branch is bound to an older approved base",
                    "retry with allow_branch_rebind=True after confirming the branch has no extra "
                    "commits",
                )
            if previous is None or existing_commit != previous:
                raise WorkspaceValidationError(
                    "incident_id",
                    "the existing incident branch has commits beyond its previous approved base",
                    "review and preserve the additional commits; automatic rebind requires the "
                    "branch to remain at its prior approved base",
                )
            audit = BranchRebindAudit(
                incident_id=workspace.inputs.incident_id,
                branch=branch,
                previous_approved_base=previous,
                new_approved_base=approved,
                recorded_at_ns=time.time_ns(),
            )
            _write_branch_rebind_event(workspace, audit, event="requested")
            _workspace_git(
                workspace,
                "switch",
                "--detach",
                approved,
                field="workspace",
                correction="retry the explicit incident-branch rebind",
            )
            _workspace_git(
                workspace,
                "branch",
                "--force",
                branch,
                approved,
                field="incident_id",
                correction="retry the explicit incident-branch rebind",
            )
            _workspace_git(
                workspace,
                "switch",
                "--no-guess",
                branch,
                field="incident_id",
                correction="retry the explicit incident-branch rebind",
            )
            _store_approved_base(workspace, approved)
            _write_branch_rebind_event(workspace, audit, event="completed")
            workspace._branch_rebind_audit = audit
        else:
            status = _workspace_git(
                workspace,
                "status",
                "--porcelain",
                "--untracked-files=all",
                field="workspace",
                correction=_RETAINED_DISCARD_CORRECTION,
            )
            if status.strip():
                raise WorkspaceValidationError(
                    "workspace",
                    "the existing incident branch has uncommitted changes",
                    _RETAINED_DISCARD_CORRECTION,
                )
            _workspace_git(
                workspace,
                "switch",
                "--no-guess",
                branch,
                field="incident_id",
                correction="retry preparation with a valid incident identifier",
            )

    current_branch = _workspace_git(
        workspace,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        field="workspace",
        correction="retry preparation with an attached incident branch",
    ).strip()
    head = _workspace_git(
        workspace,
        "rev-parse",
        "--verify",
        "HEAD",
        field="workspace",
        correction="retry preparation from the approved base commit",
    ).strip()
    if current_branch != branch or head != approved:
        raise WorkspaceValidationError(
            "workspace",
            "the prepared branch differs from the approved incident branch",
            "retry preparation in a fresh disposable clone",
        )

    workspace._incident_branch = branch
    _store_approved_base(workspace, approved)
    return branch


def _is_single_executor_retry(
    workspace: ClonedWorkspace,
    *,
    base_commit: str,
    commit: str,
    branch: str,
) -> bool:
    """Return whether a retained branch has one executor-created commit after its base."""
    merge_base = _workspace_git(
        workspace,
        "merge-base",
        base_commit,
        commit,
        field="workspace",
        correction="review the retained incident branch before retrying",
    ).strip()
    if merge_base != base_commit:
        return False
    commit_count = _workspace_git(
        workspace,
        "rev-list",
        "--count",
        f"{base_commit}..{commit}",
        field="workspace",
        correction="review the retained incident branch before retrying",
    ).strip()
    if commit_count != "1":
        return False
    subject = _workspace_git(
        workspace,
        "show",
        "-s",
        "--format=%s",
        commit,
        field="workspace",
        correction="review the retained incident branch before retrying",
    ).strip()
    return subject == f"fix(pipeline-guard): remediate {branch}"


def _verify_repository(workspace: ClonedWorkspace) -> None:
    correction = "prepare a fresh independent clone of the configured repository"
    root = _workspace_git(
        workspace,
        "rev-parse",
        "--show-toplevel",
        field="workspace",
        correction=correction,
    ).strip()
    metadata = _workspace_git(
        workspace,
        "rev-parse",
        "--absolute-git-dir",
        field="workspace",
        correction=correction,
    ).strip()
    common = _workspace_git(
        workspace,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        field="workspace",
        correction=correction,
    ).strip()
    if (
        Path(root).resolve() != workspace.path
        or Path(metadata).resolve() != workspace.path / ".git"
        or Path(common).resolve() != workspace.path / ".git"
        or (workspace.path / ".git/objects/info/alternates").exists()
    ):
        raise WorkspaceValidationError(
            "workspace",
            "the clone does not have independent repository metadata",
            correction,
        )


def _repository_location(address: str) -> str | Path:
    # Git converts relative local clone sources to absolute paths in remote.origin.url.
    path = Path(address)
    if path.is_absolute() or ":" not in address:
        return path.resolve()
    return address


def _verify_remote(workspace: ClonedWorkspace) -> None:
    correction = "use an origin remote matching the configured repository address"
    stored = _workspace_git(
        workspace,
        "config",
        "--local",
        "--null",
        "--get-all",
        "remote.origin.url",
        field="$.repository.url",
        correction=correction,
    ).split("\0")
    effective = _workspace_git(
        workspace,
        "remote",
        "get-url",
        "--all",
        "origin",
        field="$.repository.url",
        correction=correction,
    ).splitlines()
    expected = _repository_location(workspace.inputs.repository_url)
    if (
        len(stored) != 2
        or stored[-1] != ""
        or len(effective) != 1
        or _repository_location(stored[0]) != expected
        or _repository_location(effective[0]) != expected
    ):
        raise WorkspaceValidationError(
            "$.repository.url",
            "origin differs from the configured repository",
            correction,
        )


def _git_environment() -> dict[str, str]:
    """Keep Git authentication in the executor while removing inherited repository routing."""
    environment = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    ):
        environment.pop(name, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _workspace_git(
    workspace: ClonedWorkspace,
    *arguments: str,
    field: str,
    correction: str,
) -> str:
    try:
        completed = _run_git_command(
            # Command-local support for deep disposable paths on Git for Windows.
            ["git", "-C", str(workspace.path), "-c", "core.longpaths=true", *arguments],
            environment=_git_environment(),
            timeout=_WORKSPACE_GIT_TIMEOUT_SECONDS,
            capture_stdout=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise WorkspaceExecutionError(
            "Git could not complete workspace verification"
        ) from None
    if completed.returncode != 0:
        raise WorkspaceValidationError(
            field, "Git could not verify or prepare the workspace", correction
        )
    return completed.stdout


def _local_branch_commit(workspace: ClonedWorkspace, branch: str) -> str | None:
    """Return a local branch commit, or None when the branch has not been created."""
    try:
        completed = _run_git_command(
            [
                "git",
                "-C",
                str(workspace.path),
                "-c",
                "core.longpaths=true",
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"refs/heads/{branch}^{{commit}}",
            ],
            environment=_git_environment(),
            timeout=_WORKSPACE_GIT_TIMEOUT_SECONDS,
            capture_stdout=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise WorkspaceExecutionError(
            "Git could not inspect the incident branch"
        ) from None
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        raise WorkspaceValidationError(
            "incident_id",
            "Git could not inspect the existing incident branch",
            "retry preparation with a valid incident identifier",
        )
    return completed.stdout.strip()


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
        ("ThisPeriodKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    )


class _JobProcessIds(ctypes.Structure):
    _fields_ = (
        ("NumberOfAssignedProcesses", wintypes.DWORD),
        ("NumberOfProcessIdsInList", wintypes.DWORD),
        ("ProcessIdList", ctypes.c_size_t * 1),
    )


class _WindowsProcessJob:
    """Keep ownership of clone descendants even if an intermediate process exits."""

    def __init__(self) -> None:
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
            "OpenProcess": (
                [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                wintypes.HANDLE,
            ),
            "IsProcessInJob": (
                [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
                wintypes.BOOL,
            ),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "AssignProcessToJobObject": (
                [wintypes.HANDLE, wintypes.HANDLE],
                wintypes.BOOL,
            ),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = arguments
            function.restype = result
        self._process_handles: list[int] = []
        self.handle = self.api.CreateJobObjectW(None, None)
        self._check(self.handle)
        limits = _JobExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = (
            0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
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
    def _check(result: object) -> None:
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, pid: int) -> None:
        handle = self.api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        self._check(handle)
        try:
            self._check(self.api.AssignProcessToJobObject(self.handle, handle))
        finally:
            self._check(self.api.CloseHandle(handle))

    def wait_empty(self) -> None:
        deadline = time.monotonic() + _PROCESS_CLEANUP_SECONDS
        # Job accounting can reach zero before a terminated process is signaled.
        # Wait on retained handles, which also prevent PID reuse during cleanup.
        for handle in self._process_handles:
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            result = self.api.WaitForSingleObject(handle, remaining_ms)
            if result == 258:  # WAIT_TIMEOUT
                raise WorkspaceExecutionError("The Git clone process tree did not exit")
            if result != 0:  # WAIT_OBJECT_0
                raise ctypes.WinError(ctypes.get_last_error())
        while True:
            accounting = _JobAccounting()
            self._check(
                self.api.QueryInformationJobObject(
                    self.handle,
                    1,
                    ctypes.byref(accounting),
                    ctypes.sizeof(accounting),
                    None,
                )
            )
            if not accounting.ActiveProcesses:
                return
            if time.monotonic() >= deadline:
                raise WorkspaceExecutionError("The Git clone process tree did not exit")
            time.sleep(0.02)

    def terminate(self) -> None:
        try:
            self._retain_process_handles()
        finally:
            self._check(self.api.TerminateJobObject(self.handle, 1))

    def _retain_process_handles(self) -> None:
        capacity = 8
        deadline = time.monotonic() + _PROCESS_CLEANUP_SECONDS
        while True:
            buffer = ctypes.create_string_buffer(
                _JobProcessIds.ProcessIdList.offset
                + capacity * ctypes.sizeof(ctypes.c_size_t)
            )
            result = self.api.QueryInformationJobObject(
                self.handle, 3, buffer, ctypes.sizeof(buffer), None
            )
            if not result and ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                self._check(result)
            information = _JobProcessIds.from_buffer(buffer)
            if result and information.NumberOfAssignedProcesses <= capacity:
                break
            if time.monotonic() >= deadline:
                raise WorkspaceExecutionError(
                    "Could not enumerate the Git clone process tree"
                )
            capacity = max(capacity * 2, information.NumberOfAssignedProcesses)

        process_ids = (
            ctypes.c_size_t * information.NumberOfProcessIdsInList
        ).from_buffer(buffer, _JobProcessIds.ProcessIdList.offset)
        for pid in process_ids:
            # SYNCHRONIZE | QUERY_LIMITED_INFORMATION; no permission to kill an unrelated PID.
            handle = self.api.OpenProcess(0x101000, False, pid)
            if (
                not handle and ctypes.get_last_error() == 87
            ):  # Exited before OpenProcess.
                continue
            self._check(handle)
            retained = False
            try:
                belongs = wintypes.BOOL()
                self._check(
                    self.api.IsProcessInJob(handle, self.handle, ctypes.byref(belongs))
                )
                if belongs.value:
                    self._process_handles.append(handle)
                    retained = True
            finally:
                if not retained:
                    self._check(self.api.CloseHandle(handle))

    def close(self) -> None:
        try:
            self._check(self.api.CloseHandle(self.handle))
        finally:
            for handle in self._process_handles:
                self._check(self.api.CloseHandle(handle))
            self._process_handles.clear()


def _run_git_command(
    command: list[str],
    *,
    environment: dict[str, str],
    timeout: float,
    capture_stdout: bool = False,
) -> subprocess.CompletedProcess[str]:
    # The gate prevents Git from starting before ownership of its descendants is established.
    supervisor = (
        "import subprocess, sys\n"
        "if sys.stdin.buffer.read(1) != b'1': raise SystemExit(1)\n"
        "result = subprocess.run(sys.argv[1:], stdin=subprocess.DEVNULL, check=False)\n"
        "raise SystemExit(result.returncode)\n"
    )
    isolated_env = {
        name: value
        for name, value in environment.items()
        if not name.upper().startswith("PYTHON")
    }
    job = _WindowsProcessJob() if os.name == "nt" else None
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", supervisor, *command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=isolated_env,
            cwd=tempfile.gettempdir(),
            start_new_session=os.name != "nt",
        )
        if job is not None:
            job.assign(process.pid)
        stdout, _ = process.communicate(b"1", timeout=timeout)
        return subprocess.CompletedProcess(
            command,
            process.wait(),
            stdout.decode("utf-8", errors="replace") if stdout is not None else "",
        )
    finally:
        try:
            if process is not None:
                try:
                    if job is not None:
                        job.terminate()
                    else:
                        # An absent group means all owned processes have already exited.
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                finally:
                    if process.poll() is None:
                        process.kill()  # Includes failure to assign the waiting supervisor.
                    process.communicate(timeout=_PROCESS_CLEANUP_SECONDS)
                if job is not None:
                    job.wait_empty()
        finally:
            try:
                if job is not None:
                    job.close()  # Kill-on-close is also a fallback if termination fails.
            finally:
                if process is not None:
                    for pipe in (process.stdin, process.stdout):
                        if pipe is not None:
                            pipe.close()


def _remove_readonly(
    function: Callable[..., object], path: str, error: BaseException
) -> None:
    """Allow Windows cleanup of read-only Git object files inside the owned directory."""
    if os.name != "nt" or not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def validate_workspace_input(
    config_path: Path,
    *,
    base_commit: object,
    incident_id: object,
) -> WorkspaceInput:
    """Load configuration and validate required fields without creating a workspace.

    The caller supplies a base commit from an already validated approval. This
    function checks its presence, not the approval or the commit's existence.
    """
    repository_url, base_branch, branch_prefix = _workspace_repository_settings(
        config_path
    )
    approved_commit = _required_text(base_commit, "base_commit")
    normalized_incident_id = _required_text(incident_id, "incident_id")
    _validate_repository_url(repository_url)
    _validate_branch(base_branch)
    incident_branch = _incident_branch_name(branch_prefix, normalized_incident_id)
    return WorkspaceInput(
        repository_url,
        base_branch,
        approved_commit,
        incident_branch,
        normalized_incident_id,
    )


def _workspace_repository_settings(config_path: Path) -> tuple[str, str, str]:
    """Load required repository settings used to key retained workspaces."""
    config = validate_config(config_path)
    repository = config.get("repository", {})
    repository_url = _required_text(repository.get("url"), "$.repository.url")
    base_branch = _required_text(
        repository.get("default_branch"), "$.repository.default_branch"
    )
    branch_prefix = _required_text(
        repository.get("remediation_branch_prefix"),
        "$.repository.remediation_branch_prefix",
    )
    return repository_url, base_branch, branch_prefix


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceValidationError(
            field,
            "a non-empty string is required",
            f"provide {field} before preparing the workspace",
        )
    return value


def _validate_repository_url(repository_url: str) -> None:
    try:
        parsed = urlsplit(repository_url)
    except ValueError:
        raise WorkspaceValidationError(
            "$.repository.url",
            "the repository URL could not be parsed",
            "provide a valid repository address without embedded credentials",
        ) from None
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise WorkspaceValidationError(
            "$.repository.url",
            "URL userinfo, query parameters and fragments are not allowed",
            "use a repository address without credentials or authentication parameters",
        )
    if parsed.scheme == "http":
        raise WorkspaceValidationError(
            "$.repository.url",
            "HTTP repository URLs are forbidden for remediation",
            "use an HTTPS repository URL",
        )


def _validate_branch(base_branch: str) -> None:
    _validate_branch_name(base_branch, "$.repository.default_branch")


def _incident_branch_name(branch_prefix: str, incident_id: str) -> str:
    _validate_branch_name(
        f"{branch_prefix}/incident",
        "$.repository.remediation_branch_prefix",
    )
    encoded_incident_id = quote_from_bytes(incident_id.encode("utf-8"), safe="-_")
    encoded_incident_id = encoded_incident_id.replace(".", "%2E").replace("~", "%7E")
    branch = f"{branch_prefix}/{encoded_incident_id}"
    _validate_branch_name(branch, "incident_id")
    return branch


def _validate_branch_name(branch: str, field: str) -> None:
    # Reject checkout shorthand such as @{-1}; the configured branch must be literal.
    if "@{" in branch or "\x00" in branch:
        raise WorkspaceValidationError(
            field,
            "a literal Git branch name is required",
            "provide the base branch name without checkout shorthand or control characters",
        )
    try:
        completed = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise WorkspaceValidationError(
            field,
            "Git could not check the branch name",
            "ensure Git is installed and available, then retry validation",
        ) from None
    if completed.returncode != 0:
        raise WorkspaceValidationError(
            field,
            "the branch name is invalid for Git",
            "provide a valid base branch name",
        )
