"""Roteamento determinístico simple vs analítico (dual-path)."""

from __future__ import annotations

from typing import Literal

from txt2sql.artifacts import ShardRouting
from txt2sql.config import AgentConfig
from txt2sql.intent import IntentPlan

ExecutionPath = Literal["simple", "analytical"]


def _touched_table_ids(plan: IntentPlan) -> set[str]:
    ids: set[str] = set()
    for xs in (plan.filters, plan.metrics, plan.group_by, plan.order_by):
        for x in xs:
            ids.add(x.table_id)
    for j in plan.joins:
        ids.add(j.from_table_id)
        ids.add(j.to_table_id)
    for e in plan.entities:
        if e.table_id:
            ids.add(e.table_id)
    return ids


def route_execution(
    intent_plan: IntentPlan,
    shard_routing: ShardRouting,
    config: AgentConfig,
) -> ExecutionPath:
    if getattr(intent_plan, "wants_export", False):
        return "analytical"
    if shard_routing.mode == "multi":
        return "analytical"
    touched = _touched_table_ids(intent_plan)
    for tid in touched:
        try:
            table = config.get_table(tid)
        except KeyError:
            continue
        if table.requires_analytical:
            return "analytical"
        if table.uses_duckdb:
            has_agg = any(
                m.table_id == tid and m.agg and m.agg != "none"
                for m in intent_plan.metrics
            )
            has_gb = any(g.table_id == tid for g in intent_plan.group_by)
            if has_agg or has_gb:
                return "analytical"
    return "simple"


__all__ = ["ExecutionPath", "route_execution"]
