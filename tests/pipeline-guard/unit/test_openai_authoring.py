from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.execution_budget import ExecutionBudgetExceeded  # noqa: E402
from scripts.model_backend import ModelSettings  # noqa: E402
from scripts.openai_authoring import (  # noqa: E402
    DirectAuthoringError,
    DirectAuthoringLimitReached,
    run_openai_authoring,
)

sys.path.remove(str(PLUGIN_ROOT))


class FakeBudget:
    def __init__(self, *, max_tool_calls: int = 100) -> None:
        self.remaining_seconds = 60.0
        self.remaining_tokens = 100_000
        self.recorded: list[int] = []
        self.max_tool_calls = max_tool_calls
        self.tool_calls = 0

    def check(self) -> None:
        return None

    def record_tokens(self, tokens: int) -> None:
        self.recorded.append(tokens)
        self.remaining_tokens -= tokens

    def call_tool(self, operation):
        self.tool_calls += 1
        if self.tool_calls > self.max_tool_calls:
            raise ExecutionBudgetExceeded("max_tool_calls")
        return operation()


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document

    def json(self) -> object:
        return self.document


class FakeClient:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = iter(documents)
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        return FakeResponse(next(self.documents))


def _tool_response(
    call_id: str, name: str, arguments: dict[str, str]
) -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"completion_tokens": 10},
    }


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "models").mkdir()
    (tmp_path / "config" / "pipeline-guard.yaml").write_text(
        yaml.safe_dump(
            {
                "safety": {
                    "editable_paths": ["models/**"],
                    "editable_extensions": [".sql"],
                    "sensitive_paths": ["secrets/**"],
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "models" / "orders.sql").write_text(
        "select old_value\n", encoding="utf-8"
    )
    return tmp_path


def _settings(*, max_output_tokens: int | None = None) -> ModelSettings:
    return ModelSettings(
        profile="ollama",
        provider="openai-compatible",
        model="llama-test",
        base_url="http://127.0.0.1:11434/v1",
        api_key=None,
        max_output_tokens=max_output_tokens,
    )


def test_direct_authoring_exposes_only_bounded_file_tools(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    client = FakeClient(
        [
            _tool_response("read-1", "read_file", {"file_path": "models/orders.sql"}),
            _tool_response(
                "edit-1",
                "edit_file",
                {
                    "file_path": "models/orders.sql",
                    "old_text": "old_value",
                    "new_text": "new_value",
                },
            ),
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Repair completed."},
                    }
                ],
                "usage": {"completion_tokens": 5},
            },
        ]
    )

    result = run_openai_authoring(
        workspace,
        prompt="Repair the confirmed failure.",
        agent_prompt="Use only the supplied tools.",
        approved_read_paths=["models/orders.sql"],
        settings=_settings(),
        budget=FakeBudget(),
        max_turns=3,
        client=client,
    )

    assert (workspace / "models" / "orders.sql").read_text(encoding="utf-8") == (
        "select new_value\n"
    )
    assert result.result["provider"] == "ollama"
    exposed_tools = client.requests[0]["json"]["tools"]
    assert {tool["function"]["name"] for tool in exposed_tools} == {
        "read_file",
        "write_file",
        "edit_file",
    }


def test_direct_authoring_respects_per_request_output_limit(tmp_path: Path) -> None:
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "No repair required."},
                    }
                ],
                "usage": {"completion_tokens": 5},
            }
        ]
    )

    run_openai_authoring(
        _workspace(tmp_path),
        prompt="Inspect the failure.",
        agent_prompt="Use only the supplied tools.",
        approved_read_paths=["models/orders.sql"],
        settings=_settings(max_output_tokens=1024),
        budget=FakeBudget(),
        max_turns=1,
        client=client,
    )

    assert client.requests[0]["json"]["max_tokens"] == 1024


def test_direct_authoring_fails_closed_outside_editable_paths(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    client = FakeClient(
        [
            _tool_response(
                "write-1",
                "write_file",
                {"file_path": "README.md", "content": "not allowed"},
            )
        ]
    )

    with pytest.raises(DirectAuthoringError, match="outside safety.editable_paths"):
        run_openai_authoring(
            workspace,
            prompt="Repair the confirmed failure.",
            agent_prompt="Use only the supplied tools.",
            approved_read_paths=["models/orders.sql"],
            settings=_settings(),
            budget=FakeBudget(),
            max_turns=1,
            client=client,
        )

    assert not (workspace / "README.md").exists()


def test_direct_authoring_enforces_the_shared_tool_call_limit(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    response = _tool_response("read-1", "read_file", {"file_path": "models/orders.sql"})
    tool_calls = response["choices"][0]["message"]["tool_calls"]
    tool_calls.append(
        {
            "id": "read-2",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"file_path": "models/orders.sql"}),
            },
        }
    )
    budget = FakeBudget(max_tool_calls=1)

    with pytest.raises(DirectAuthoringLimitReached) as captured:
        run_openai_authoring(
            workspace,
            prompt="Repair the confirmed failure.",
            agent_prompt="Use only the supplied tools.",
            approved_read_paths=["models/orders.sql"],
            settings=_settings(),
            budget=budget,
            max_turns=1,
            client=FakeClient([response]),
        )

    assert captured.value.reason_code == "TOOL_CALL_LIMIT"
    assert budget.tool_calls == 2
