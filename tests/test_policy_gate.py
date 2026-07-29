"""Testes offline do Policy Gate (S5)."""

from __future__ import annotations

from txt2sql.artifacts import ShardBinding, ShardRouting, SQLPlan
from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    DatabaseConfig,
    DuckDBConfig,
    ShardingConfig,
    TableConfig,
)
from txt2sql.policy import check_sql_plan


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
                duckdb=DuckDBConfig(enabled=True, force_analytical=True),
            ),
        ],
        dialect="postgres",
    )


def test_rejects_dml() -> None:
    cfg = _config()
    plan = SQLPlan(sql="DELETE FROM clientes", dialect="postgres")
    d = check_sql_plan(plan, config=cfg, shard_routing=ShardRouting(), path="simple")
    assert d.status == "rejected"
    assert d.error


def test_rejects_unresolved_sharded_logical_name() -> None:
    cfg = _config()
    plan = SQLPlan(sql="SELECT * FROM recebiveis", dialect="postgres")
    d = check_sql_plan(plan, config=cfg, shard_routing=ShardRouting(), path="simple")
    assert d.status == "rejected"
    assert d.error
    assert "shardada" in d.error.lower() or "resolve_shard" in d.error


def test_rejects_aggregation_on_source_when_force_analytical() -> None:
    cfg = _config()
    routing = ShardRouting(
        mode="single",
        bindings=[
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="001",
                database_id="db_shard_1",
                physical_table="recebiveis_001",
            )
        ],
    )
    plan = SQLPlan(
        sql="SELECT cnpj, SUM(valor) FROM recebiveis_001 GROUP BY cnpj",
        dialect="postgres",
    )
    d = check_sql_plan(
        plan,
        config=cfg,
        shard_routing=routing,
        context="source_extract",
    )
    assert d.status == "rejected"
    assert "force_analytical" in (d.error or "").lower() or "pushdown" in (
        d.error or ""
    ).lower()


def test_injects_limit_when_missing() -> None:
    cfg = _config()
    plan = SQLPlan(sql="SELECT * FROM clientes", dialect="postgres")
    d = check_sql_plan(plan, config=cfg, shard_routing=ShardRouting(), max_rows=100)
    assert d.status == "ok"
    assert "limit" in d.sql.lower()
    assert "100" in d.sql


def test_ok_with_binding_and_existing_limit() -> None:
    cfg = _config()
    routing = ShardRouting(
        mode="single",
        bindings=[
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="001",
                database_id="db_shard_1",
                physical_table="recebiveis_001",
            )
        ],
    )
    plan = SQLPlan(
        sql="SELECT cnpj, valor FROM recebiveis_001 LIMIT 50",
        dialect="postgres",
    )
    d = check_sql_plan(plan, config=cfg, shard_routing=routing, max_rows=100)
    assert d.status == "ok"
    assert "limit 50" in d.sql.lower() or "limit 50" in d.sql.replace("\n", " ").lower()


def test_rejects_physical_shard_names_when_logical_in_duckdb_catalog() -> None:
    """Trace Langfuse: LLM gerou UNION recebiveis_654/747 + JOIN clientes no DuckDB."""
    from txt2sql.artifacts import DuckDBCatalog, DuckDBTableInfo

    cfg = _config()
    routing = ShardRouting(
        mode="multi",
        logical_table="recebiveis",
        bindings=[
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="65410433218196",
                database_id="db_shard_2",
                physical_table="recebiveis_654",
            ),
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="74778161849593",
                database_id="db_shard_2",
                physical_table="recebiveis_747",
            ),
        ],
    )
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(name="recebiveis", row_count=10),
            DuckDBTableInfo(name="clientes", row_count=2),
        ]
    )
    plan = SQLPlan(
        sql=(
            "WITH recebiveis_unificados AS ("
            " SELECT * FROM recebiveis_654 UNION ALL SELECT * FROM recebiveis_747)"
            " SELECT r.cnpj, c.razao_social AS nome, SUM(r.valor) AS valor"
            " FROM recebiveis_unificados r"
            " LEFT JOIN clientes c ON r.cnpj = c.cnpj"
            " GROUP BY r.cnpj, c.razao_social"
        ),
        dialect="duckdb",
    )
    d = check_sql_plan(
        plan,
        config=cfg,
        shard_routing=routing,
        path="analytical",
        dialect="duckdb",
        duckdb_catalog=catalog,
    )
    assert d.status == "rejected"
    assert d.error
    assert "físico" in d.error.lower() or "lógico" in d.error.lower()
    assert "recebiveis" in d.error.lower()
    # Não deve parecer só "cross-database" genérico sem orientação de nome lógico
    assert "recebiveis_654" in d.error or "físic" in d.error.lower()


def test_ok_logical_names_on_duckdb_after_materialize() -> None:
    from txt2sql.artifacts import DuckDBCatalog, DuckDBTableInfo

    cfg = _config()
    routing = ShardRouting(
        mode="multi",
        logical_table="recebiveis",
        bindings=[
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="654",
                database_id="db_shard_2",
                physical_table="recebiveis_654",
            ),
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="747",
                database_id="db_shard_2",
                physical_table="recebiveis_747",
            ),
        ],
    )
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(name="recebiveis", row_count=10),
            DuckDBTableInfo(name="clientes", row_count=2),
        ]
    )
    plan = SQLPlan(
        sql=(
            "SELECT r.cnpj, c.razao_social AS nome, SUM(r.valor) AS valor "
            "FROM recebiveis r JOIN clientes c ON r.cnpj = c.cnpj "
            "GROUP BY r.cnpj, c.razao_social"
        ),
        dialect="duckdb",
    )
    d = check_sql_plan(
        plan,
        config=cfg,
        shard_routing=routing,
        path="analytical",
        dialect="duckdb",
        duckdb_catalog=catalog,
    )
    assert d.status == "ok", d.error
