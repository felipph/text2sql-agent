from txt2sql.artifacts import ShardRouting
from txt2sql.config import AgentConfig, DatabaseConfig, DuckDBConfig, TableConfig
from txt2sql.intent import IntentPlan, MetricClause
from txt2sql.path_routing import route_execution


def _cfg(*tables: TableConfig) -> AgentConfig:
    return AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite:///:memory:")],
        tables=list(tables),
    )


def test_force_analytical_forces_path() -> None:
    t = TableConfig(
        id="recebiveis",
        database="db",
        name="recebiveis",
        duckdb=DuckDBConfig(enabled=True, force_analytical=True),
    )
    plan = IntentPlan(
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="none")]
    )
    assert route_execution(plan, ShardRouting(mode="none"), _cfg(t)) == "analytical"


def test_multi_shard_forces_analytical() -> None:
    t = TableConfig(id="recebiveis", database="db", name="recebiveis")
    plan = IntentPlan()
    assert route_execution(plan, ShardRouting(mode="multi"), _cfg(t)) == "analytical"


def test_agg_on_duckdb_enabled_analytical() -> None:
    t = TableConfig(
        id="recebiveis",
        database="db",
        name="recebiveis",
        duckdb=DuckDBConfig(enabled=True, trigger="aggregation"),
    )
    plan = IntentPlan(
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")]
    )
    assert route_execution(plan, ShardRouting(mode="none"), _cfg(t)) == "analytical"


def test_plain_lookup_simple() -> None:
    t = TableConfig(id="clientes", database="db", name="clientes")
    plan = IntentPlan(
        metrics=[MetricClause(table_id="clientes", column_id="cnpj", agg="none")]
    )
    assert route_execution(plan, ShardRouting(mode="none"), _cfg(t)) == "simple"
