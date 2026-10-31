"""Allowlisted query boundary for evidence providers."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

_PARAMETER = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")
_MUTATING_SQL = re.compile(
    r"\b(ALTER|ANALYZE|ATTACH|CALL|CREATE|DELETE|DETACH|DROP|EXEC|EXECUTE|GRANT|INSERT|"
    r"MERGE|PRAGMA|REPLACE|REVOKE|TRUNCATE|UPDATE|VACUUM)\b",
    re.IGNORECASE,
)
_LEADING_READ = re.compile(r"\A\s*(SELECT|WITH)\b", re.IGNORECASE)


class ReadOnlyQueryConfigurationError(ValueError):
    """Raised when a query or its limits do not match explicit configuration."""


@dataclass(frozen=True, slots=True)
class QueryExecutionGuarantees:
    """Execution properties a concrete query executor must support and verify."""

    read_only: bool
    bound_parameters: bool
    source_row_limit: bool
    source_timeout: bool

    @property
    def complete(self) -> bool:
        """Whether the executor provides every required safety property."""
        return all(
            (
                self.read_only,
                self.bound_parameters,
                self.source_row_limit,
                self.source_timeout,
            )
        )


class ReadOnlyQueryExecutor(Protocol):
    """Driver adapter with source-enforced, verifiable safety guarantees."""

    @property
    def guarantees(self) -> QueryExecutionGuarantees:
        """Declare capabilities that this concrete driver actually enforces."""
        ...

    def execute_read_only(
        self,
        query: str,
        parameters: Mapping[str, object],
        *,
        max_rows: int,
        timeout_seconds: float,
    ) -> Sequence[Mapping[str, object]]:
        """Execute a read-only query with server-side row and time limits."""
        ...


@dataclass(frozen=True, slots=True)
class ReadOnlyQuerySpec:
    """A trusted query statement and its maximum execution budget."""

    query: str
    max_rows: int
    timeout_seconds: float


class ReadOnlyQueryWrapper:
    """Permit only exact configured SELECT statements and declared parameters."""

    def __init__(
        self,
        executor: ReadOnlyQueryExecutor,
        allowed_queries: Mapping[str, ReadOnlyQuerySpec],
    ) -> None:
        guarantees = getattr(executor, "guarantees", None)
        if (
            not isinstance(guarantees, QueryExecutionGuarantees)
            or not guarantees.complete
        ):
            raise ReadOnlyQueryConfigurationError(
                "Query executor must guarantee read-only execution, bound parameters, "
                "a source-enforced row limit, and a source-enforced timeout."
            )
        self._executor = executor
        self._allowed_queries = dict(allowed_queries)
        for query_id, spec in self._allowed_queries.items():
            if not query_id.strip():
                raise ReadOnlyQueryConfigurationError("Query IDs must not be empty.")
            self._validate_statement(spec.query)
            self._validate_limits(spec.max_rows, spec.timeout_seconds)

    def execute(
        self,
        query_id: str,
        query: str,
        parameters: Mapping[str, object],
    ) -> tuple[Mapping[str, object], ...]:
        """Run a statement only when its ID and exact text match configuration."""
        spec = self._allowed_queries.get(query_id)
        if spec is None:
            raise ReadOnlyQueryConfigurationError(
                f"Query {query_id!r} is not allowlisted."
            )
        self._validate_statement(query)
        if query != spec.query:
            raise ReadOnlyQueryConfigurationError(
                f"Query text does not match the configured statement for {query_id!r}."
            )

        expected_parameters = set(_PARAMETER.findall(spec.query))
        if set(parameters) != expected_parameters:
            raise ReadOnlyQueryConfigurationError(
                f"Query {query_id!r} requires exactly these parameters: "
                f"{', '.join(sorted(expected_parameters)) or '(none)'}."
            )

        rows = self._executor.execute_read_only(
            spec.query,
            parameters,
            max_rows=spec.max_rows,
            timeout_seconds=spec.timeout_seconds,
        )
        return tuple(rows[: spec.max_rows])

    @staticmethod
    def _validate_statement(query: str) -> None:
        if not query.strip() or ";" in query or "--" in query or "/*" in query:
            raise ReadOnlyQueryConfigurationError(
                "Queries must be one configured statement without comments or separators."
            )
        if not _LEADING_READ.match(query) or _MUTATING_SQL.search(query):
            raise ReadOnlyQueryConfigurationError(
                "Only read-only SELECT queries are allowed."
            )

    @staticmethod
    def _validate_limits(max_rows: int, timeout_seconds: float) -> None:
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows <= 0:
            raise ReadOnlyQueryConfigurationError(
                "max_rows must be a positive integer."
            )
        if (
            not isinstance(timeout_seconds, int | float)
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ReadOnlyQueryConfigurationError(
                "timeout_seconds must be a positive number."
            )
