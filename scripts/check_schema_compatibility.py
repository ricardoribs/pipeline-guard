"""Check additive compatibility for versioned JSON Schema contracts."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def schema_compatibility_issues(previous: object, current: object) -> list[str]:
    """Report removed properties and newly required fields in a schema change."""
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return ["both schema documents must have an object at the root"]

    issues: list[str] = []
    _compare_schema_node(previous, current, "$", issues)
    return issues


def _compare_schema_node(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    path: str,
    issues: list[str],
) -> None:
    previous_required = previous.get("required", [])
    current_required = current.get("required", [])
    if isinstance(previous_required, list) and isinstance(current_required, list):
        added_required = sorted(set(current_required) - set(previous_required))
        if added_required:
            issues.append(
                f"{path}: newly required fields: {', '.join(map(str, added_required))}"
            )

    previous_properties = previous.get("properties", {})
    current_properties = current.get("properties", {})
    if isinstance(previous_properties, Mapping) and isinstance(
        current_properties, Mapping
    ):
        for name, previous_property in previous_properties.items():
            child_path = f"{path}.properties.{name}"
            if name not in current_properties:
                issues.append(f"{child_path}: property was removed")
                continue
            current_property = current_properties[name]
            if isinstance(previous_property, Mapping) and isinstance(
                current_property, Mapping
            ):
                _compare_schema_node(
                    previous_property, current_property, child_path, issues
                )

    for keyword in (
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedProperties",
    ):
        previous_child = previous.get(keyword)
        current_child = current.get(keyword)
        if isinstance(previous_child, Mapping) and isinstance(current_child, Mapping):
            _compare_schema_node(
                previous_child, current_child, f"{path}.{keyword}", issues
            )

    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        previous_children = previous.get(keyword)
        current_children = current.get(keyword)
        if not isinstance(previous_children, list) or not isinstance(
            current_children, list
        ):
            continue
        for index, previous_child in enumerate(previous_children):
            if index >= len(current_children):
                break
            current_child = current_children[index]
            if isinstance(previous_child, Mapping) and isinstance(
                current_child, Mapping
            ):
                _compare_schema_node(
                    previous_child,
                    current_child,
                    f"{path}.{keyword}[{index}]",
                    issues,
                )
        for index in range(len(previous_children), len(current_children)):
            current_child = current_children[index]
            if isinstance(current_child, Mapping):
                _compare_schema_node(
                    {}, current_child, f"{path}.{keyword}[{index}]", issues
                )


def _load_schema(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top-level schema value must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        print(
            "usage: check_schema_compatibility.py PREVIOUS_SCHEMA CURRENT_SCHEMA",
            file=sys.stderr,
        )
        return 2

    previous_path, current_path = map(Path, arguments)
    try:
        issues = schema_compatibility_issues(
            _load_schema(previous_path), _load_schema(current_path)
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if issues:
        print("Schema compatibility check failed:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    print("Incident schema change is backward compatible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
