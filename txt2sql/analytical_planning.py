"""Orquestração analítica: sufficiency gate, plano de materialize e check pós-materialize."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel

from txt2sql.artifacts import Budget, DuckDBCatalog, MaterializationPlan, ShardRouting
from txt2sql.config import AgentConfig
from txt2sql.intent import IntentPlan
from txt2sql.sufficiency import (
    SufficiencyDecision,
    build_deterministic_mat_plan,
    evaluate_sufficiency,
)


class GateDecision(BaseModel):
    """Schema LLM do sufficiency_gate: reutilizar catálogo ou refresh."""

    action: Literal["reuse", "refresh"] = "refresh"


class MaterializationCheck(BaseModel):
    """Schema LLM pós-materialize: catálogo pronto para SQL analítico."""

    ready: bool = True
    reason: str = ""


def run_sufficiency_gate(
    *,
    intent: IntentPlan,
    shard: ShardRouting,
    catalog: DuckDBCatalog,
    config: AgentConfig,
    budget: Budget,
    dialect: str | None,
    llm_fallback: Callable[[SufficiencyDecision], Literal["reuse", "refresh"]] | None = None,
) -> tuple[str, SufficiencyDecision, Budget]:
    """Retorna (gate_action, decision, budget atualizado com gate_visits+1)."""
    if budget.exhausted("gate_visits"):
        return (
            "refresh",
            SufficiencyDecision(action="refresh", reasons=["max_gate_visits atingido"]),
            budget,
        )

    decision = evaluate_sufficiency(intent, shard, catalog, config, dialect=dialect)

    if decision.action == "unknown" and llm_fallback is not None:
        action = llm_fallback(decision)
        if action not in {"reuse", "refresh"}:
            action = "refresh"
        decision = SufficiencyDecision(
            action=action,
            gaps=decision.gaps,
            reasons=decision.reasons,
        )

    gate_action = decision.action if decision.action in {"reuse", "refresh"} else "refresh"
    updated_budget = budget.model_copy(update={"gate_visits": budget.gate_visits + 1})
    return gate_action, decision, updated_budget


def build_materialization_plan(
    *,
    intent: IntentPlan,
    shard: ShardRouting,
    catalog: DuckDBCatalog,
    config: AgentConfig,
    decision: SufficiencyDecision | None,
    llm_fallback: Callable[[], MaterializationPlan] | None = None,
) -> MaterializationPlan:
    """Plano determinístico quando possível; senão llm_fallback."""
    if decision is not None:
        det = build_deterministic_mat_plan(decision, catalog, config)
        if det is not None:
            return det
    if llm_fallback is None:
        raise ValueError(
            "materialization_plan exige llm_fallback quando plano determinístico indisponível"
        )
    return llm_fallback()


def check_materialization_ready(
    *,
    intent: IntentPlan,
    shard: ShardRouting,
    catalog: DuckDBCatalog,
    config: AgentConfig,
    budget: Budget,
    last_status: str,
    dialect: str | None,
    llm_fallback: Callable[[SufficiencyDecision], bool] | None = None,
) -> tuple[bool, bool, SufficiencyDecision | None]:
    """Retorna (mat_ready, partial, decision?)."""
    if budget.exhausted("mat_loop_count"):
        return True, True, None

    if last_status in {"rejected", "timeout", "error"}:
        return False, False, None

    decision = evaluate_sufficiency(intent, shard, catalog, config, dialect=dialect)

    if decision.action == "reuse":
        return True, False, decision
    if decision.action == "refresh":
        return False, False, decision

    if llm_fallback is not None:
        ready = llm_fallback(decision)
        return ready, False, decision

    return False, False, decision


__all__ = [
    "GateDecision",
    "MaterializationCheck",
    "build_materialization_plan",
    "check_materialization_ready",
    "run_sufficiency_gate",
]
