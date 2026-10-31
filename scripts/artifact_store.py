"""Store pipeline-guard run artifacts locally or through a host-provided store."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ArtifactStoreConfigurationError(ValueError):
    """Raised when no usable artifact store has been configured."""


@runtime_checkable
class ArtifactStore(Protocol):
    """Public interface for storing bytes addressed by run ID and reference."""

    def put(self, run_id: str, reference: str, artifact: bytes) -> bool:
        """Store an artifact, returning false when that reference already exists."""
        ...

    def get(self, run_id: str, reference: str) -> bytes:
        """Return a stored artifact or raise ``FileNotFoundError``."""
        ...

    def exists(self, run_id: str, reference: str) -> bool:
        """Return whether the referenced artifact exists."""
        ...


@runtime_checkable
class ArtifactSession(ArtifactStore, Protocol):
    """Temporary artifact scope that expires its new content when closed."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def close(self) -> None:
        """Expire artifacts created within this session."""
        ...


@runtime_checkable
class SessionArtifactStore(ArtifactStore, Protocol):
    """Artifact store with an explicit retention-session lifecycle."""

    def session(self) -> ArtifactSession:
        """Open a temporary artifact session."""
        ...


class LocalArtifactStore:
    """Filesystem-backed store rooted at ``CLAUDE_PLUGIN_DATA/runs``."""

    def __init__(self, plugin_data_dir: Path | str | None = None) -> None:
        if plugin_data_dir is None:
            plugin_data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
        if plugin_data_dir is None or not str(plugin_data_dir).strip():
            raise ArtifactStoreConfigurationError(
                "CLAUDE_PLUGIN_DATA is not set; configure an artifact store or provide a "
                "host store."
            )

        self._runs_dir = Path(plugin_data_dir).expanduser() / "runs"

    def put(self, run_id: str, reference: str, artifact: bytes) -> bool:
        """Create an artifact once; repeated writes preserve the original bytes."""
        target = self._artifact_path(run_id, reference)
        if not isinstance(artifact, bytes):
            raise TypeError("artifact must be bytes")

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(artifact)
                temporary.flush()
                os.fsync(temporary.fileno())

            try:
                # Linking the completed temporary file makes creation atomic and
                # prevents concurrent retries from replacing an existing value.
                os.link(temporary_path, target)
            except FileExistsError:
                return False
            return True
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def get(self, run_id: str, reference: str) -> bytes:
        """Read the referenced artifact from the local run directory."""
        return self._artifact_path(run_id, reference).read_bytes()

    def exists(self, run_id: str, reference: str) -> bool:
        """Check for the referenced artifact without creating directories."""
        return self._artifact_path(run_id, reference).is_file()

    def delete(self, run_id: str, reference: str) -> bool:
        """Delete one artifact and prune its empty run directory."""
        target = self._artifact_path(run_id, reference)

        try:
            target.unlink()
        except FileNotFoundError:
            return False

        with suppress(OSError):
            target.parent.rmdir()

        return True

    def session(self) -> ArtifactSession:
        """Open a scope whose newly created artifacts expire when it closes."""
        return LocalArtifactSession(self)

    def _artifact_path(self, run_id: str, reference: str) -> Path:
        if not isinstance(run_id, str) or _RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise ValueError(
                "run_id must contain only letters, digits, dots, underscores, or hyphens"
            )
        if run_id in {".", ".."}:
            raise ValueError("run_id cannot be a path traversal component")
        if not isinstance(reference, str) or not reference or "\x00" in reference:
            raise ValueError("reference must be a non-empty string without NUL characters")

        # References are opaque identifiers, not paths supplied by a consumer.
        reference_hash = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        return self._runs_dir / run_id / reference_hash


class LocalArtifactSession:
    """Artifact-store view that removes its new artifacts at session end.

    Durable artifacts written directly through :class:`LocalArtifactStore` are
    not affected. This lets idempotency markers survive while evidence marked
    with ``retention=session`` is discarded deterministically.
    """

    def __init__(self, store: LocalArtifactStore) -> None:
        self._store = store
        self._created: set[tuple[str, str]] = set()
        self._closed = False

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.close()

    def put(self, run_id: str, reference: str, artifact: bytes) -> bool:
        """Store an artifact and mark newly created content for cleanup."""
        self._ensure_open()
        created = self._store.put(run_id, reference, artifact)

        if created:
            self._created.add((run_id, reference))

        return created

    def get(self, run_id: str, reference: str) -> bytes:
        """Read an artifact while the session remains open."""
        self._ensure_open()
        return self._store.get(run_id, reference)

    def exists(self, run_id: str, reference: str) -> bool:
        """Check for an artifact while the session remains open."""
        self._ensure_open()
        return self._store.exists(run_id, reference)

    def close(self) -> None:
        """Expire every artifact created by this session."""
        if self._closed:
            return

        try:
            for run_id, reference in sorted(self._created):
                self._store.delete(run_id, reference)
        finally:
            self._created.clear()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("artifact session is closed")


def resolve_artifact_store(
    config: Mapping[str, object] | None,
    *,
    host_reference: ArtifactStore | None = None,
    plugin_data_dir: Path | str | None = None,
) -> ArtifactStore:
    """Resolve the configured store, preferring an explicitly supplied host store."""
    if host_reference is not None:
        if not isinstance(host_reference, ArtifactStore):
            raise TypeError("host_reference must implement put, get, and exists")
        return host_reference

    runtime = config.get("runtime") if config is not None else None
    store_kind = runtime.get("artifact_store") if isinstance(runtime, Mapping) else None
    if store_kind is None:
        raise ArtifactStoreConfigurationError(
            "runtime.artifact_store is not configured and no host artifact store was provided."
        )
    if store_kind != "local":
        raise ArtifactStoreConfigurationError(
            f"unsupported artifact store {store_kind!r}; provide its host implementation "
            "explicitly."
        )

    return LocalArtifactStore(plugin_data_dir=plugin_data_dir)
