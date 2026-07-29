"""Testes do Result Compactor (S6)."""

from __future__ import annotations

from txt2sql.artifacts import Budget
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.middleware import compact_result


def test_truncated_with_session_sets_full_result_ref_and_table_exists() -> None:
    rows = [{"id": i, "name": f"n{i}"} for i in range(30)]
    budget = Budget(sample_rows=5)
    session = DuckDBSession()

    result = compact_result(rows, budget, session=session)

    assert result.truncated is True
    assert result.full_result_ref == "duckdb://result_1"
    assert len(result.sample) == 5
    stored = session.execute("SELECT COUNT(*) AS n FROM result_1")
    assert stored[0]["n"] == 30


def test_truncated_without_session_keeps_full_result_ref_none() -> None:
    rows = [{"id": i} for i in range(30)]
    budget = Budget(sample_rows=5)

    result = compact_result(rows, budget)

    assert result.truncated is True
    assert result.full_result_ref is None


def test_empty_result_warning_when_metrics_expected() -> None:
    result = compact_result([], Budget(), intent_had_metrics=True)

    assert result.row_count == 0
    assert "EMPTY_RESULT" in result.warnings


def test_no_warning_when_empty_without_metrics() -> None:
    result = compact_result([], Budget(), intent_had_metrics=False)

    assert result.row_count == 0
    assert "EMPTY_RESULT" not in result.warnings
    assert result.warnings == []
