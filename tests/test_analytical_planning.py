"""Testes de txt2sql/analytical_planning.py (sem LLM real)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from txt2sql.analytical_planning import (
    build_materialization_plan,
    check_materialization_ready,
    run_sufficiency_gate,
)
from txt2sql.artifacts import Budget, DuckDBCatalog, DuckDBTableInfo, ShardBinding, ShardRouting
from txt2sql.config import AgentConfig, DatabaseConfig, ShardingConfig, TableConfig
from txt2sql.intent import FilterClause, IntentPlan, MetricClause
from txt2sql.sufficiency import SufficiencyDecision


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


def _catalog_reuse_vendas() -> DuckDBCatalog:
    b = _binding("vendas", "654")
    return DuckDBCatalog(
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


def test_gate_reuse_when_decision_reuse() -> None:
    b = _binding("vendas", "654")
    intent = IntentPlan(
        filters=[FilterClause(table_id="vendas", column_id="filial", op="eq", value="654")]
    )
    routing = ShardRouting(mode="single", bindings=[b], logical_table="vendas")
    catalog = _catalog_reuse_vendas()
    budget = Budget()

    gate_action, decision, new_budget = run_sufficiency_gate(
        intent=intent,
        shard=routing,
        catalog=catalog,
        config=_cfg(_table("vendas", sharded=True)),
        budget=budget,
        dialect="postgres",
    )

    assert gate_action == "reuse"
    assert decision.action == "reuse"
    assert new_budget.gate_visits == 1


def test_gate_refresh_when_decision_refresh() -> None:
    intent = IntentPlan(
        filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="SP")]
    )
    budget = Budget()

    gate_action, decision, new_budget = run_sufficiency_gate(
        intent=intent,
        shard=ShardRouting(),
        catalog=DuckDBCatalog(),
        config=_cfg(_table("vendas")),
        budget=budget,
        dialect="postgres",
    )

    assert gate_action == "refresh"
    assert decision.action == "refresh"
    assert new_budget.gate_visits == 1


def test_gate_unknown_without_llm_fallback_refreshes() -> None:
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
    budget = Budget()

    gate_action, decision, _ = run_sufficiency_gate(
        intent=intent,
        shard=ShardRouting(),
        catalog=catalog,
        config=_cfg(_table("vendas")),
        budget=budget,
        dialect="postgres",
        llm_fallback=None,
    )

    assert gate_action == "refresh"
    assert decision.action == "unknown"


def test_gate_budget_exhausted_refreshes_without_llm() -> None:
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
    budget = Budget(gate_visits=2, max_gate_visits=2)
    calls: list[SufficiencyDecision] = []

    def fallback(decision: SufficiencyDecision) -> str:
        calls.append(decision)
        return "reuse"

    gate_action, decision, new_budget = run_sufficiency_gate(
        intent=intent,
        shard=ShardRouting(),
        catalog=catalog,
        config=_cfg(_table("vendas")),
        budget=budget,
        dialect="postgres",
        llm_fallback=fallback,
    )

    assert gate_action == "refresh"
    assert decision.action == "refresh"
    assert "max_gate_visits" in " ".join(decision.reasons)
    assert new_budget.gate_visits == 2
    assert calls == []


def test_gate_unknown_calls_llm_fallback_once() -> None:
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
    calls: list[SufficiencyDecision] = []

    def fallback(decision: SufficiencyDecision) -> str:
        calls.append(decision)
        return "reuse"

    gate_action, decision, new_budget = run_sufficiency_gate(
        intent=intent,
        shard=ShardRouting(),
        catalog=catalog,
        config=_cfg(_table("vendas")),
        budget=Budget(),
        dialect="postgres",
        llm_fallback=fallback,
    )

    assert len(calls) == 1
    assert calls[0].action == "unknown"
    assert gate_action == "reuse"
    assert decision.action == "reuse"
    assert new_budget.gate_visits == 1


def test_check_mat_loop_exhausted_ready_partial() -> None:
    intent = IntentPlan(
        filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="SP")]
    )
    budget = Budget(mat_loop_count=3, max_mat_loops=3)

    mat_ready, partial, decision = check_materialization_ready(
        intent=intent,
        shard=ShardRouting(),
        catalog=DuckDBCatalog(),
        config=_cfg(_table("vendas")),
        budget=budget,
        last_status="ok",
        dialect="postgres",
    )

    assert mat_ready is True
    assert partial is True
    assert decision is None


def test_check_last_status_rejected_not_ready() -> None:
    intent = IntentPlan(
        filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="SP")]
    )

    mat_ready, partial, decision = check_materialization_ready(
        intent=intent,
        shard=ShardRouting(),
        catalog=DuckDBCatalog(),
        config=_cfg(_table("vendas")),
        budget=Budget(),
        last_status="rejected",
        dialect="postgres",
    )

    assert mat_ready is False
    assert partial is False
    assert decision is None


def test_build_materialization_plan_deterministic_shard_gap() -> None:
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
    from txt2sql.sufficiency import evaluate_sufficiency

    decision = evaluate_sufficiency(
        intent, routing, catalog, _cfg(_table("vendas", sharded=True)), dialect="postgres"
    )
    assert decision.action == "refresh"

    plan = build_materialization_plan(
        intent=intent,
        shard=routing,
        catalog=catalog,
        config=_cfg(_table("vendas", sharded=True)),
        decision=decision,
    )

    assert plan.rationale.startswith("deterministic")
    assert len(plan.steps) == 1
    assert plan.steps[0].target_table == "vendas"


def test_build_materialization_plan_requires_llm_without_deterministic() -> None:
    intent = IntentPlan(
        filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="SP")]
    )
    from txt2sql.sufficiency import evaluate_sufficiency

    decision = evaluate_sufficiency(
        intent, ShardRouting(), DuckDBCatalog(), _cfg(_table("vendas")), dialect="postgres"
    )

    with pytest.raises(ValueError, match="llm_fallback"):
        build_materialization_plan(
            intent=intent,
            shard=ShardRouting(),
            catalog=DuckDBCatalog(),
            config=_cfg(_table("vendas")),
            decision=decision,
        )
