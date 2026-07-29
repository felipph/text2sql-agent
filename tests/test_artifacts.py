"""Artefatos tipados do grafo dual-path."""

from txt2sql.artifacts import Budget, ExecutionResult, LogicalPlan
from txt2sql.intent import (
    EntityRef,
    FilterClause,
    IntentPlan,
    JoinClause,
    MetricClause,
)


def test_budget_exhausted_refine() -> None:
    b = Budget(refine_count=3, max_refine=3)
    assert b.exhausted("refine_count") is True
    assert b.exhausted("mat_loop_count") is False


def test_budget_exhausted_gate_visits() -> None:
    b = Budget(gate_visits=2, max_gate_visits=2)
    assert b.exhausted("gate_visits") is True
    b_below = Budget(gate_visits=1, max_gate_visits=2)
    assert b_below.exhausted("gate_visits") is False


def test_execution_result_schema_alias_roundtrip() -> None:
    result = ExecutionResult(
        status="ok",
        schema_=[{"name": "id"}],
    )
    dumped = result.model_dump(by_alias=True)
    assert "schema" in dumped
    assert dumped["schema"] == [{"name": "id"}]
    assert "schema_" not in dumped

    restored = ExecutionResult.model_validate(dumped)
    assert restored.schema_ == [{"name": "id"}]


def test_logical_plan_from_intent() -> None:
    plan = IntentPlan(
        status="ready",
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="1")],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        assumptions=["ano corrente"],
        limit=100,
    )
    lp = LogicalPlan.from_intent(plan)
    assert "recebiveis" in lp.tables
    assert any("sum" in a.lower() or "valor" in a for a in lp.aggregations)
    assert lp.assumptions == ["ano corrente"]
    assert lp.limit == 100


def test_logical_plan_from_intent_joins() -> None:
    plan = IntentPlan(
        status="ready",
        joins=[
            JoinClause(from_table_id="recebiveis", to_table_id="clientes"),
        ],
        entities=[
            EntityRef(mention="clientes", table_id="clientes", role="table"),
        ],
    )
    lp = LogicalPlan.from_intent(plan)
    assert "recebiveis->clientes" in lp.joins
    assert "recebiveis" in lp.tables
    assert "clientes" in lp.tables
