"""Testes da camada DuckDB intermediária."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from txt2sql.config import DuckDBConfig, TableConfig
from txt2sql.db import duckdb_layer
from txt2sql.db.duckdb_layer import DuckDBSession


def _source_engine_with_rows(n: int):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE origem (id INTEGER, valor REAL)"))
        conn.execute(
            text("INSERT INTO origem (id, valor) VALUES (:id, :valor)"),
            [{"id": i, "valor": float(i)} for i in range(n)],
        )
    return engine


def _table(fetch_limit: int = 100_000) -> TableConfig:
    return TableConfig(
        id="origem_logica",
        database="db",
        name="origem",
        duckdb=DuckDBConfig(enabled=True, trigger="always", fetch_limit=fetch_limit),
    )


def test_materialize_streams_multiple_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duckdb_layer, "BATCH_SIZE", 10)
    n = 25  # > BATCH_SIZE → pelo menos 3 lotes
    engine = _source_engine_with_rows(n)
    session = DuckDBSession()
    try:
        session.materialize(_table(), engine, physical_name="origem")
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == n
        total = session.execute("SELECT SUM(valor) AS s FROM origem_logica")
        assert total[0]["s"] == sum(range(n))
    finally:
        session.close()


def test_materialize_empty_table() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE origem (id INTEGER, nome TEXT)"))
    session = DuckDBSession()
    try:
        session.materialize(_table(), engine, physical_name="origem")
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == 0
        assert session.is_materialized("origem_logica")
    finally:
        session.close()


def test_materialize_is_idempotent() -> None:
    engine = _source_engine_with_rows(3)
    session = DuckDBSession()
    try:
        cfg = _table()
        session.materialize(cfg, engine, physical_name="origem")
        session.materialize(cfg, engine, physical_name="origem")
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == 3
    finally:
        session.close()


def test_materialize_respects_fetch_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duckdb_layer, "BATCH_SIZE", 10)
    engine = _source_engine_with_rows(50)
    session = DuckDBSession()
    try:
        session.materialize(_table(fetch_limit=15), engine, physical_name="origem")
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == 15
    finally:
        session.close()
