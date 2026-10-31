from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.execution_budget import (  # noqa: E402
    ExecutionBudget,
    ExecutionBudgetConfigurationError,
    ExecutionBudgetExceeded,
)
from scripts.validate_config import ConfigValidationError, validate_config  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _config(**limits: object) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "pipeline": {"adapter": "mock"},
        "runtime": {
            "execution_budget": {
                "max_tool_calls": 3,
                "max_seconds": 10,
                "max_tokens": 100,
                **limits,
            }
        },
    }


def _diagnosis() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "incident_id": "incident-budget",
        "failure_family": "CODE",
        "confidence": "HIGH",
        "cause_status": "CONFIRMED",
        "cause": "A model failed.",
        "evidence_citations": [
            {"evidence_id": "evidence-1", "claim": "Model failure."}
        ],
    }


def _validate(budget: ExecutionBudget) -> dict[str, Any]:
    return budget.validate_diagnosis(
        _diagnosis(),
        incident_id="incident-budget",
        evidences=[{"evidence_id": "evidence-1"}],
    )


def _assert_unknown(budget: ExecutionBudget) -> None:
    result = _validate(budget)
    assert result["cause_status"] == "UNKNOWN"
    assert result["failure_family"] == "UNKNOWN"
    assert result["confidence"] == "LOW"
    assert result["evidence_citations"] == []
    assert result["limitations"] == ["investigation_budget_exhausted"]
    assert result["cause"] != _diagnosis()["cause"]
    schema = json.loads(
        (PLUGIN_ROOT / "contracts/schema/pipeline_diagnosis_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(result)


def test_limits_are_read_from_validated_project_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "project.yaml"
    config_path.write_text(yaml.safe_dump(_config(max_tokens=7)), encoding="utf-8")
    budget = ExecutionBudget(validate_config(config_path), clock=FakeClock())

    budget.record_tokens(6)
    assert _validate(budget) == _diagnosis()
    with pytest.raises(ExecutionBudgetExceeded, match="max_tokens"):
        budget.record_tokens(1)
    _assert_unknown(budget)


@pytest.mark.parametrize("preset_name", ["generic.yaml", "dbt.yaml"])
def test_shipped_presets_define_a_usable_execution_budget(preset_name: str) -> None:
    config = validate_config(PLUGIN_ROOT / "config" / "presets" / preset_name)
    budget = ExecutionBudget(config, clock=FakeClock())

    assert budget.remaining_seconds == 600
    assert budget.remaining_tokens == 32000
    assert budget.call_tool(lambda: "evidence") == "evidence"


def test_tools_stop_at_limit_and_no_further_tool_is_called() -> None:
    budget = ExecutionBudget(_config(max_tool_calls=2), clock=FakeClock())
    calls: list[str] = []

    def tool() -> str:
        calls.append("called")
        return "evidence"

    assert budget.call_tool(tool) == "evidence"
    assert budget.call_tool(tool) == "evidence"
    with pytest.raises(ExecutionBudgetExceeded, match="max_tool_calls"):
        budget.call_tool(tool)
    assert calls == ["called", "called"]
    _assert_unknown(budget)


def test_single_tool_limit_returns_first_result_and_blocks_second_call() -> None:
    budget = ExecutionBudget(_config(max_tool_calls=1), clock=FakeClock())
    calls: list[str] = []

    assert budget.call_tool(lambda: calls.append("first") or "evidence") == "evidence"
    assert _validate(budget) == _diagnosis()
    with pytest.raises(ExecutionBudgetExceeded, match="max_tool_calls"):
        budget.call_tool(lambda: calls.append("second"))
    assert calls == ["first"]
    _assert_unknown(budget)


def test_failed_tool_attempt_also_consumes_budget() -> None:
    budget = ExecutionBudget(_config(max_tool_calls=1), clock=FakeClock())

    def tool() -> None:
        raise OSError("Evidence source unavailable")

    with pytest.raises(OSError, match="unavailable"):
        budget.call_tool(tool)
    with pytest.raises(ExecutionBudgetExceeded, match="max_tool_calls"):
        budget.call_tool(tool)
    _assert_unknown(budget)


@pytest.mark.parametrize("elapsed", [10, 11])
def test_time_limit_blocks_tools_and_speculative_diagnosis(elapsed: int) -> None:
    clock = FakeClock()
    budget = ExecutionBudget(_config(), clock=clock)
    clock.now += elapsed
    calls: list[str] = []

    with pytest.raises(ExecutionBudgetExceeded, match="max_seconds"):
        budget.call_tool(lambda: calls.append("must not run"))
    assert calls == []
    assert budget.remaining_seconds == 0
    _assert_unknown(budget)


def test_time_spent_inside_tool_discards_result() -> None:
    clock = FakeClock()
    budget = ExecutionBudget(_config(), clock=clock)

    def tool() -> str:
        clock.now += 10
        return "late evidence"

    with pytest.raises(ExecutionBudgetExceeded, match="max_seconds"):
        budget.call_tool(tool)
    _assert_unknown(budget)


@pytest.mark.parametrize("last_usage", [60, 61])
def test_cumulative_tokens_stop_at_or_above_limit(last_usage: int) -> None:
    budget = ExecutionBudget(_config(), clock=FakeClock())
    budget.record_tokens(40)
    assert budget.remaining_tokens == 60

    with pytest.raises(ExecutionBudgetExceeded, match="max_tokens"):
        budget.record_tokens(last_usage)
    assert budget.remaining_tokens == 0
    _assert_unknown(budget)


def test_exhaustion_remains_latched() -> None:
    clock = FakeClock()
    budget = ExecutionBudget(_config(), clock=clock)
    clock.now += 10
    with pytest.raises(ExecutionBudgetExceeded):
        budget.check()
    clock.now -= 10
    _assert_unknown(budget)


@pytest.mark.parametrize("usage", [-1, True, 1.5, "10"])
def test_invalid_token_usage_does_not_change_budget(usage: Any) -> None:
    budget = ExecutionBudget(_config(), clock=FakeClock())
    with pytest.raises(ValueError, match="tokens"):
        budget.record_tokens(usage)
    assert budget.remaining_tokens == 100
    assert _validate(budget) == _diagnosis()


@pytest.mark.parametrize("field", ["max_tool_calls", "max_seconds", "max_tokens"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10", None])
def test_invalid_project_limits_are_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    config = _config(**{field: value})
    with pytest.raises(ExecutionBudgetConfigurationError, match=field):
        ExecutionBudget(config)
    config_path = tmp_path / "invalid-budget.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        validate_config(config_path)


@pytest.mark.parametrize("field", ["max_tool_calls", "max_seconds", "max_tokens"])
def test_incomplete_budget_is_rejected(tmp_path: Path, field: str) -> None:
    config = _config()
    del config["runtime"]["execution_budget"][field]
    with pytest.raises(ExecutionBudgetConfigurationError, match="execution_budget"):
        ExecutionBudget(config)
    config_path = tmp_path / "incomplete-budget.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        validate_config(config_path)


def test_missing_budget_has_no_silent_unlimited_default(tmp_path: Path) -> None:
    with pytest.raises(ExecutionBudgetConfigurationError, match="execution_budget"):
        ExecutionBudget({"runtime": {}})

    config_path = tmp_path / "missing-budget.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "pipeline": {"adapter": "mock"},
                "runtime": {"artifact_store": "local"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError) as error:
        validate_config(config_path)

    assert error.value.field == "$.runtime.execution_budget"


def test_unknown_budget_field_is_rejected(tmp_path: Path) -> None:
    config = _config(max_token=5)
    with pytest.raises(ExecutionBudgetConfigurationError, match="execution_budget"):
        ExecutionBudget(config)
    config_path = tmp_path / "unknown-budget-field.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        validate_config(config_path)
