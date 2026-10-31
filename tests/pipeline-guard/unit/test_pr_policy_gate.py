"""Unit tests for the deterministic unvalidated-draft policy gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.pr_policy_gate import PRPolicyGateError, decide_pr_publication  # noqa: E402


def test_all_passed_is_always_validated() -> None:
    decision = decide_pr_publication("ALL_PASSED", allow_unvalidated_draft=False)

    assert decision.status == "VALIDATED"


@pytest.mark.parametrize("validation_status", ["HAS_FAILURES", "INCOMPLETE"])
def test_failed_or_incomplete_validation_is_blocked_by_default(
    validation_status: str,
) -> None:
    decision = decide_pr_publication(validation_status, allow_unvalidated_draft=False)

    assert decision.status == "BLOCKED"


@pytest.mark.parametrize("validation_status", ["HAS_FAILURES", "INCOMPLETE"])
def test_explicit_policy_allows_an_unvalidated_draft(validation_status: str) -> None:
    decision = decide_pr_publication(validation_status, allow_unvalidated_draft=True)

    assert decision.status == "PUBLISHED_UNVALIDATED"


def test_invalid_status_is_rejected() -> None:
    with pytest.raises(PRPolicyGateError, match="validation_status"):
        decide_pr_publication("PASSED", allow_unvalidated_draft=False)
