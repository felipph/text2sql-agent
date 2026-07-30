"""Testes do lookup-then-route de discriminador."""

from __future__ import annotations

from unittest.mock import MagicMock

from txt2sql.artifacts import DuckDBCatalog, DuckDBTableInfo
from txt2sql.config import (
    AgentConfig,
    ColumnRef,
    DatabaseConfig,
    DuckDBConfig,
    RelationshipConfig,
    ShardingConfig,
    TableConfig,
)
from txt2sql.discriminator_lookup import (
    LookupSource,
    find_lookup_source,
    inject_discriminator_filter,
    run_discriminator_lookup,
)
from txt2sql.intent import (
    EntityRef,
    FilterClause,
    IntentPlan,
    JoinClause,
    JoinOn,
    MetricClause,
)


def _cfg(*, relationships: list[RelationshipConfig] | None = None) -> AgentConfig:
    if relationships is None:
        relationships = [
            RelationshipConfig(
                from_ref=ColumnRef(table="recebiveis", column="cnpj"),
                to_ref=ColumnRef(table="clientes", column="cnpj"),
            )
        ]
    return AgentConfig(
        databases=[
            DatabaseConfig(id="db_main", connection_string="sqlite:///:memory:"),
            DatabaseConfig(id="db_shard", connection_string="sqlite:///:memory:"),
        ],
        tables=[
            TableConfig(
                id="clientes",
                database="db_main",
                name="clientes",
                duckdb=DuckDBConfig(enabled=True, trigger="join", fetch_limit=100),
            ),
            TableConfig(
                id="recebiveis",
                database="db_main",
                name="recebiveis",
                sharding=ShardingConfig(
                    discriminator_column="cnpj",
                    resolver="playground.shard_resolver:resolve_cnpj_shard",
                ),
            ),
            TableConfig(
                id="filiais",
                database="db_main",
                name="filiais",
                sharding=ShardingConfig(
                    discriminator_column="cnpj",
                    resolver="playground.shard_resolver:resolve_cnpj_shard",
                ),
            ),
        ],
        relationships=relationships,
        max_shards=20,
    )


def test_find_lookup_source_hit() -> None:
    plan = IntentPlan(
        status="ready",
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        entities=[
            EntityRef(mention="clientes", table_id="clientes", role="table"),
            EntityRef(mention="recebiveis", table_id="recebiveis", role="table"),
        ],
        joins=[
            JoinClause(
                from_table_id="clientes",
                to_table_id="recebiveis",
                on=[JoinOn(from_column="cnpj", to_column="cnpj")],
            )
        ],
    )
    src = find_lookup_source(plan, _cfg())
    assert src is not None
    assert src.lookup_table_id == "clientes"
    assert src.lookup_column == "cnpj"
    assert src.sharded_table_id == "recebiveis"
    assert src.discriminator_column == "cnpj"


def test_find_lookup_source_miss_without_relationship() -> None:
    plan = IntentPlan(
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    assert find_lookup_source(plan, _cfg(relationships=[])) is None


def test_find_lookup_source_prefers_entity_table() -> None:
    cfg = AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite:///:memory:")],
        tables=[
            TableConfig(id="clientes", database="db", name="clientes"),
            TableConfig(id="parceiros", database="db", name="parceiros"),
            TableConfig(
                id="recebiveis",
                database="db",
                name="recebiveis",
                sharding=ShardingConfig(
                    discriminator_column="cnpj",
                    resolver="playground.shard_resolver:resolve_cnpj_shard",
                ),
            ),
        ],
        relationships=[
            RelationshipConfig(
                from_ref=ColumnRef(table="recebiveis", column="cnpj"),
                to_ref=ColumnRef(table="parceiros", column="cnpj"),
            ),
            RelationshipConfig(
                from_ref=ColumnRef(table="recebiveis", column="cnpj"),
                to_ref=ColumnRef(table="clientes", column="cnpj"),
            ),
        ],
    )
    plan = IntentPlan(
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        entities=[EntityRef(mention="clientes", table_id="clientes", role="table")],
    )
    src = find_lookup_source(plan, cfg)
    assert src is not None
    assert src.lookup_table_id == "clientes"


def test_find_lookup_source_rejects_sharded_lookup() -> None:
    cfg = _cfg(
        relationships=[
            RelationshipConfig(
                from_ref=ColumnRef(table="recebiveis", column="cnpj"),
                to_ref=ColumnRef(table="filiais", column="cnpj"),
            )
        ]
    )
    plan = IntentPlan(
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    assert find_lookup_source(plan, cfg) is None


def test_find_lookup_source_none_when_filter_present() -> None:
    plan = IntentPlan(
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="1")],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    assert find_lookup_source(plan, _cfg()) is None


def test_inject_discriminator_filter() -> None:
    plan = IntentPlan(
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    src = LookupSource(
        lookup_table_id="clientes",
        lookup_column="cnpj",
        sharded_table_id="recebiveis",
        discriminator_column="cnpj",
    )
    enriched = inject_discriminator_filter(plan, src, ["a", "b"])
    assert len(enriched.filters) == 1
    assert enriched.filters[0].op == "in"
    assert enriched.filters[0].value == ["a", "b"]


def test_run_discriminator_lookup_from_registry() -> None:
    source = LookupSource(
        lookup_table_id="clientes",
        lookup_column="cnpj",
        sharded_table_id="recebiveis",
        discriminator_column="cnpj",
    )
    registry = MagicMock()
    registry.execute.return_value = [
        {"cnpj": "111"},
        {"cnpj": "222"},
        {"cnpj": "111"},
    ]
    result = run_discriminator_lookup(
        source,
        config=_cfg(),
        registry=registry,
        duckdb_session=None,
        catalog=None,
    )
    assert result.error is None
    assert result.values == ["111", "222"]
    assert not result.from_cache
    assert "DISTINCT" in result.source_sql.upper()
    registry.execute.assert_called_once()


def test_run_discriminator_lookup_from_duckdb_cache() -> None:
    source = LookupSource(
        lookup_table_id="clientes",
        lookup_column="cnpj",
        sharded_table_id="recebiveis",
        discriminator_column="cnpj",
    )
    session = MagicMock()
    session.is_materialized.return_value = True
    session.execute.return_value = [{"cnpj": "aaa"}, {"cnpj": "bbb"}]
    catalog = DuckDBCatalog(
        tables=[DuckDBTableInfo(name="clientes", row_count=2)]
    )
    result = run_discriminator_lookup(
        source,
        config=_cfg(),
        registry=MagicMock(),
        duckdb_session=session,
        catalog=catalog,
    )
    assert result.values == ["aaa", "bbb"]
    assert result.from_cache
    session.execute.assert_called_once()


def test_run_discriminator_lookup_empty() -> None:
    source = LookupSource(
        lookup_table_id="clientes",
        lookup_column="cnpj",
        sharded_table_id="recebiveis",
        discriminator_column="cnpj",
    )
    registry = MagicMock()
    registry.execute.return_value = []
    result = run_discriminator_lookup(
        source,
        config=_cfg(),
        registry=registry,
        duckdb_session=None,
        catalog=None,
    )
    assert result.values == []
    assert result.error


def test_run_discriminator_lookup_error() -> None:
    source = LookupSource(
        lookup_table_id="clientes",
        lookup_column="cnpj",
        sharded_table_id="recebiveis",
        discriminator_column="cnpj",
    )
    registry = MagicMock()
    registry.execute.side_effect = RuntimeError("boom")
    result = run_discriminator_lookup(
        source,
        config=_cfg(),
        registry=registry,
        duckdb_session=None,
        catalog=None,
    )
    assert result.values == []
    assert result.error
    assert "clientes" in result.error
