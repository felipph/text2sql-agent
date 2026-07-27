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


def _source_engine_filtered():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE origem (cnpj TEXT, valor REAL)"))
        conn.execute(
            text("INSERT INTO origem (cnpj, valor) VALUES (:c, :v)"),
            [
                {"c": "111", "v": 10.0},
                {"c": "222", "v": 20.0},
                {"c": "111", "v": 5.0},
            ],
        )
    return engine


def test_materialize_append_merges_sources() -> None:
    eng_a = create_engine("sqlite:///:memory:")
    eng_b = create_engine("sqlite:///:memory:")
    with eng_a.begin() as c:
        c.execute(text("CREATE TABLE t_a (cnpj TEXT, valor REAL)"))
        c.execute(text("INSERT INTO t_a VALUES ('111', 10.0)"))
    with eng_b.begin() as c:
        c.execute(text("CREATE TABLE t_b (cnpj TEXT, valor REAL)"))
        c.execute(text("INSERT INTO t_b VALUES ('222', 20.0)"))
    cfg = _table()
    session = DuckDBSession()
    try:
        session.materialize(cfg, eng_a, physical_name="t_a", replace=True)
        session.materialize(cfg, eng_b, physical_name="t_b", append=True)
        rows = session.execute(
            "SELECT cnpj, SUM(valor) AS s FROM origem_logica GROUP BY cnpj ORDER BY cnpj"
        )
        assert [(r["cnpj"], r["s"]) for r in rows] == [("111", 10.0), ("222", 20.0)]
    finally:
        session.close()


def test_materialize_replace_clears_previous() -> None:
    eng = _source_engine_with_rows(3)
    session = DuckDBSession()
    try:
        cfg = _table()
        session.materialize(cfg, eng, physical_name="origem")
        session.materialize(cfg, eng, physical_name="origem", replace=True)
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == 3
    finally:
        session.close()


def test_materialize_filter_sql() -> None:
    eng = _source_engine_filtered()
    session = DuckDBSession()
    try:
        session.materialize(
            _table(), eng, physical_name="origem", filter_sql="cnpj IN ('111')"
        )
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == 2
    finally:
        session.close()


def test_materialize_append_and_replace_raise() -> None:
    eng = _source_engine_with_rows(1)
    session = DuckDBSession()
    try:
        with pytest.raises(ValueError, match="mutuamente"):
            session.materialize(
                _table(), eng, physical_name="origem", append=True, replace=True
            )
    finally:
        session.close()
