"""Subpacote de acesso a dados: registry, schema, sharding e camada DuckDB."""

from __future__ import annotations

from txt2sql.db.duckdb_layer import DuckDBSession, needs_duckdb
from txt2sql.db.registry import DatabaseRegistry
from txt2sql.db.schema import SchemaLoader
from txt2sql.db.shard import ShardResolver

__all__ = [
    "DatabaseRegistry",
    "SchemaLoader",
    "ShardResolver",
    "DuckDBSession",
    "needs_duckdb",
]
