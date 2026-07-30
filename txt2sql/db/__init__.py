"""Subpacote de acesso a dados: registry, schema, sharding e camada DuckDB."""

from __future__ import annotations

from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.db.fan_in import fan_in
from txt2sql.db.materialize import MaterializeOutcome, materialize_tables
from txt2sql.db.registry import DatabaseRegistry
from txt2sql.db.schema import SchemaLoader
from txt2sql.db.session_store import DuckDBSessionStore

__all__ = [
    "DatabaseRegistry",
    "DuckDBSession",
    "DuckDBSessionStore",
    "MaterializeOutcome",
    "SchemaLoader",
    "fan_in",
    "materialize_tables",
]
