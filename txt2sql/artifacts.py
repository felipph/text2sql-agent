"""Artefatos tipados do grafo dual-path (provenance, routing, execução)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from txt2sql.intent import IntentPlan


class LogicalPlan(BaseModel):
    """Projeção de :class:`IntentPlan` para provenance (S4/S8), não output LLM."""

    tables: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    aggregations: list[str] = Field(default_factory=list)
    limit: int | None = None
    assumptions: list[str] = Field(default_factory=list)

    @classmethod
    def from_intent(cls, intent_plan: IntentPlan) -> LogicalPlan:
        tables: set[str] = set()
        filters: list[str] = []
        aggregations: list[str] = []
        joins: list[str] = []
        for f in intent_plan.filters:
            tables.add(f.table_id)
            filters.append(f"{f.table_id}.{f.column_id} {f.op} {f.value!r}")
        for m in intent_plan.metrics:
            tables.add(m.table_id)
            if m.agg and m.agg != "none":
                aggregations.append(f"{m.agg}({m.table_id}.{m.column_id})")
            elif m.column_id:
                aggregations.append(f"{m.table_id}.{m.column_id}")
        for g in intent_plan.group_by:
            tables.add(g.table_id)
        for j in intent_plan.joins:
            tables.add(j.from_table_id)
            tables.add(j.to_table_id)
            joins.append(f"{j.from_table_id}->{j.to_table_id}")
        for e in intent_plan.entities:
            if e.table_id:
                tables.add(e.table_id)
        return cls(
            tables=sorted(tables),
            joins=joins,
            filters=filters,
            aggregations=aggregations,
            limit=intent_plan.limit,
            assumptions=list(intent_plan.assumptions),
        )


class ShardBinding(BaseModel):
    """Binding determinístico de shard para uma tabela lógica."""

    table_id: str
    discriminator_value: str
    database_id: str
    physical_table: str


class ShardRouting(BaseModel):
    """Resultado de resolução de shard (none / single / multi)."""

    mode: Literal["none", "single", "multi"] = "none"
    bindings: list[ShardBinding] = Field(default_factory=list)
    logical_table: str | None = None
    capped: bool = False
    cap_assumption: str | None = None


class SQLPlan(BaseModel):
    """Plano SQL tipado antes da execução.

    Sem ``dict`` livre: OpenAI structured output exige schema estrito
    (``additionalProperties: false``) — ver IntentPlan / FilterValue.
    """

    sql: str
    dialect: Literal["postgres", "duckdb"]
    expected_shape: Literal["scalar", "row", "table"] = "table"


class MaterializationStep(BaseModel):
    """Passo de materialização no DuckDB intermediário."""

    source_query: str
    target_table: str
    mode: Literal["create", "append", "replace"] = "replace"
    estimated_rows: int | None = None
    shard_binding: ShardBinding | None = None
    shard_bindings: list[ShardBinding] = Field(default_factory=list)


class MaterializationPlan(BaseModel):
    """Sequência de materializações para o path analítico."""

    steps: list[MaterializationStep]
    rationale: str = ""


class ExecutionResult(BaseModel):
    """Resultado compactado de execução SQL."""

    status: Literal["ok", "error", "rejected", "timeout"]
    row_count: int = 0
    schema_: list[dict] = Field(default_factory=list, alias="schema")
    sample: list[dict] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    truncated: bool = False
    full_result_ref: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class DuckDBTableInfo(BaseModel):
    """Metadados de uma tabela materializada no DuckDB da sessão."""

    name: str
    schema_: list[dict] = Field(default_factory=list, alias="schema")
    row_count: int = 0
    source_queries: list[str] = Field(default_factory=list)
    covered_filters: list[str] = Field(default_factory=list)
    shard_bindings: list[ShardBinding] = Field(default_factory=list)
    materialized_at: datetime | None = None

    model_config = {"populate_by_name": True}


class DuckDBCatalog(BaseModel):
    """Catálogo de tabelas DuckDB reutilizáveis na sessão."""

    tables: list[DuckDBTableInfo] = Field(default_factory=list)


BudgetCounter = Literal[
    "refine_count",
    "mat_loop_count",
    "gate_visits",
    "total_rows_materialized",
    "clarification_count",
]


class Budget(BaseModel):
    """Contadores e limites transversais do grafo."""

    refine_count: int = 0
    max_refine: int = 3
    mat_loop_count: int = 0
    max_mat_loops: int = 3
    gate_visits: int = 0
    max_gate_visits: int = 2
    clarification_count: int = 0
    max_clarifications: int = 2
    total_rows_materialized: int = 0
    max_rows_materialized: int = 2_000_000
    max_rows_per_extract: int = 500_000
    sample_rows: int = 20

    def exhausted(self, counter: BudgetCounter) -> bool:
        mapping = {
            "refine_count": ("refine_count", "max_refine"),
            "mat_loop_count": ("mat_loop_count", "max_mat_loops"),
            "gate_visits": ("gate_visits", "max_gate_visits"),
            "clarification_count": ("clarification_count", "max_clarifications"),
            "total_rows_materialized": (
                "total_rows_materialized",
                "max_rows_materialized",
            ),
        }
        cur_name, max_name = mapping[counter]
        return getattr(self, cur_name) >= getattr(self, max_name)


class VerifyDecision(BaseModel):
    """Decisão do nó verify (answer / refine_sql / data_gap)."""

    action: Literal["answer", "refine_sql", "data_gap"]
    reason: str = ""


__all__ = [
    "Budget",
    "DuckDBCatalog",
    "DuckDBTableInfo",
    "ExecutionResult",
    "LogicalPlan",
    "MaterializationPlan",
    "MaterializationStep",
    "SQLPlan",
    "ShardBinding",
    "ShardRouting",
    "VerifyDecision",
]
