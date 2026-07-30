"""Testes da sufficiency gate determinística (sem LLM/banco)."""

from __future__ import annotations

from datetime import UTC, datetime

from txt2sql.artifacts import DuckDBCatalog, DuckDBTableInfo, ShardBinding, ShardRouting
from txt2sql.config import AgentConfig, DatabaseConfig, ShardingConfig, TableConfig
from txt2sql.intent import FilterClause, IntentPlan, MetricClause
from txt2sql.sufficiency import (
    SufficiencyDecision,
    TableGap,
    build_deterministic_mat_plan,
    evaluate_sufficiency,
)


def _cfg(*tables: TableConfig, reuse_ttl_seconds: int = 0) -> AgentConfig:
    return AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite://")],
        tables=list(tables),
        dialect="postgres",
        reuse_ttl_seconds=reuse_ttl_seconds,
    )


def _table(tid: str, *, sharded: bool = False) -> TableConfig:
    return TableConfig(
        id=tid,
        database="db",
        name=tid,
        sharding=(
            ShardingConfig(discriminator_column="filial", resolver="x:y") if sharded else None
        ),
    )


def _binding(tid: str, disc: str, db: str = "db") -> ShardBinding:
    return ShardBinding(
        table_id=tid,
        discriminator_value=disc,
        database_id=db,
        physical_table=f"{tid}_{disc}",
    )


def test_empty_catalog_refresh() -> None:
    intent = IntentPlan(
        filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="SP")]
    )
    d = evaluate_sufficiency(
        intent,
        ShardRouting(),
        DuckDBCatalog(),
        _cfg(_table("vendas")),
        dialect="postgres",
    )
    assert d.action == "refresh"
    assert any(g.reason == "missing_table" for g in d.gaps)


def test_missing_table_gap() -> None:
    intent = IntentPlan(metrics=[MetricClause(table_id="vendas", column_id="valor")])
    catalog = DuckDBCatalog(tables=[DuckDBTableInfo(name="clientes", row_count=1)])
    d = evaluate_sufficiency(
        intent,
        ShardRouting(),
        catalog,
        _cfg(_table("vendas"), _table("clientes")),
        dialect="postgres",
    )
    assert d.action == "refresh"
    assert d.gaps[0].reason == "missing_table"
    assert d.gaps[0].table_id == "vendas"


def test_empty_intent_tables_reuse_if_catalog_nonempty() -> None:
    catalog = DuckDBCatalog(tables=[DuckDBTableInfo(name="vendas", row_count=1)])
    d = evaluate_sufficiency(
        IntentPlan(),
        ShardRouting(),
        catalog,
        _cfg(_table("vendas")),
        dialect="postgres",
    )
    assert d.action == "reuse"


def test_shard_same_bindings_reuse() -> None:
    b = _binding("vendas", "654")
    intent = IntentPlan(
        filters=[FilterClause(table_id="vendas", column_id="filial", op="eq", value="654")]
    )
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                row_count=10,
                source_queries=["fan-in:1 bindings"],
                shard_bindings=[b],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    routing = ShardRouting(mode="single", bindings=[b], logical_table="vendas")
    d = evaluate_sufficiency(
        intent, routing, catalog, _cfg(_table("vendas", sharded=True)), dialect="postgres"
    )
    assert d.action == "reuse"


def test_shard_missing_binding_gap() -> None:
    b654 = _binding("vendas", "654")
    b747 = _binding("vendas", "747")
    intent = IntentPlan(
        filters=[FilterClause(table_id="vendas", column_id="filial", op="in", value=["654", "747"])]
    )
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                row_count=10,
                source_queries=["fan-in:1 bindings"],
                shard_bindings=[b654],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    routing = ShardRouting(mode="multi", bindings=[b654, b747], logical_table="vendas")
    d = evaluate_sufficiency(
        intent, routing, catalog, _cfg(_table("vendas", sharded=True)), dialect="postgres"
    )
    assert d.action == "refresh"
    assert d.gaps[0].reason == "missing_shard"
    assert [b.discriminator_value for b in d.gaps[0].missing_bindings] == ["747"]


def test_columns_select_star_covers() -> None:
    intent = IntentPlan(metrics=[MetricClause(table_id="vendas", column_id="valor", agg="sum")])
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas"],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    d = evaluate_sufficiency(
        intent, ShardRouting(), catalog, _cfg(_table("vendas")), dialect="postgres"
    )
    assert d.action == "reuse"


def test_columns_projection_missing() -> None:
    intent = IntentPlan(metrics=[MetricClause(table_id="vendas", column_id="c", agg="sum")])
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT a, b FROM vendas"],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    d = evaluate_sufficiency(
        intent, ShardRouting(), catalog, _cfg(_table("vendas")), dialect="postgres"
    )
    assert d.action == "refresh"
    assert d.gaps[0].reason == "missing_columns"
    assert "c" in d.gaps[0].missing_columns


def test_unparseable_sql_unknown() -> None:
    intent = IntentPlan(metrics=[MetricClause(table_id="vendas", column_id="a")])
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["NOT VALID SQL [[["],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    d = evaluate_sufficiency(
        intent, ShardRouting(), catalog, _cfg(_table("vendas")), dialect="postgres"
    )
    assert d.action == "unknown"


def test_predicate_extract_narrower_than_intent_mismatch() -> None:
    """Cache com WHERE status=pendente não serve intent sem filtro de status (pago/vencido)."""
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=[
                    "SELECT cnpj, valor, status FROM vendas WHERE status = 'pendente'"
                ],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    d = evaluate_sufficiency(
        IntentPlan(
            metrics=[MetricClause(table_id="vendas", column_id="valor", agg="sum")]
        ),
        ShardRouting(),
        catalog,
        _cfg(_table("vendas")),
        dialect="postgres",
    )
    assert d.action == "refresh"
    assert d.gaps[0].reason == "predicate_mismatch"


def test_predicate_eq_reuse_and_mismatch() -> None:
    cfg = _cfg(_table("vendas"))
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas WHERE uf = 'SP'"],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    ok = evaluate_sufficiency(
        IntentPlan(
            filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="SP")]
        ),
        ShardRouting(),
        catalog,
        cfg,
        dialect="postgres",
    )
    assert ok.action == "reuse"
    bad = evaluate_sufficiency(
        IntentPlan(
            filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="RJ")]
        ),
        ShardRouting(),
        catalog,
        cfg,
        dialect="postgres",
    )
    assert bad.action == "refresh"
    assert bad.gaps[0].reason == "predicate_mismatch"


def test_predicate_range_contained() -> None:
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas WHERE valor > 100"],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    d = evaluate_sufficiency(
        IntentPlan(
            filters=[FilterClause(table_id="vendas", column_id="valor", op="gt", value=500)]
        ),
        ShardRouting(),
        catalog,
        _cfg(_table("vendas")),
        dialect="postgres",
    )
    assert d.action == "reuse"


def test_predicate_or_like_unknown() -> None:
    cfg = _cfg(_table("vendas"))
    for sql in (
        "SELECT * FROM vendas WHERE uf = 'SP' OR uf = 'RJ'",
        "SELECT * FROM vendas WHERE nome LIKE '%a%'",
    ):
        catalog = DuckDBCatalog(
            tables=[
                DuckDBTableInfo(
                    name="vendas",
                    source_queries=[sql],
                    materialized_at=datetime.now(UTC),
                )
            ]
        )
        d = evaluate_sufficiency(
            IntentPlan(
                filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="SP")]
            ),
            ShardRouting(),
            catalog,
            cfg,
            dialect="postgres",
        )
        assert d.action == "unknown", sql


def test_ttl_stale_and_missing_timestamp() -> None:
    cfg = _cfg(_table("vendas"), reuse_ttl_seconds=1800)
    old = datetime(2020, 1, 1, tzinfo=UTC)
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas"],
                materialized_at=old,
            )
        ]
    )
    intent = IntentPlan(metrics=[MetricClause(table_id="vendas", column_id="a")])
    d = evaluate_sufficiency(
        intent,
        ShardRouting(),
        catalog,
        cfg,
        dialect="postgres",
        now=datetime(2020, 1, 1, 1, 0, tzinfo=UTC),
    )
    assert d.action == "refresh"
    assert d.gaps[0].reason == "stale"

    catalog2 = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas"],
                materialized_at=None,
            )
        ]
    )
    d2 = evaluate_sufficiency(
        intent,
        ShardRouting(),
        catalog2,
        cfg,
        dialect="postgres",
        now=datetime.now(UTC),
    )
    assert d2.gaps[0].reason == "stale"


def test_ttl_disabled_ignores_age() -> None:
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas"],
                materialized_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        ]
    )
    d = evaluate_sufficiency(
        IntentPlan(metrics=[MetricClause(table_id="vendas", column_id="a")]),
        ShardRouting(),
        catalog,
        _cfg(_table("vendas"), reuse_ttl_seconds=0),
        dialect="postgres",
        now=datetime.now(UTC),
    )
    assert d.action == "reuse"


def test_deterministic_plan_unions_cached_bindings() -> None:
    b654 = _binding("vendas", "654")
    b747 = _binding("vendas", "747")
    decision = SufficiencyDecision(
        action="refresh",
        gaps=[
            TableGap(
                table_id="vendas",
                reason="missing_shard",
                missing_bindings=[b747],
            )
        ],
    )
    catalog = DuckDBCatalog(tables=[DuckDBTableInfo(name="vendas", shard_bindings=[b654])])
    plan = build_deterministic_mat_plan(decision, catalog, _cfg(_table("vendas", sharded=True)))
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].source_query == ""
    discs = {b.discriminator_value for b in plan.steps[0].shard_bindings}
    assert discs == {"654", "747"}


def test_deterministic_plan_rejects_missing_columns() -> None:
    decision = SufficiencyDecision(
        action="refresh",
        gaps=[TableGap(table_id="vendas", reason="missing_columns", missing_columns=["c"])],
    )
    assert (
        build_deterministic_mat_plan(
            decision, DuckDBCatalog(), _cfg(_table("vendas", sharded=True))
        )
        is None
    )
