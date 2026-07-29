"""Plano semântico de intenção (IntentPlan) e validação contra o schema.

O nó ``interpret_intent`` produz um :class:`IntentPlan` via structured output;
:func:`validate_intent` confere IDs de tabela/coluna de forma fail-closed antes
de seguir para geração de SQL.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FilterOp = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "like", "between", "is_null"
]
AggOp = Literal["count", "sum", "avg", "min", "max", "none"]
EntityRole = Literal["table", "column", "value"]
IntentStatus = Literal["ready", "needs_clarification"]
SortDirection = Literal["asc", "desc"]
# OpenAI structured output exige `type` no JSON Schema — não usar Any.
FilterValue = str | int | float | bool | list[str] | list[int] | list[float] | None


class EntityRef(BaseModel):
    """Grounding de uma menção do usuário a tabela/coluna/valor."""

    mention: str
    table_id: str | None = None
    column_id: str | None = None
    role: EntityRole = "table"


class FilterClause(BaseModel):
    """Filtro semântico sobre uma coluna."""

    table_id: str
    column_id: str
    op: FilterOp = "eq"
    value: FilterValue = None


class MetricClause(BaseModel):
    """Métrica / projeção agregada ou simples."""

    table_id: str
    column_id: str | None = None
    agg: AggOp = "none"


class GroupByClause(BaseModel):
    """Coluna de agrupamento."""

    table_id: str
    column_id: str


class JoinOn(BaseModel):
    """Par de colunas de um JOIN."""

    from_column: str
    to_column: str


class JoinClause(BaseModel):
    """JOIN semântico entre duas tabelas lógicas."""

    from_table_id: str
    to_table_id: str
    on: list[JoinOn] = Field(default_factory=list)


class OrderByClause(BaseModel):
    """Ordenação."""

    table_id: str
    column_id: str
    direction: SortDirection = "asc"


class Clarification(BaseModel):
    """Pergunta de esclarecimento ao usuário (HITL)."""

    question: str
    options: list[str] = Field(default_factory=list)


class IntentPlan(BaseModel):
    """Plano semântico leve casado com o schema (sem SQL)."""

    status: IntentStatus = "ready"
    question_rewrite: str = ""
    entities: list[EntityRef] = Field(default_factory=list)
    filters: list[FilterClause] = Field(default_factory=list)
    metrics: list[MetricClause] = Field(default_factory=list)
    group_by: list[GroupByClause] = Field(default_factory=list)
    joins: list[JoinClause] = Field(default_factory=list)
    order_by: list[OrderByClause] = Field(default_factory=list)
    limit: int | None = None
    clarification: Clarification | None = None
    assumptions: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    """Resultado de :func:`validate_intent`."""

    ok: bool
    errors: list[str] = Field(default_factory=list)
    needs_clarification: bool = False


def _check_table(table_id: str, index: dict[str, set[str]], errors: list[str]) -> bool:
    if table_id not in index:
        errors.append(f"table_id desconhecido: {table_id!r}")
        return False
    return True


def _check_column(
    table_id: str, column_id: str | None, index: dict[str, set[str]], errors: list[str]
) -> None:
    if column_id is None:
        return
    if not _check_table(table_id, index, errors):
        return
    cols = index[table_id]
    if column_id not in cols:
        errors.append(f"column_id desconhecido: {table_id}.{column_id}")


def validate_intent(
    plan: IntentPlan, schema_index: dict[str, set[str]]
) -> ValidationResult:
    """Valida o plan contra o índice de schema (fail-closed).

    Se ``status == needs_clarification``, não exige IDs válidos — apenas marca
    que o fluxo deve ir para HITL.
    """
    if plan.status == "needs_clarification":
        return ValidationResult(ok=True, errors=[], needs_clarification=True)

    errors: list[str] = []

    for ent in plan.entities:
        if ent.table_id:
            _check_column(ent.table_id, ent.column_id, schema_index, errors)

    for f in plan.filters:
        _check_column(f.table_id, f.column_id, schema_index, errors)

    for m in plan.metrics:
        if m.column_id is None:
            _check_table(m.table_id, schema_index, errors)
        else:
            _check_column(m.table_id, m.column_id, schema_index, errors)

    for g in plan.group_by:
        _check_column(g.table_id, g.column_id, schema_index, errors)

    for o in plan.order_by:
        _check_column(o.table_id, o.column_id, schema_index, errors)

    for j in plan.joins:
        _check_table(j.from_table_id, schema_index, errors)
        _check_table(j.to_table_id, schema_index, errors)
        for on in j.on:
            _check_column(j.from_table_id, on.from_column, schema_index, errors)
            _check_column(j.to_table_id, on.to_column, schema_index, errors)

    # dedupe preservando ordem
    seen: set[str] = set()
    unique: list[str] = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            unique.append(e)

    return ValidationResult(ok=not unique, errors=unique, needs_clarification=False)


__all__ = [
    "AggOp",
    "Clarification",
    "EntityRef",
    "EntityRole",
    "FilterClause",
    "FilterOp",
    "FilterValue",
    "GroupByClause",
    "IntentPlan",
    "IntentStatus",
    "JoinClause",
    "JoinOn",
    "MetricClause",
    "OrderByClause",
    "SortDirection",
    "ValidationResult",
    "validate_intent",
]
