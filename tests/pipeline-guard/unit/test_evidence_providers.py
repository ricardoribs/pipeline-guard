from __future__ import annotations

import hashlib
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from adapters.evidence import (  # noqa: E402
    EvidenceCapability,
    EvidencePayload,
    QueryExecutionGuarantees,
    ReadOnlyQueryConfigurationError,
    ReadOnlyQuerySpec,
    ReadOnlyQueryWrapper,
    collect_if_supported,
)

sys.path.remove(str(PLUGIN_ROOT))


class _Provider:
    name = "fixture"
    capabilities = frozenset({EvidenceCapability.LOGS})

    def collect(self, capability, request):
        return [EvidencePayload(kind=capability, content=request)]


class _Executor:
    guarantees = QueryExecutionGuarantees(
        read_only=True,
        bound_parameters=True,
        source_row_limit=True,
        source_timeout=True,
    )

    def __init__(self, rows: Sequence[Mapping[str, object]] | None = None):
        self.rows = rows or ()
        self.calls = []

    def execute_read_only(self, query, parameters, *, max_rows, timeout_seconds):
        self.calls.append((query, parameters, max_rows, timeout_seconds))
        return self.rows


def test_missing_or_unsupported_provider_is_optional():
    assert collect_if_supported(None, EvidenceCapability.LOGS, {}) == ()
    assert collect_if_supported(_Provider(), EvidenceCapability.QUERY, {}) == ()


def test_provider_exposes_and_returns_only_supported_capability():
    request = {"incident_id": "incident-1", "run": "r1"}
    payloads = collect_if_supported(_Provider(), EvidenceCapability.LOGS, request)

    assert len(payloads) == 1
    assert payloads[0].kind == EvidenceCapability.LOGS
    record = payloads[0].to_dict()
    assert record == {
        "evidence_id": payloads[0].evidence_id,
        "origin": "fixture",
        "type": "LOG",
        "hash": f"sha256:{hashlib.sha256(payloads[0].content.encode()).hexdigest()}",
        "retention": "session",
        "content": '{"incident_id":"incident-1","run":"r1"}',
    }


def test_provider_evidence_id_is_stable_for_the_same_incident_and_payload():
    request = {"incident_id": "incident-1", "run": "r1"}

    first = collect_if_supported(_Provider(), EvidenceCapability.LOGS, request)[0]
    second = collect_if_supported(_Provider(), EvidenceCapability.LOGS, request)[0]

    assert first.evidence_id == second.evidence_id


def test_supported_provider_requires_an_incident_id():
    class _UnexpectedProviderCall(_Provider):
        def collect(self, capability, request):
            _ = capability, request
            raise AssertionError("provider must not be accessed without an incident_id")

    with pytest.raises(ValueError, match="incident_id"):
        collect_if_supported(
            _UnexpectedProviderCall(), EvidenceCapability.LOGS, {"run": "r1"}
        )


@pytest.mark.parametrize(
    "missing_guarantee",
    ["read_only", "bound_parameters", "source_row_limit", "source_timeout"],
)
def test_query_executor_missing_a_guarantee_is_rejected(missing_guarantee):
    executor = _Executor()
    executor.guarantees = replace(
        executor.guarantees,
        **{missing_guarantee: False},
    )

    with pytest.raises(ReadOnlyQueryConfigurationError, match="must guarantee"):
        ReadOnlyQueryWrapper(executor, {})


def test_allowlisted_query_uses_bound_parameters_and_source_limits():
    executor = _Executor(rows=[{"id": 1}, {"id": 2}, {"id": 3}])
    query = "SELECT id FROM runs WHERE run_id = :run_id"
    wrapper = ReadOnlyQueryWrapper(
        executor,
        {"run_by_id": ReadOnlyQuerySpec(query, max_rows=2, timeout_seconds=1.5)},
    )

    result = wrapper.execute("run_by_id", query, {"run_id": "r1"})

    assert result == ({"id": 1}, {"id": 2})
    assert executor.calls == [(query, {"run_id": "r1"}, 2, 1.5)]


def test_wrapper_rejects_boolean_timeout():
    executor = _Executor()
    query = "SELECT id FROM runs"

    with pytest.raises(ReadOnlyQueryConfigurationError, match="timeout_seconds"):
        ReadOnlyQueryWrapper(
            executor,
            {"read": ReadOnlyQuerySpec(query, max_rows=5, timeout_seconds=True)},
        )


@pytest.mark.parametrize(
    ("query_id", "query", "parameters"),
    [
        ("unknown", "SELECT id FROM runs", {}),
        ("read", "DELETE FROM runs", {}),
        ("read", "SELECT id FROM runs; DROP TABLE runs", {}),
        ("read", "SELECT id FROM runs WHERE run_id = :run_id", {}),
        (
            "read",
            "SELECT id FROM runs WHERE run_id = :run_id",
            {"run_id": "r1", "extra": 1},
        ),
    ],
)
def test_wrapper_rejects_unallowlisted_or_unsafe_queries(query_id, query, parameters):
    executor = _Executor()
    configured_query = "SELECT id FROM runs WHERE run_id = :run_id"
    wrapper = ReadOnlyQueryWrapper(
        executor,
        {"read": ReadOnlyQuerySpec(configured_query, max_rows=5, timeout_seconds=1)},
    )

    with pytest.raises(ReadOnlyQueryConfigurationError):
        wrapper.execute(query_id, query, parameters)

    assert executor.calls == []
