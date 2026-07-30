from txt2sql.artifacts import ShardBinding, ShardRouting
from txt2sql.config import (
    AgentConfig,
    DatabaseConfig,
    ShardingConfig,
    ShardResult,
    TableConfig,
)
from txt2sql.intent import FilterClause, IntentPlan, MetricClause
from txt2sql.shard_routing import (
    ClarifyNeeded,
    ensure_discriminator_filters,
    missing_discriminator_filter_errors,
    resolve_routing,
)


def _cfg_sharded(*, value_extractor: str | None = None) -> AgentConfig:
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
                    value_extractor=value_extractor,
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
        "65410433218196": ShardResult(database_id="shard1", table_name="recebiveis_654"),
        "74778161849593": ShardResult(database_id="shard2", table_name="recebiveis_747"),
    }
    return mapping[value]


RESOLVERS = {"recebiveis": _resolve_recebiveis}


def _extract_token_values(text: str) -> list[str]:
    """Extractor de teste: pega tokens TOKEN_<id> — domínio fictício, não CNPJ."""
    import re

    return re.findall(r"TOKEN_(\w+)", text)


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


def test_value_extractor_fallback_from_rewrite() -> None:
    """Extractor plugável resolve discriminadores no rewrite sem filters."""
    plan = IntentPlan(
        status="ready",
        question_rewrite="totais para TOKEN_1 e TOKEN_2",
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        filters=[],
    )
    out = resolve_routing(
        plan,
        _cfg_sharded(),
        resolvers=RESOLVERS,
        extractors={"recebiveis": _extract_token_values},
    )
    assert isinstance(out, ShardRouting)
    assert out.mode == "multi"
    assert {b.discriminator_value for b in out.bindings} == {"1", "2"}


def test_value_extractor_fallback_from_extra_text() -> None:
    plan = IntentPlan(
        status="ready",
        question_rewrite="soma",
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    out = resolve_routing(
        plan,
        _cfg_sharded(),
        resolvers=RESOLVERS,
        extractors={"recebiveis": _extract_token_values},
        extra_text="valores TOKEN_1 e TOKEN_2",
    )
    assert isinstance(out, ShardRouting)
    assert len(out.bindings) == 2


def test_without_extractor_still_clarifies() -> None:
    plan = IntentPlan(
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        question_rewrite="CNPJs 65410433218196 e 74778161849593",
    )
    # Sem extractor: texto com CNPJ NÃO resolve — core não conhece CNPJ
    out = resolve_routing(plan, _cfg_sharded(), resolvers=RESOLVERS)
    assert isinstance(out, ClarifyNeeded)


def test_missing_discriminator_filter_errors() -> None:
    plan = IntentPlan(
        status="ready",
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        question_rewrite="soma com 65410433218196",
    )
    errs = missing_discriminator_filter_errors(plan, _cfg_sharded())
    assert errs
    assert "cnpj" in errs[0]


def test_missing_discriminator_filter_errors_ok_when_present() -> None:
    plan = IntentPlan(
        status="ready",
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="1")],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    assert missing_discriminator_filter_errors(plan, _cfg_sharded()) == []


def test_ensure_discriminator_filters_injects_in_clause() -> None:
    plan = IntentPlan(
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        filters=[],
    )
    routing = ShardRouting(
        mode="multi",
        bindings=[
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="65410433218196",
                database_id="shard1",
                physical_table="r1",
            ),
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="74778161849593",
                database_id="shard2",
                physical_table="r2",
            ),
        ],
        logical_table="recebiveis",
    )
    enriched = ensure_discriminator_filters(plan, routing, _cfg_sharded())
    assert len(enriched.filters) == 1
    assert enriched.filters[0].op == "in"
    assert set(enriched.filters[0].value) == {"65410433218196", "74778161849593"}
