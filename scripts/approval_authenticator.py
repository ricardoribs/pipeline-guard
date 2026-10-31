# /// script
# requires-python = ">=3.12"
# ///
"""Authentication boundary for orchestrated remediation approvals."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any, cast

_DOMAIN_SEPARATOR = b"pipeline-guard:approval_input_v1:v1\n"
_SIGNATURE_PREFIX = "hmac-sha256:"
_MIN_SECRET_BYTES = 32
_SHA256_HEX_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


class ApprovalAuthenticationError(ValueError):
    """Raised when an orchestrated approval cannot be authenticated."""


def canonical_approval_bytes(
    approval: Mapping[str, object],
) -> bytes:
    """Return the domain-separated canonical representation to authenticate."""
    unsigned = {key: value for key, value in approval.items() if key != "signature"}
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return _DOMAIN_SEPARATOR + canonical


def verify_orchestrated_approval(
    approval: object,
    *,
    shared_secret: bytes,
) -> dict[str, Any]:
    """Authenticate an orchestrated approval before semantic validation."""
    if not isinstance(approval, Mapping):
        raise ApprovalAuthenticationError("approval must be a mapping")

    if not isinstance(shared_secret, bytes) or len(shared_secret) < _MIN_SECRET_BYTES:
        raise ApprovalAuthenticationError("shared secret must contain at least 32 bytes")

    document = dict(cast(Mapping[str, Any], approval))
    signature = document.get("signature")
    if not isinstance(signature, str) or not signature.startswith(_SIGNATURE_PREFIX):
        raise ApprovalAuthenticationError("approval signature must use the hmac-sha256 format")

    provided_digest = signature.removeprefix(_SIGNATURE_PREFIX)
    if _SHA256_HEX_PATTERN.fullmatch(provided_digest) is None:
        raise ApprovalAuthenticationError(
            "approval signature must contain exactly 64 hexadecimal characters"
        )

    provided_digest_bytes = bytes.fromhex(provided_digest)
    expected_digest = hmac.new(
        shared_secret,
        canonical_approval_bytes(document),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected_digest, provided_digest_bytes):
        raise ApprovalAuthenticationError("orchestrated approval authentication failed")

    return document
