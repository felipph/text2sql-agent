"""Testes do módulo materialize (materialização deep no DuckDB)."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from txt2sql.artifacts import (
    DuckDBCatalog,
    MaterializationPlan,
    MaterializationStep,
    ShardBinding,
    ShardRouting,
)
from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    DatabaseConfig,
    DuckDBConfig,
    ShardingConfig,
    ShardResult,
    TableConfig,
)
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.db.materialize import MaterializeOutcome, materialize_tables
from txt2sql.db.registry import DatabaseRegistry
from txt2sql.intent import (
    FilterClause,
    IntentPlan,
    JoinClause,
    JoinOn,
    MetricClause,
)


def _sharded_table() -> TableConfig:
    return TableConfig(
        id="recebiveis",
        database="db_a",
        name="recebiveis",
        sharding=ShardingConfig(
            discriminator_column="cnpj",
            resolver="tests.test_materialize:_resolver_fn",
        ),
        duckdb=DuckDBConfig(enabled=True, trigger="aggregation", fetch_limit=100_000),
        columns=[
            ColumnConfig(name="cnpj"),
            ColumnConfig(name="valor"),
        ],
    )


def _clientes_table() -> TableConfig:
    return TableConfig(
        id="clientes",
        database="db_main",
        name="clientes",
        duckdb=DuckDBConfig(enabled=True, trigger="aggregation", fetch_limit=100_000),
        columns=[
            ColumnConfig(name="cnpj"),
            ColumnConfig(name="razao_social"),
        ],
    )


def _resolver_fn(v: str) -> ShardResult:
    if v.startswith("1"):
        return ShardResult(database_id="db_a", table_name="rec_a")
    return ShardResult(database_id="db_b", table_name="rec_b")


def _build_sharded_registry() -> tuple[DatabaseRegistry, AgentConfig]:
    eng_a = create_engine("sqlite:///:memory:")
    eng_b = create_engine("sqlite:///:memory:")
    with eng_a.begin() as c:
        c.execute(text("CREATE TABLE rec_a (cnpj TEXT, valor REAL)"))
        c.execute(text("INSERT INTO rec_a VALUES ('111', 10.0), ('122', 15.0)"))
    with eng_b.begin() as c:
        c.execute(text("CREATE TABLE rec_b (cnpj TEXT, valor REAL)"))
        c.execute(text("INSERT INTO rec_b VALUES ('222', 20.0), ('333', 30.0)"))

    table = _sharded_table()
    config = AgentConfig(
        databases=[
            DatabaseConfig(id="db_a", connection_string="sqlite:///:memory:"),
            DatabaseConfig(id="db_b", connection_string="sqlite:///:memory:"),
        ],
        tables=[table],
        override_connections={},
    )
    registry = DatabaseRegistry.__new__(DatabaseRegistry)
    registry._engines = {"db_a": eng_a, "db_b": eng_b}  # type: ignore[attr-defined]
    registry._inspection_engines = {"db_a": eng_a, "db_b": eng_b}  # type: ignore[attr-defined]
    registry._config = config  # type: ignore[attr-defined]
    return registry, config


def _bindings(values: list[str]) -> list[ShardBinding]:
    return [
        ShardBinding(
            table_id="recebiveis",
            database_id=_resolver_fn(v).database_id,
            physical_table=_resolver_fn(v).table_name,
            discriminator_value=v,
        )
        for v in values
    ]


def _build_main_registry() -> tuple[DatabaseRegistry, AgentConfig]:
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE clientes (cnpj TEXT, razao_social TEXT)"))
        c.execute(
            text(
                "INSERT INTO clientes VALUES ('111', 'Alpha'), ('222', 'Beta')"
            )
        )

    config = AgentConfig(
        databases=[DatabaseConfig(id="db_main", connection_string="sqlite:///:memory:")],
        tables=[_clientes_table()],
        override_connections={},
    )
    registry = DatabaseRegistry.__new__(DatabaseRegistry)
    registry._engines = {"db_main": eng}  # type: ignore[attr-defined]
    registry._inspection_engines = {"db_main": eng}  # type: ignore[attr-defined]
    registry._config = config  # type: ignore[attr-defined]
    return registry, config


def test_materialize_multi_binding_uses_fan_in_and_synthetic_provenance() -> None:
    registry, config = _build_sharded_registry()
    session = DuckDBSession()
    shard = ShardRouting(mode="multi", bindings=_bindings(["111", "222"]))
    mat_plan = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="",
                target_table="recebiveis",
                mode="replace",
            )
        ],
    )
    intent = IntentPlan(
        status="ready",
        filters=[
            FilterClause(table_id="recebiveis", column_id="cnpj", op="in", value=["111", "222"]),
        ],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    try:
        outcome = materialize_tables(
            mat_plan=mat_plan,
            intent=intent,
            shard=shard,
            catalog=DuckDBCatalog(),
            session=session,
            registry=registry,
            config=config,
            max_rows_per_extract=10_000,
            dialect="postgres",
        )
        assert isinstance(outcome, MaterializeOutcome)
        assert outcome.error_kind == "ok"
        assert outcome.error is None
        entry = next(t for t in outcome.catalog.tables if t.name == "recebiveis")
        assert entry.source_queries == ["fan-in:2 bindings"]
        assert entry.row_count == 2
        assert {b.discriminator_value for b in entry.shard_bindings} == {"111", "222"}
        assert session.is_materialized("recebiveis")
    finally:
        session.close()


def test_materialize_catalog_records_step_bindings_not_only_routing() -> None:
    """Catálogo deve refletir bindings do step (fan-in), mesmo se ShardRouting for single."""
    registry, config = _build_sharded_registry()
    session = DuckDBSession()
    step_bindings = _bindings(["111", "222"])
    # Roteamento atual só conhece o CNPJ novo — bug histórico gravava só este.
    shard = ShardRouting(mode="single", bindings=_bindings(["222"]))
    mat_plan = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="",
                target_table="recebiveis",
                mode="replace",
                shard_bindings=step_bindings,
            )
        ],
    )
    intent = IntentPlan(
        status="ready",
        filters=[
            FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="222"),
        ],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    try:
        outcome = materialize_tables(
            mat_plan=mat_plan,
            intent=intent,
            shard=shard,
            catalog=DuckDBCatalog(),
            session=session,
            registry=registry,
            config=config,
            max_rows_per_extract=10_000,
            dialect="postgres",
        )
        assert outcome.error_kind == "ok"
        entry = next(t for t in outcome.catalog.tables if t.name == "recebiveis")
        assert {b.discriminator_value for b in entry.shard_bindings} == {"111", "222"}
        assert entry.source_queries == ["fan-in:2 bindings"]
    finally:
        session.close()


def test_materialize_single_binding_missing_physical_rejects() -> None:
    registry, config = _build_sharded_registry()
    session = DuckDBSession()
    shard = ShardRouting(
        mode="single",
        bindings=[
            ShardBinding(
                table_id="recebiveis",
                database_id="db_a",
                physical_table="rec_missing",
                discriminator_value="111",
            )
        ],
    )
    mat_plan = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="",
                target_table="recebiveis",
                mode="replace",
            )
        ],
    )
    intent = IntentPlan(
        status="ready",
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="111")],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    outcome = materialize_tables(
        mat_plan=mat_plan,
        intent=intent,
        shard=shard,
        catalog=DuckDBCatalog(),
        session=session,
        registry=registry,
        config=config,
        max_rows_per_extract=10_000,
        dialect="postgres",
    )
    session.close()
    assert outcome.error_kind == "rejected"
    assert outcome.error is not None
    assert "inexistente" in outcome.error.lower()


def test_materialize_non_sharded_source_query_uses_source_sql_path() -> None:
    registry, config = _build_main_registry()
    session = DuckDBSession()
    shard = ShardRouting(mode="none")
    mat_plan = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="SELECT cnpj, razao_social FROM clientes WHERE cnpj = '111'",
                target_table="clientes",
                mode="replace",
            )
        ],
    )
    intent = IntentPlan(
        status="ready",
        metrics=[MetricClause(table_id="clientes", column_id="razao_social", agg="none")],
    )
    try:
        outcome = materialize_tables(
            mat_plan=mat_plan,
            intent=intent,
            shard=shard,
            catalog=DuckDBCatalog(),
            session=session,
            registry=registry,
            config=config,
            max_rows_per_extract=10_000,
            dialect="postgres",
        )
        assert outcome.error_kind == "ok"
        entry = next(t for t in outcome.catalog.tables if t.name == "clientes")
        assert entry.source_queries
        assert "clientes" in entry.source_queries[0].lower()
        assert entry.row_count == 1
        rows = session.execute("SELECT razao_social FROM clientes")
        assert rows[0]["razao_social"] == "Alpha"
    finally:
        session.close()


def test_materialize_completes_missing_intent_table() -> None:
    eng_a = create_engine("sqlite:///:memory:")
    eng_main = create_engine("sqlite:///:memory:")
    with eng_a.begin() as c:
        c.execute(text("CREATE TABLE rec_a (cnpj TEXT, valor REAL)"))
        c.execute(text("INSERT INTO rec_a VALUES ('111', 10.0)"))
    with eng_main.begin() as c:
        c.execute(text("CREATE TABLE clientes (cnpj TEXT, razao_social TEXT)"))
        c.execute(text("INSERT INTO clientes VALUES ('111', 'Alpha')"))

    config = AgentConfig(
        databases=[
            DatabaseConfig(id="db_a", connection_string="sqlite:///:memory:"),
            DatabaseConfig(id="db_main", connection_string="sqlite:///:memory:"),
        ],
        tables=[_sharded_table(), _clientes_table()],
        override_connections={},
    )
    registry = DatabaseRegistry.__new__(DatabaseRegistry)
    registry._engines = {"db_a": eng_a, "db_main": eng_main}  # type: ignore[attr-defined]
    registry._inspection_engines = {"db_a": eng_a, "db_main": eng_main}  # type: ignore[attr-defined]
    registry._config = config  # type: ignore[attr-defined]

    session = DuckDBSession()
    shard = ShardRouting(mode="single", bindings=_bindings(["111"])[:1])
    mat_plan = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="",
                target_table="recebiveis",
                mode="replace",
            )
        ],
    )
    intent = IntentPlan(
        status="ready",
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="111")],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        joins=[
            JoinClause(
                from_table_id="recebiveis",
                to_table_id="clientes",
                on=[JoinOn(from_column="cnpj", to_column="cnpj")],
            )
        ],
    )
    try:
        outcome = materialize_tables(
            mat_plan=mat_plan,
            intent=intent,
            shard=shard,
            catalog=DuckDBCatalog(),
            session=session,
            registry=registry,
            config=config,
            max_rows_per_extract=10_000,
            dialect="postgres",
        )
        assert outcome.error_kind == "ok"
        names = {t.name for t in outcome.catalog.tables}
        assert "recebiveis" in names
        assert "clientes" in names
        assert session.is_materialized("recebiveis")
        assert session.is_materialized("clientes")
    finally:
        session.close()
