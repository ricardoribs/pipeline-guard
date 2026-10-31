# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Cooperative investigation budget, read from ``runtime.execution_budget``.

Wrap investigation tools with ``call_tool``, account for total input and output
tokens with ``record_tokens``, and accept results only through ``validate_diagnosis``.
Checks run before and after tools, on token accounting, and before accepting a
diagnosis. In-flight operations must use ``remaining_seconds`` as their timeout;
this module does not forcibly terminate a running tool or model request.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from time import monotonic
from typing import Any, TypeVar

try:  # Support both direct script execution and package-style imports.
    from .diagnosis_validator import validate_diagnosis
except ImportError:  # pragma: no cover - direct script execution
    from diagnosis_validator import validate_diagnosis

_Result = TypeVar("_Result")
_LIMIT_FIELDS = frozenset({"max_tool_calls", "max_seconds", "max_tokens"})


class ExecutionBudgetConfigurationError(ValueError):
    """Raised when a project does not provide explicit valid investigation limits."""


class ExecutionBudgetExceeded(RuntimeError):
    """Raised to stop investigation once a configured limit has been reached."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Investigation budget exhausted: {reason}")


class ExecutionBudget:
    """Track one diagnosis run; an exhausted budget cannot be resumed or reset."""

    def __init__(
        self,
        project_config: Mapping[str, Any],
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        runtime = project_config.get("runtime")
        limits = runtime.get("execution_budget") if isinstance(runtime, Mapping) else None
        if not isinstance(limits, Mapping) or set(limits) != _LIMIT_FIELDS:
            raise ExecutionBudgetConfigurationError(
                "runtime.execution_budget must contain max_tool_calls, max_seconds and max_tokens"
            )
        for name in sorted(_LIMIT_FIELDS):
            value = limits[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ExecutionBudgetConfigurationError(f"{name} must be a positive integer")

        self._max_tool_calls: int = limits["max_tool_calls"]
        self._max_seconds: int = limits["max_seconds"]
        self._max_tokens: int = limits["max_tokens"]
        self._clock = clock
        self._started_at = clock()
        self._tool_calls = 0
        self._tokens = 0
        self._exhausted_reason: str | None = None

    @property
    def remaining_seconds(self) -> float:
        """Remaining wall time for timeouts on tools or model requests."""
        return max(0.0, self._max_seconds - (self._clock() - self._started_at))

    @property
    def remaining_tokens(self) -> int:
        """Remaining token allowance, including input and output tokens."""
        return max(0, self._max_tokens - self._tokens)

    def check(self) -> None:
        """Stop at reached time/token limits or a previously rejected tool call."""
        if self._exhausted_reason is None:
            if self.remaining_seconds <= 0:
                self._exhausted_reason = "max_seconds"
            elif self._tokens >= self._max_tokens:
                self._exhausted_reason = "max_tokens"
        if self._exhausted_reason is not None:
            raise ExecutionBudgetExceeded(self._exhausted_reason)

    def call_tool(self, tool: Callable[[], _Result]) -> _Result:
        """Return up to ``max_tool_calls`` results and reject the next call."""
        self.check()
        if self._tool_calls >= self._max_tool_calls:
            self._exhausted_reason = "max_tool_calls"
            raise ExecutionBudgetExceeded(self._exhausted_reason)
        self._tool_calls += 1
        result = tool()
        self.check()
        return result

    def record_tokens(self, tokens: int) -> None:
        """Account for input plus output usage, stopping at the configured limit."""
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError("tokens must be a non-negative integer")
        self.check()
        self._tokens += tokens
        self.check()

    def validate_diagnosis(
        self,
        diagnosis: object,
        *,
        incident_id: str,
        evidences: Iterable[Mapping[str, object]],
    ) -> dict[str, Any]:
        """Use the existing validator to replace exhausted results with UNKNOWN."""
        try:
            self.check()
        except ExecutionBudgetExceeded:
            exhausted = True
        else:
            exhausted = False
        return validate_diagnosis(
            diagnosis,
            incident_id=incident_id,
            evidences=evidences,
            investigation_budget_exhausted=exhausted,
        )
