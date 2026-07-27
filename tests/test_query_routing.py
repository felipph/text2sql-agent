"""Testes de roteamento fail-closed (shard lógico + cross-database)."""

from __future__ import annotations

from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    DatabaseConfig,
    DuckDBConfig,
    ShardingConfig,
    ShardResult,
    TableConfig,
)
from txt2sql.query_routing import analyze_table_refs, routing_rejection_reason


def _config() -> AgentConfig:
    return AgentConfig(
        databases=[
            DatabaseConfig(id="db_main", connection_string="sqlite:///:memory:"),
            DatabaseConfig(id="db_shard_1", connection_string="sqlite:///:memory:"),
        ],
        tables=[
            TableConfig(
                id="clientes",
                database="db_main",
                name="clientes",
            ),
            TableConfig(
                id="recebiveis",
                database="db_main",
                name="recebiveis",
                sharding=ShardingConfig(
                    discriminator_column="cnpj",
                    resolver="playground.shard_resolver:resolve_cnpj_shard",
                ),
                columns=[ColumnConfig(name="cnpj"), ColumnConfig(name="valor")],
                duckdb=DuckDBConfig(enabled=True, trigger="aggregation"),
            ),
        ],
        dialect="postgres",
    )


def test_reject_logical_sharded_join_with_main() -> None:
    cfg = _config()
    sql = (
        "SELECT DISTINCT clientes.razao_social FROM clientes "
        "INNER JOIN recebiveis ON clientes.cnpj = recebiveis.cnpj "
        "WHERE recebiveis.status = 'vencido' LIMIT 50"
    )
    refs = analyze_table_refs(sql, cfg, {}, {}, "postgres")
    reason = routing_rejection_reason(refs)
    assert reason is not None
    assert "shardada" in reason.lower() or "resolve_shard" in reason


def test_reject_cross_db_after_resolve() -> None:
    cfg = _config()
    resolved = {
        ("recebiveis", "123"): ShardResult(
            database_id="db_shard_1", table_name="recebiveis_123"
        )
    }
    sql = (
        "SELECT c.razao_social FROM clientes c "
        "JOIN recebiveis_123 r ON c.cnpj = r.cnpj"
    )
    refs = analyze_table_refs(sql, cfg, resolved, {}, "postgres")
    reason = routing_rejection_reason(refs)
    assert reason is not None
    assert "cross-database" in reason.lower() or "distintos" in reason.lower()


def test_allow_single_shard_physical_query() -> None:
    cfg = _config()
    resolved = {
        ("recebiveis", "123"): ShardResult(
            database_id="db_shard_1", table_name="recebiveis_123"
        )
    }
    sql = "SELECT SUM(valor) FROM recebiveis_123 WHERE cnpj = 'x'"
    refs = analyze_table_refs(sql, cfg, resolved, {}, "postgres")
    assert routing_rejection_reason(refs) is None


def test_allow_multi_logical_only_on_duckdb() -> None:
    cfg = _config()
    multi = {"recebiveis": {"values": ["a", "b"], "truncated": False}}
    sql = "SELECT SUM(valor) FROM recebiveis WHERE cnpj IN ('a','b')"
    refs = analyze_table_refs(sql, cfg, {}, multi, "postgres")
    assert routing_rejection_reason(refs) is None


def test_reject_multi_logical_joined_with_main() -> None:
    cfg = _config()
    multi = {"recebiveis": {"values": ["a", "b"], "truncated": False}}
    sql = (
        "SELECT c.razao_social FROM clientes c "
        "JOIN recebiveis r ON c.cnpj = r.cnpj"
    )
    refs = analyze_table_refs(sql, cfg, {}, multi, "postgres")
    reason = routing_rejection_reason(refs)
    assert reason is not None
