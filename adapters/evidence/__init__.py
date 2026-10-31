"""Read-only, explicitly enabled evidence provider interfaces."""

from .base import (
    CollectedEvidence,
    EvidenceCapability,
    EvidencePayload,
    EvidenceProvider,
    collect_if_supported,
)
from .logs import LargeLogPolicy, LargeLogProcessingError
from .read_only import (
    QueryExecutionGuarantees,
    ReadOnlyQueryConfigurationError,
    ReadOnlyQueryExecutor,
    ReadOnlyQuerySpec,
    ReadOnlyQueryWrapper,
)
from .registry import (
    EVIDENCE_PROVIDER_REGISTRY,
    EvidenceProviderRegistration,
    EvidenceProviderRegistry,
    EvidenceProviderRegistryError,
)

__all__ = [
    "EVIDENCE_PROVIDER_REGISTRY",
    "CollectedEvidence",
    "EvidenceCapability",
    "EvidencePayload",
    "EvidenceProvider",
    "EvidenceProviderRegistration",
    "EvidenceProviderRegistry",
    "EvidenceProviderRegistryError",
    "LargeLogPolicy",
    "LargeLogProcessingError",
    "QueryExecutionGuarantees",
    "ReadOnlyQueryConfigurationError",
    "ReadOnlyQueryExecutor",
    "ReadOnlyQuerySpec",
    "ReadOnlyQueryWrapper",
    "collect_if_supported",
]
