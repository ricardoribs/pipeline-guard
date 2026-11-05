from __future__ import annotations

from scripts.check_schema_compatibility import schema_compatibility_issues


def test_optional_fields_can_be_added_without_breaking_compatibility() -> None:
    previous = {
        "type": "object",
        "required": ["incident_id"],
        "properties": {"incident_id": {"type": "string"}},
    }
    current = {
        "type": "object",
        "required": ["incident_id"],
        "properties": {
            "incident_id": {"type": "string"},
            "host_run": {"type": "object"},
        },
    }

    assert schema_compatibility_issues(previous, current) == []


def test_removing_a_property_is_reported() -> None:
    previous = {"properties": {"incident_id": {"type": "string"}}}
    current = {"properties": {}}

    assert schema_compatibility_issues(previous, current) == [
        "$.properties.incident_id: property was removed"
    ]


def test_making_an_optional_field_required_is_reported() -> None:
    previous = {"properties": {"host_run": {"type": "object"}}}
    current = {
        "required": ["host_run"],
        "properties": {"host_run": {"type": "object"}},
    }

    assert schema_compatibility_issues(previous, current) == [
        "$: newly required fields: host_run"
    ]


def test_nested_required_changes_are_reported() -> None:
    previous = {
        "properties": {
            "host_run": {"properties": {"run_reference": {"type": "string"}}}
        }
    }
    current = {
        "properties": {
            "host_run": {
                "required": ["run_reference"],
                "properties": {"run_reference": {"type": "string"}},
            }
        }
    }

    assert schema_compatibility_issues(previous, current) == [
        "$.properties.host_run: newly required fields: run_reference"
    ]
