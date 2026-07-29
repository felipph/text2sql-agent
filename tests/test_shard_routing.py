from txt2sql.artifacts import ShardRouting
from txt2sql.config import (
    AgentConfig,
    DatabaseConfig,
    ShardingConfig,
    ShardResult,
    TableConfig,
)
from txt2sql.intent import FilterClause, IntentPlan, MetricClause
from txt2sql.shard_routing import ClarifyNeeded, resolve_routing


def _cfg_sharded() -> AgentConfig:
    return AgentConfig(
        databases=[
            DatabaseConfig(id="db", connection_string="sqlite:///:memory:"),
            DatabaseConfig(id="shard1", connection_string="sqlite:///:memory:"),
            DatabaseConfig(id="shard2", connection_string="sqlite:///:memory:"),
        ],
        tables=[
            TableConfig(
                id="recebiveis",
                database="db",
                name="recebiveis",
                sharding=ShardingConfig(
                    discriminator_column="cnpj",
                    resolver="playground.shard_resolver:resolve_cnpj_shard",
                ),
            ),
            TableConfig(id="clientes", database="db", name="clientes"),
        ],
        max_shard_discriminators=20,
    )


def _resolve_recebiveis(value: str) -> ShardResult:
    mapping = {
        "1": ShardResult(database_id="shard1", table_name="recebiveis_001"),
        "2": ShardResult(database_id="shard2", table_name="recebiveis_002"),
    }
    return mapping[value]


RESOLVERS = {"recebiveis": _resolve_recebiveis}


def test_non_sharded_none() -> None:
    cfg = AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite:///:memory:")],
        tables=[TableConfig(id="clientes", database="db", name="clientes")],
    )
    out = resolve_routing(IntentPlan(), cfg)
    assert isinstance(out, ShardRouting)
    assert out.mode == "none"


def test_sharded_missing_discriminator_clarify() -> None:
    plan = IntentPlan(
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    out = resolve_routing(plan, _cfg_sharded(), resolvers=RESOLVERS)
    assert isinstance(out, ClarifyNeeded)
    assert out.table_id == "recebiveis"
    assert out.discriminator_column == "cnpj"


def test_sharded_one_value_single() -> None:
    plan = IntentPlan(
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="1")],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    out = resolve_routing(plan, _cfg_sharded(), resolvers=RESOLVERS)
    assert isinstance(out, ShardRouting)
    assert out.mode == "single"
    assert len(out.bindings) == 1
    assert out.bindings[0].database_id == "shard1"
    assert out.bindings[0].physical_table == "recebiveis_001"


def test_sharded_two_values_multi() -> None:
    plan = IntentPlan(
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="in", value=["1", "2"])],
    )
    out = resolve_routing(plan, _cfg_sharded(), resolvers=RESOLVERS)
    assert isinstance(out, ShardRouting)
    assert out.mode == "multi"
    assert len(out.bindings) == 2
    assert out.logical_table == "recebiveis"
