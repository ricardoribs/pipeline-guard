# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Record an explicit interactive confirmation as a validated local approval."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.approval_validator import (  # noqa: E402
    ApprovalValidationError,
    validate_approval,
)


class LocalApprovalError(ValueError):
    """Raised when the interactive confirmation cannot produce an approval."""


def record_local_approval(
    confirmation: object,
    *,
    output_path: Path,
    max_age: timedelta,
    confirmation_reference: str,
    plugin_data_dir: Path | str | None = None,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    """Validate and save an approval after the person has explicitly confirmed.

    The interactive skill supplies the exact context shown to the person. This
    function never prompts, infers an actor, or chooses an approval lifetime.
    The original confirmation timestamp is copied without refreshing it on retries.
    The canonical approval references the confirmation source rather than itself.
    Its nonce is consumed once in the installation data directory before issuance.
    An explicit data directory takes precedence over CLAUDE_PLUGIN_DATA.
    """
    if not isinstance(confirmation, Mapping):
        raise LocalApprovalError("confirmation must be a JSON object")
    if confirmation.get("confirmed") is not True:
        raise LocalApprovalError("explicit confirmation is required")

    actor = _required_text(confirmation.get("actor"), "actor")
    incident_id = _required_text(confirmation.get("incident_id"), "incident_id")
    diagnosis_hash = _required_text(
        confirmation.get("diagnosis_hash"), "diagnosis_hash"
    )
    base_commit = _required_text(confirmation.get("base_commit"), "base_commit")
    confirmed_at = _required_text(confirmation.get("confirmed_at"), "confirmed_at")
    nonce = _required_text(confirmation.get("nonce"), "nonce")
    source_reference = _required_text(confirmation_reference, "confirmation_reference")
    if re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
        raise LocalApprovalError(
            "nonce must contain exactly 64 lowercase hexadecimal characters"
        )
    now = current_time if current_time is not None else datetime.now(UTC)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise LocalApprovalError("current_time must be a timezone-aware datetime")

    approval = validate_approval(
        {
            "schema_version": "1.0",
            "incident_id": incident_id,
            "diagnosis_hash": diagnosis_hash,
            "base_commit": base_commit,
            "decision": "APPROVED",
            "actor": actor,
            "decided_at": confirmed_at,
            "channel_type": "local",
            "external_reference": source_reference,
        },
        expected_incident_id=incident_id,
        expected_diagnosis_hash=diagnosis_hash,
        expected_base_commit=base_commit,
        max_age=max_age,
        current_time=now,
    )

    serialized = json.dumps(approval, ensure_ascii=False, indent=2) + "\n"
    if plugin_data_dir is None:
        plugin_data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data_dir is None or not str(plugin_data_dir).strip():
        raise LocalApprovalError(
            "plugin_data_dir or CLAUDE_PLUGIN_DATA is required for local nonce consumption"
        )
    data_dir = Path(plugin_data_dir).expanduser()
    if not data_dir.is_absolute():
        raise LocalApprovalError(
            "plugin_data_dir or CLAUDE_PLUGIN_DATA must be an absolute path"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _consume_nonce(nonce, data_dir)
    # An existing approval must not be replaced or have its timestamp refreshed.
    with output_path.open("x", encoding="utf-8") as output:
        output.write(serialized)
    return approval


def _consume_nonce(nonce: str, plugin_data_dir: Path) -> None:
    """Claim a nonce exclusively; failures after claiming never make it reusable."""
    nonce_path = plugin_data_dir / "local-approval-nonces" / nonce
    nonce_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        # Exclusive creation prevents two simultaneous issuers from claiming it.
        consumed = nonce_path.open("x", encoding="utf-8")
    except FileExistsError as error:
        raise LocalApprovalError("confirmation nonce has already been used") from error
    with consumed:
        consumed.write("consumed\n")
        consumed.flush()
        os.fsync(consumed.fileno())


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalApprovalError(f"{field} must be a non-empty string")
    return value


def _approval_lifetime(value: str) -> timedelta:
    try:
        seconds = int(value)
        if seconds <= 0:
            raise ValueError
        return timedelta(seconds=seconds)
    except (ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError(
            "max-age-seconds must be a positive integer"
        ) from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "confirmation", type=Path, help="UTF-8 JSON confirmation from the skill"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="New local approval JSON file"
    )
    parser.add_argument("--max-age-seconds", type=_approval_lifetime, required=True)
    parser.add_argument(
        "--plugin-data-dir",
        type=Path,
        required=True,
        help="Absolute persistent installation data directory",
    )
    arguments = parser.parse_args(argv)

    try:
        confirmation = json.loads(arguments.confirmation.read_text(encoding="utf-8"))
        approval = record_local_approval(
            confirmation,
            output_path=arguments.output,
            max_age=arguments.max_age_seconds,
            confirmation_reference=arguments.confirmation.resolve().as_uri(),
            plugin_data_dir=arguments.plugin_data_dir,
        )
    except (
        LocalApprovalError,
        ApprovalValidationError,
        json.JSONDecodeError,
        UnicodeError,
    ) as error:
        print(f"local approval: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"cannot read or write local approval files: {error}", file=sys.stderr)
        return 1

    print(json.dumps(approval, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
