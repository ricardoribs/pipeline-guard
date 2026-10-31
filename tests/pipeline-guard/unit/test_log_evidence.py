from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

# These runtime imports must follow sys.path injection; Ruff versions classify them differently.
# isort: off
from adapters.evidence import (
    EvidenceCapability,
    EvidencePayload,
    LargeLogPolicy,
    LargeLogProcessingError,
    collect_if_supported,
)
from adapters.evidence.logs import LogArtifactStore
from scripts.artifact_store import LocalArtifactStore
# isort: on

sys.path.remove(str(PLUGIN_ROOT))


class _MemoryArtifactStore:
    def __init__(self) -> None:
        self.artifacts: dict[tuple[str, str], bytes] = {}

    def put(self, run_id: str, reference: str, artifact: bytes) -> bool:
        key = (run_id, reference)
        if key in self.artifacts:
            return False
        self.artifacts[key] = artifact
        return True

    def get(self, run_id: str, reference: str) -> bytes:
        return self.artifacts[(run_id, reference)]


class _LogProvider:
    name = "fixture-logs"
    capabilities = frozenset({EvidenceCapability.LOGS})

    def __init__(self, content: bytes | str, reference: str | None = None) -> None:
        self.content = content
        self.reference = reference

    def collect(self, capability, request):
        _ = request
        return [
            EvidencePayload(
                kind=capability,
                content=self.content,
                reference=self.reference,
            )
        ]


_TEST_POLICY = LargeLogPolicy(
    max_inline_bytes=40,
    chunk_lines=4,
    overlap_lines=1,
    max_critical_lines=2,
    max_warning_lines=1,
    warning_critical_threshold=2,
    tail_lines=3,
    max_line_chars=80,
)


def _collect_large_log(
    content: bytes | str,
    store: LogArtifactStore,
    *,
    reference: str | None = None,
):
    return collect_if_supported(
        _LogProvider(content, reference),
        EvidenceCapability.LOGS,
        {"incident_id": "incident-large-log"},
        artifact_store=store,
        large_log_policy=_TEST_POLICY,
    )[0]


def test_session_retention_removes_original_and_index_after_collection(
    tmp_path: Path,
) -> None:
    original = b"INFO filler that exceeds the test threshold\nERROR temporary failure\n"
    store = LocalArtifactStore(plugin_data_dir=tmp_path)

    with store.session() as session_store:
        evidence = _collect_large_log(original, session_store)
        filtered = json.loads(evidence.content)
        run_id = filtered["original"]["artifact_run_id"]
        original_reference = filtered["original"]["reference"]
        index_reference = filtered["index"]["reference"]

        assert session_store.exists(run_id, original_reference) is True
        assert session_store.exists(run_id, index_reference) is True

    assert store.exists(run_id, original_reference) is False
    assert store.exists(run_id, index_reference) is False


def test_small_log_remains_inline_without_artifact_store() -> None:
    evidence = collect_if_supported(
        _LogProvider("INFO complete"),
        EvidenceCapability.LOGS,
        {"incident_id": "incident-small-log"},
        large_log_policy=_TEST_POLICY,
    )[0]

    assert evidence.content == "INFO complete"


def test_large_log_is_filtered_and_original_bytes_are_preserved() -> None:
    original = (
        b"INFO irrelevant-start\n"
        b"INFO irrelevant-middle\n"
        b"ERROR database unavailable\n"
        b"INFO irrelevant-end\n"
        b"WARNING retrying connection\n"
    )
    store = _MemoryArtifactStore()

    evidence = _collect_large_log(original, store)
    filtered = json.loads(evidence.content)
    original_metadata = filtered["original"]

    assert filtered["filtered"] is True
    assert filtered["summary"] == {
        "critical_count": 1,
        "selected_count": 2,
        "truncated": True,
        "warning_count": 1,
    }
    assert [entry["line_number"] for entry in filtered["entries"]] == [3, 5]
    assert "irrelevant-middle" not in evidence.content
    assert (
        store.get(
            original_metadata["artifact_run_id"],
            original_metadata["reference"],
        )
        == original
    )
    assert original_metadata["hash"] == f"sha256:{hashlib.sha256(original).hexdigest()}"


def test_large_log_index_has_overlapping_ranges_and_verified_hash() -> None:
    original = "\n".join(f"INFO line-{index}" for index in range(1, 9))
    store = _MemoryArtifactStore()

    evidence = _collect_large_log(original, store)
    filtered = json.loads(evidence.content)
    index_metadata = filtered["index"]
    index_bytes = store.get(
        index_metadata["artifact_run_id"],
        index_metadata["reference"],
    )
    index = json.loads(index_bytes)

    assert index_metadata["hash"] == f"sha256:{hashlib.sha256(index_bytes).hexdigest()}"
    assert [(chunk["start_line"], chunk["end_line"]) for chunk in index["chunks"]] == [
        (1, 4),
        (4, 7),
        (7, 8),
    ]
    assert all(chunk["hash"].startswith("sha256:") for chunk in index["chunks"])


def test_repeated_large_log_collection_is_deterministic_and_does_not_overwrite() -> (
    None
):
    original = b"INFO filler that exceeds the test threshold\nERROR stable failure\n"
    store = _MemoryArtifactStore()

    first = _collect_large_log(original, store)
    artifact_snapshot = dict(store.artifacts)
    second = _collect_large_log(original, store)

    assert first == second
    assert store.artifacts == artifact_snapshot


def test_large_log_reference_collision_preserves_existing_content() -> None:
    store = _MemoryArtifactStore()
    reference = "logs/shared.log"
    _collect_large_log(
        b"INFO original content above threshold\nERROR original failure\n",
        store,
        reference=reference,
    )

    with pytest.raises(LargeLogProcessingError, match="reference collision"):
        _collect_large_log(
            b"INFO replacement content above threshold\nERROR changed failure\n",
            store,
            reference=reference,
        )

    original_key = next(key for key in store.artifacts if key[1] == reference)
    assert store.artifacts[original_key].startswith(b"INFO original")


def test_large_log_without_artifact_store_is_rejected() -> None:
    with pytest.raises(LargeLogProcessingError, match="requires an ArtifactStore"):
        collect_if_supported(
            _LogProvider(
                "INFO filler above threshold\nERROR unavailable artifact store\n"
            ),
            EvidenceCapability.LOGS,
            {"incident_id": "incident-no-store"},
            large_log_policy=_TEST_POLICY,
        )


def test_log_above_global_input_limit_is_rejected_before_storage() -> None:
    store = _MemoryArtifactStore()
    bounded_policy = replace(_TEST_POLICY, max_input_bytes=60)

    with pytest.raises(LargeLogProcessingError, match="exceeds max_input_bytes=60"):
        collect_if_supported(
            _LogProvider("é" * 31),
            EvidenceCapability.LOGS,
            {"incident_id": "incident-oversized-log"},
            artifact_store=store,
            large_log_policy=bounded_policy,
        )

    assert store.artifacts == {}


def test_large_log_policy_rejects_inline_limit_above_global_limit() -> None:
    with pytest.raises(
        ValueError, match="max_inline_bytes cannot exceed max_input_bytes"
    ):
        replace(_TEST_POLICY, max_input_bytes=39)


def test_large_log_counts_all_matches_but_keeps_only_bounded_selections() -> None:
    original = "\n".join(f"ERROR failure-{index}" for index in range(10))
    store = _MemoryArtifactStore()

    evidence = _collect_large_log(original, store)
    filtered = json.loads(evidence.content)

    assert filtered["summary"]["critical_count"] == 10
    assert filtered["summary"]["selected_count"] == _TEST_POLICY.max_critical_lines
    assert len(filtered["entries"]) == _TEST_POLICY.max_critical_lines


def test_large_log_without_matches_returns_only_bounded_tail() -> None:
    original = "\n".join(f"INFO line-{index}" for index in range(1, 9))
    store = _MemoryArtifactStore()

    evidence = _collect_large_log(original, store)
    filtered = json.loads(evidence.content)

    assert [entry["line_number"] for entry in filtered["entries"]] == [6, 7, 8]
    assert {entry["classification"] for entry in filtered["entries"]} == {"tail"}
    assert "line-1" not in evidence.content
