"""Common contract for optional evidence sources."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .logs import (
    DEFAULT_LARGE_LOG_POLICY,
    LargeLogPolicy,
    LogArtifactStore,
    filter_and_index_large_log,
)

_RETENTION_POLICY = "session"
_EVIDENCE_TYPE = {
    "logs": "LOG",
    "metadata": "METADATA",
    "query": "QUERY",
    "artifacts": "ARTIFACT",
    "links": "LINK",
}


class EvidenceCapability(StrEnum):
    """Kinds of evidence a provider may explicitly support."""

    LOGS = "logs"
    METADATA = "metadata"
    QUERY = "query"
    ARTIFACTS = "artifacts"
    LINKS = "links"


@dataclass(frozen=True, slots=True)
class EvidencePayload:
    """Provider output before common evidence identity and retention metadata."""

    kind: EvidenceCapability
    content: bytes | str | Mapping[str, object]
    reference: str | None = None
    media_type: str | None = None


@dataclass(frozen=True, slots=True)
class CollectedEvidence:
    """Evidence with stable identity and metadata ready for the diagnosis contract."""

    evidence_id: str
    origin: str
    kind: EvidenceCapability
    hash: str
    retention: str
    content: str

    def to_dict(self) -> dict[str, str]:
        """Return the pipeline adapter contract representation."""
        return {
            "evidence_id": self.evidence_id,
            "origin": self.origin,
            "type": _EVIDENCE_TYPE[self.kind.value],
            "hash": self.hash,
            "retention": self.retention,
            "content": self.content,
        }


@runtime_checkable
class EvidenceProvider(Protocol):
    """Optional source that advertises and returns only its supported capabilities."""

    @property
    def name(self) -> str:
        """Stable provider name used as the evidence origin."""
        ...

    @property
    def capabilities(self) -> frozenset[EvidenceCapability]:
        """Capabilities implemented by this provider."""
        ...

    def collect(
        self,
        capability: EvidenceCapability,
        request: Mapping[str, object],
    ) -> Iterable[EvidencePayload]:
        """Collect one configured capability without modifying its source."""
        ...


def collect_if_supported(
    provider: EvidenceProvider | None,
    capability: EvidenceCapability,
    request: Mapping[str, object],
    *,
    artifact_store: LogArtifactStore | None = None,
    large_log_policy: LargeLogPolicy = DEFAULT_LARGE_LOG_POLICY,
) -> tuple[CollectedEvidence, ...]:
    """Collect and finalize evidence, returning nothing for optional unsupported sources."""
    if provider is None or capability not in provider.capabilities:
        return ()

    incident_id = request.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id.strip():
        raise ValueError("evidence collection request must include a non-empty incident_id")

    origin = provider.name
    if not isinstance(origin, str) or not origin.strip():
        raise ValueError("evidence provider name must be a non-empty string")

    payloads = tuple(provider.collect(capability, request))
    if any(payload.kind != capability for payload in payloads):
        raise ValueError(
            f"Evidence provider {origin!r} returned a payload for the wrong capability."
        )

    return tuple(
        _finalize_payload(
            payload,
            incident_id=incident_id,
            origin=origin,
            artifact_store=artifact_store,
            large_log_policy=large_log_policy,
        )
        for payload in payloads
    )


def _finalize_payload(
    payload: EvidencePayload,
    *,
    incident_id: str,
    origin: str,
    artifact_store: LogArtifactStore | None,
    large_log_policy: LargeLogPolicy,
) -> CollectedEvidence:
    """Create deterministic metadata for one provider payload."""
    if payload.kind is EvidenceCapability.LOGS:
        processed_log = filter_and_index_large_log(
            payload.content,
            incident_id=incident_id,
            origin=origin,
            reference=payload.reference,
            artifact_store=artifact_store,
            policy=large_log_policy,
        )
        if processed_log is not None:
            payload = EvidencePayload(
                kind=payload.kind,
                content=processed_log.content,
                reference=processed_log.reference,
                media_type="application/json",
            )

    content = _content_as_text(payload.content)
    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    identity = {
        "incident_id": incident_id,
        "origin": origin,
        "type": _EVIDENCE_TYPE[payload.kind.value],
        "reference": payload.reference,
        "content_hash": content_digest if payload.reference is None else None,
    }
    identity_text = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    identity_digest = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()

    return CollectedEvidence(
        evidence_id=f"{incident_id}:{origin}:{identity_digest[:16]}",
        origin=origin,
        kind=payload.kind,
        hash=f"sha256:{content_digest}",
        retention=_RETENTION_POLICY,
        content=content,
    )


def _content_as_text(content: bytes | str | Mapping[str, object]) -> str:
    """Represent provider content as contract text while preserving binary payloads."""
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return f"base64:{base64.b64encode(content).decode('ascii')}"

    try:
        return json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("evidence content mappings must be JSON serializable") from error
