from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.approval_authenticator import (
    ApprovalAuthenticationError,
    verify_orchestrated_approval,
)

sys.path.remove(str(PLUGIN_ROOT))

DOMAIN_SEPARATOR = b"pipeline-guard:approval_input_v1:v1\n"
SHARED_SECRET = b"x" * 32


def _signed_approval() -> dict[str, Any]:
    approval: dict[str, Any] = {
        "schema_version": "1.0",
        "incident_id": "incident-auth-001",
        "diagnosis_hash": f"sha256:{'a' * 64}",
        "decision": "APPROVED",
        "actor": "maintainer",
        "decided_at": "2026-10-16T12:00:00Z",
        "channel_type": "orchestrated",
        "external_reference": "approval-001",
        "base_commit": "0123456789abcdef0123456789abcdef01234567",
    }
    canonical = json.dumps(
        approval,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    digest = hmac.new(
        SHARED_SECRET,
        DOMAIN_SEPARATOR + canonical,
        hashlib.sha256,
    ).hexdigest()
    approval["signature"] = f"hmac-sha256:{digest}"
    return approval


def test_valid_signature_is_accepted() -> None:
    approval = _signed_approval()

    result = verify_orchestrated_approval(
        approval,
        shared_secret=SHARED_SECRET,
    )

    assert result == approval
    assert result is not approval


def test_tampered_approval_is_rejected() -> None:
    approval = _signed_approval()
    approval["base_commit"] = "f" * 40

    with pytest.raises(
        ApprovalAuthenticationError,
        match="authentication failed",
    ):
        verify_orchestrated_approval(
            approval,
            shared_secret=SHARED_SECRET,
        )


def test_missing_signature_is_rejected() -> None:
    approval = _signed_approval()
    approval.pop("signature")

    with pytest.raises(
        ApprovalAuthenticationError,
        match="signature must use the hmac-sha256 format",
    ):
        verify_orchestrated_approval(
            approval,
            shared_secret=SHARED_SECRET,
        )


def test_unsupported_signature_scheme_is_rejected() -> None:
    approval = _signed_approval()
    approval["signature"] = f"sha256:{'0' * 64}"

    with pytest.raises(
        ApprovalAuthenticationError,
        match="signature must use the hmac-sha256 format",
    ):
        verify_orchestrated_approval(
            approval,
            shared_secret=SHARED_SECRET,
        )


@pytest.mark.parametrize(
    "digest",
    [
        "g" * 64,
        "é" * 64,
        "0" * 63,
        "0" * 65,
    ],
)
def test_malformed_signature_digest_is_rejected(digest: str) -> None:
    approval = _signed_approval()
    approval["signature"] = f"hmac-sha256:{digest}"

    with pytest.raises(
        ApprovalAuthenticationError,
        match="exactly 64 hexadecimal characters",
    ):
        verify_orchestrated_approval(
            approval,
            shared_secret=SHARED_SECRET,
        )


def test_wrong_shared_secret_is_rejected() -> None:
    approval = _signed_approval()

    with pytest.raises(
        ApprovalAuthenticationError,
        match="authentication failed",
    ):
        verify_orchestrated_approval(
            approval,
            shared_secret=b"y" * 32,
        )


def test_short_shared_secret_is_rejected() -> None:
    approval = _signed_approval()

    with pytest.raises(
        ApprovalAuthenticationError,
        match="shared secret must contain at least 32 bytes",
    ):
        verify_orchestrated_approval(
            approval,
            shared_secret=b"too-short",
        )
