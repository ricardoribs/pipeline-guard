# /// script
# requires-python = ">=3.12"
# ///
"""Deterministically decide whether validation permits publishing a draft PR."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_VALIDATION_STATUSES = frozenset({"ALL_PASSED", "HAS_FAILURES", "INCOMPLETE"})
_PUBLICATION_STATUSES = frozenset({"VALIDATED", "PUBLISHED_UNVALIDATED", "BLOCKED"})


class PRPolicyGateError(ValueError):
    """Raised when the policy gate cannot make a deterministic decision."""


@dataclass(frozen=True)
class PRPolicyDecision:
    """The publication outcome and its deterministic explanation."""

    status: str
    reason: str


def decide_pr_publication(
    validation_status: object,
    *,
    allow_unvalidated_draft: object,
) -> PRPolicyDecision:
    """Return the only permitted publication state for a validation outcome.

    This function is deliberately independent of PR transport.  A caller must
    not invoke a Git-hosting integration when the result is ``BLOCKED``.
    """
    if (
        not isinstance(validation_status, str)
        or validation_status not in _VALIDATION_STATUSES
    ):
        raise PRPolicyGateError("validation_status is invalid")
    if type(allow_unvalidated_draft) is not bool:
        raise PRPolicyGateError("allow_unvalidated_draft must be a boolean")

    if validation_status == "ALL_PASSED":
        return PRPolicyDecision(
            status="VALIDATED",
            reason="All declared validation commands passed.",
        )
    if allow_unvalidated_draft:
        return PRPolicyDecision(
            status="PUBLISHED_UNVALIDATED",
            reason="The project explicitly allows a draft PR without complete passing validation.",
        )
    return PRPolicyDecision(
        status="BLOCKED",
        reason=(
            "The project does not allow publishing a draft PR with incomplete or failed validation."
        ),
    )


def decide_pr_publication_from_config(
    validation_status: object,
    project_config: object,
) -> PRPolicyDecision:
    """Read only the explicit remediation policy from a validated project configuration."""
    if not isinstance(project_config, Mapping):
        raise PRPolicyGateError("project_config must be an object")
    remediation = project_config.get("remediation", {})
    if not isinstance(remediation, Mapping):
        raise PRPolicyGateError("project_config.remediation must be an object")
    return decide_pr_publication(
        validation_status,
        allow_unvalidated_draft=remediation.get("allow_unvalidated_draft", False),
    )
