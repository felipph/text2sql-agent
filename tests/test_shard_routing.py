import pytest

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
    cap_bindings_by_shards,
    ensure_discriminator_filters,
    missing_discriminator_filter_errors,
    resolve_routing,
)


def _cfg_sharded(*, value_extractor: str | None = None, max_shards: int = 20) -> AgentConfig:
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
        max_shards=max_shards,
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


def test_ensure_discriminator_filters_syncs_to_capped_bindings() -> None:
    """Após max_shards, filters devem refletir só os discriminadores retidos."""
    plan = IntentPlan(
        filters=[
            FilterClause(
                table_id="recebiveis",
                column_id="cnpj",
                op="in",
                value=["1", "2", "3", "4", "5"],
            )
        ],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    routing = ShardRouting(
        mode="multi",
        bindings=[
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="1",
                database_id="shard1",
                physical_table="t1",
            ),
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="2",
                database_id="shard2",
                physical_table="t2",
            ),
        ],
        logical_table="recebiveis",
        capped=True,
        cap_assumption="Cobertura parcial: 2 de 5 shards físicos (max_shards=2)",
    )
    synced = ensure_discriminator_filters(plan, routing, _cfg_sharded())
    assert len(synced.filters) == 1
    assert synced.filters[0].op == "in"
    assert set(synced.filters[0].value) == {"1", "2"}


def test_ensure_discriminator_filters_preserves_non_disc_filters() -> None:
    plan = IntentPlan(
        filters=[
            FilterClause(
                table_id="recebiveis",
                column_id="status",
                op="in",
                value=["pago", "vencido"],
            ),
            FilterClause(
                table_id="recebiveis",
                column_id="cnpj",
                op="in",
                value=["1", "2", "3"],
            ),
        ],
    )
    routing = ShardRouting(
        mode="multi",
        bindings=[
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="1",
                database_id="shard1",
                physical_table="t1",
            ),
        ],
        logical_table="recebiveis",
    )
    synced = ensure_discriminator_filters(plan, routing, _cfg_sharded())
    assert len(synced.filters) == 2
    status_f = next(f for f in synced.filters if f.column_id == "status")
    cnpj_f = next(f for f in synced.filters if f.column_id == "cnpj")
    assert status_f.value == ["pago", "vencido"]
    assert cnpj_f.op == "eq"
    assert cnpj_f.value == "1"


def test_resolve_routing_rejects_unknown_database_id() -> None:
    class FakeReg:
        def has_database(self, db_id: str) -> bool:
            return db_id == "db_ok"

    def bad_resolver(value: str) -> ShardResult:
        return ShardResult(database_id="db_missing", table_name="recebiveis_x")

    plan = IntentPlan(
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="1")],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    with pytest.raises(ValueError, match="database_id inexistente"):
        resolve_routing(
            plan,
            _cfg_sharded(),
            resolvers={"recebiveis": bad_resolver},
            registry=FakeReg(),
        )


def test_resolve_routing_skips_db_check_without_registry() -> None:
    def odd_resolver(value: str) -> ShardResult:
        return ShardResult(database_id="not_in_config", table_name="t")

    plan = IntentPlan(
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="1")],
    )
    out = resolve_routing(
        plan,
        _cfg_sharded(),
        resolvers={"recebiveis": odd_resolver},
    )
    assert isinstance(out, ShardRouting)
    assert out.bindings[0].database_id == "not_in_config"


def test_cap_bindings_by_shards_many_discs_few_shards() -> None:
    """Muitos discriminadores no mesmo físico contam como 1 shard."""

    def resolver(value: str) -> ShardResult:
        # pares → shard1, ímpares → shard2
        n = int(value)
        if n % 2 == 0:
            return ShardResult(database_id="shard1", table_name="t_even")
        return ShardResult(database_id="shard2", table_name="t_odd")

    bindings = [
        ShardBinding(
            table_id="recebiveis",
            discriminator_value=str(i),
            database_id=resolver(str(i)).database_id,
            physical_table=resolver(str(i)).table_name,
        )
        for i in range(50)
    ]
    capped = cap_bindings_by_shards(bindings, max_shards=20)
    assert not capped.truncated
    assert len(capped.bindings) == 50
    assert capped.total_shards == 2
    assert capped.kept_shards == 2


def test_cap_bindings_by_shards_truncates_physical_shards() -> None:
    bindings = [
        ShardBinding(
            table_id="recebiveis",
            discriminator_value=str(i),
            database_id=f"db_{i}",
            physical_table=f"t_{i}",
        )
        for i in range(25)
    ]
    capped = cap_bindings_by_shards(bindings, max_shards=20)
    assert capped.truncated
    assert capped.total_shards == 25
    assert capped.kept_shards == 20
    assert len(capped.bindings) == 20
    assert {b.discriminator_value for b in capped.bindings} == {str(i) for i in range(20)}
    assert "max_shards=20" in (capped.assumption or "")


def test_resolve_routing_caps_by_physical_shards_not_disc_count() -> None:
    """50 CNPJs → 2 shards físicos com max_shards=2 → sem truncar bindings."""

    def resolver(value: str) -> ShardResult:
        n = int(value)
        db = "shard1" if n % 2 == 0 else "shard2"
        table = "t_even" if n % 2 == 0 else "t_odd"
        return ShardResult(database_id=db, table_name=table)

    values = [str(i) for i in range(50)]
    plan = IntentPlan(
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="in", value=values)],
    )
    out = resolve_routing(
        plan,
        _cfg_sharded(max_shards=2),
        resolvers={"recebiveis": resolver},
    )
    assert isinstance(out, ShardRouting)
    assert out.mode == "multi"
    assert len(out.bindings) == 50
    assert not out.capped


def test_resolve_routing_caps_when_too_many_shards() -> None:
    def resolver(value: str) -> ShardResult:
        return ShardResult(database_id=f"db_{value}", table_name=f"t_{value}")

    values = [str(i) for i in range(5)]
    plan = IntentPlan(
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="in", value=values)],
    )
    # databases only has shard1/shard2/db — skip registry check
    out = resolve_routing(
        plan,
        _cfg_sharded(max_shards=2),
        resolvers={"recebiveis": resolver},
    )
    assert isinstance(out, ShardRouting)
    assert out.capped
    assert len(out.bindings) == 2
    assert out.cap_assumption
    assert "2 de 5" in out.cap_assumption
