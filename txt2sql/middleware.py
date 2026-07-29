"""Middleware transversal: factories de ExecutionResult e compactação de amostra."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from txt2sql.artifacts import Budget, ExecutionResult

if TYPE_CHECKING:
    from txt2sql.db.duckdb_layer import DuckDBSession


def result_from_timeout(message: str) -> ExecutionResult:
    return ExecutionResult(status="timeout", error=message)


def result_from_rejection(message: str) -> ExecutionResult:
    return ExecutionResult(status="rejected", error=message)


def compact_result(
    rows: list[dict],
    budget: Budget,
    *,
    schema: list[dict] | None = None,
    warnings: list[str] | None = None,
    session: DuckDBSession | None = None,
    expected_shape: Literal["scalar", "row", "table"] | None = None,
    intent_had_metrics: bool = False,
) -> ExecutionResult:
    """Compacta linhas para amostra no budget; overflow opcionalmente no DuckDB."""
    sample_limit = budget.sample_rows
    row_count = len(rows)
    truncated = row_count > sample_limit
    out_warnings = list(warnings or [])

    if intent_had_metrics and row_count == 0:
        out_warnings.append("EMPTY_RESULT")

    if expected_shape == "scalar" and row_count != 1:
        out_warnings.append(
            f"SHAPE_MISMATCH: expected scalar, got {row_count} row(s)"
        )

    full_result_ref: str | None = None
    if truncated and session is not None:
        full_result_ref = session.store_result_rows(rows)

    return ExecutionResult(
        status="ok",
        row_count=row_count,
        schema_=schema or [],
        sample=rows[:sample_limit],
        truncated=truncated,
        full_result_ref=full_result_ref,
        warnings=out_warnings,
    )
