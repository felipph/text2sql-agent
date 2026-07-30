"""Materialização de tabelas no DuckDB intermediário (módulo deep)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from txt2sql.artifacts import (
    DuckDBCatalog,
    DuckDBTableInfo,
    MaterializationPlan,
    MaterializationStep,
    ShardBinding,
    ShardRouting,
    SQLPlan,
)
from txt2sql.config import AgentConfig, TableConfig
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.db.fan_in import fan_in
from txt2sql.db.registry import DatabaseRegistry, QueryTimeoutError
from txt2sql.intent import IntentPlan
from txt2sql.policy import check_sql_plan
from txt2sql.query_routing import extract_table_names
from txt2sql.sufficiency import intent_table_ids


@dataclass(frozen=True)
class MaterializeOutcome:
    """Resultado da materialização de tabelas no DuckDB."""

    catalog: DuckDBCatalog
    sample_rows: list[dict[str, Any]]
    error: str | None = None
    error_kind: Literal["ok", "rejected", "timeout", "error"] = "ok"


def _table_ids_from_mat_plan(
    mat_plan: MaterializationPlan,
    agent_config: AgentConfig,
    dialect: str | None,
) -> set[str]:
    """Table ids citados em target_table / source_query do plano de materialização."""
    ids: set[str] = set()
    by_name: dict[str, str] = {}
    for table in agent_config.tables:
        by_name[table.id.lower()] = table.id
        by_name[table.name.lower()] = table.id
        by_name[table.qualified_name.lower()] = table.id

    for step in mat_plan.steps:
        target = (step.target_table or "").lower()
        if target in by_name:
            ids.add(by_name[target])
        for name in extract_table_names(step.source_query or "", dialect):
            if name in by_name:
                ids.add(by_name[name])
    return ids


def _resolve_step_table(
    step: MaterializationStep,
    *,
    shard: ShardRouting,
    intent: IntentPlan,
    agent_config: AgentConfig,
) -> TableConfig:
    """Resolve a :class:`TableConfig` lógica para um passo de materialização."""
    if step.shard_binding is not None:
        table = agent_config.try_get_table(step.shard_binding.table_id)
        if table is not None:
            return table

    table = agent_config.try_get_table(step.target_table)
    if table is not None:
        return table

    target_l = (step.target_table or "").lower()
    if target_l:
        for candidate in agent_config.tables:
            if target_l.startswith((candidate.id.lower(), candidate.name.lower())):
                return candidate

    if len(shard.bindings) == 1:
        table = agent_config.try_get_table(shard.bindings[0].table_id)
        if table is not None:
            return table

    intent_ids = sorted(intent_table_ids(intent))
    if len(intent_ids) == 1:
        table = agent_config.try_get_table(intent_ids[0])
        if table is not None:
            return table

    known = ", ".join(sorted(t.id for t in agent_config.tables)) or "(nenhuma)"
    raise KeyError(
        f"Não foi possível mapear target_table={step.target_table!r} a um "
        f"table_id conhecido. IDs: {known}"
    )


def _bindings_for_table(
    table: TableConfig,
    shard: ShardRouting,
    step: MaterializationStep | None,
) -> list[ShardBinding]:
    """Bindings de shard para ``table`` — nunca usa binding de outra tabela."""
    if not table.is_sharded:
        return []
    if step is not None and step.shard_bindings:
        return [b for b in step.shard_bindings if b.table_id == table.id]
    matching = [b for b in shard.bindings if b.table_id == table.id]
    if matching:
        return matching
    if (
        step is not None
        and step.shard_binding is not None
        and step.shard_binding.table_id == table.id
    ):
        return [step.shard_binding]
    return []


def _upsert_catalog(
    catalog: DuckDBCatalog,
    *,
    logical_name: str,
    row_count: int,
    queries: list[str],
    shard_bindings: list[ShardBinding] | None = None,
) -> DuckDBCatalog:
    info = DuckDBTableInfo(
        name=logical_name,
        row_count=row_count,
        source_queries=queries,
        shard_bindings=list(shard_bindings or []),
        materialized_at=datetime.now(UTC),
    )
    return DuckDBCatalog(
        tables=[t for t in catalog.tables if t.name != logical_name] + [info]
    )


def _error_outcome(
    catalog: DuckDBCatalog,
    sample_rows: list[dict[str, Any]],
    message: str,
    *,
    kind: Literal["rejected", "timeout", "error"],
) -> MaterializeOutcome:
    return MaterializeOutcome(
        catalog=catalog,
        sample_rows=sample_rows,
        error=message,
        error_kind=kind,
    )


def materialize_tables(
    *,
    mat_plan: MaterializationPlan,
    intent: IntentPlan,
    shard: ShardRouting,
    catalog: DuckDBCatalog,
    session: DuckDBSession,
    registry: DatabaseRegistry,
    config: AgentConfig,
    max_rows_per_extract: int,
    dialect: str | None,
) -> MaterializeOutcome:
    """Materializa steps do plano + tabelas do intent ausentes no catálogo."""
    total_rows = 0
    last_rows: list[dict[str, Any]] = []
    source_queries_by_table: dict[str, list[str]] = {}

    def _materialize_one(
        table: TableConfig,
        step: MaterializationStep | None,
    ) -> MaterializeOutcome | None:
        nonlocal catalog, total_rows, last_rows
        logical_name = table.id
        bindings = _bindings_for_table(table, shard, step)
        queries = (
            [step.source_query]
            if step is not None and step.source_query
            else source_queries_by_table.get(logical_name, [])
        )

        if table.is_sharded and len(bindings) >= 1:
            try:
                result = fan_in(
                    session=session,
                    table=table,
                    registry=registry,
                    bindings=bindings,
                )
            except ValueError as err:
                return _error_outcome(catalog, last_rows, str(err), kind="rejected")
            total_rows = result.row_count
            last_rows = session.execute(f'SELECT * FROM "{logical_name}" LIMIT 5')
            source_queries_by_table[logical_name] = queries or [
                f"fan-in:{len(bindings)} bindings"
            ]
            catalog = _upsert_catalog(
                catalog,
                logical_name=logical_name,
                row_count=total_rows,
                queries=source_queries_by_table[logical_name],
                shard_bindings=bindings,
            )
            return None

        # Não-shardada (ou shardada sem binding — extract via SQL do plano)
        if step is not None and step.source_query.strip():
            extract_sql = step.source_query
        else:
            extract_sql = f"SELECT * FROM {table.qualified_name}"

        extract_plan = SQLPlan(
            sql=extract_sql,
            dialect=dialect or "postgres",
        )
        decision = check_sql_plan(
            extract_plan,
            config=config,
            shard_routing=shard,
            path="analytical",
            context="source_extract",
            dialect=dialect,
            max_rows=max_rows_per_extract,
        )
        if decision.status == "rejected":
            return _error_outcome(
                catalog,
                last_rows,
                decision.error or "rejeitado",
                kind="rejected",
            )

        db_id = table.database
        source_engine = registry.get_engine(db_id)
        try:
            session.materialize(
                table,
                source_engine,
                source_sql=decision.sql,
                replace=True,
            )
        except QueryTimeoutError as err:
            return _error_outcome(catalog, last_rows, str(err), kind="timeout")
        except Exception as err:  # noqa: BLE001
            return _error_outcome(catalog, last_rows, str(err), kind="error")

        last_rows = session.execute(f'SELECT * FROM "{logical_name}" LIMIT 5')
        count_rows = session.execute(f'SELECT COUNT(*) AS n FROM "{logical_name}"')
        total_rows = int(count_rows[0]["n"]) if count_rows else len(last_rows)
        source_queries_by_table[logical_name] = [decision.sql]
        catalog = _upsert_catalog(
            catalog,
            logical_name=logical_name,
            row_count=total_rows,
            queries=[decision.sql],
            shard_bindings=bindings,
        )
        return None

    planned_ids: list[str] = []
    for step in mat_plan.steps:
        try:
            table = _resolve_step_table(
                step, shard=shard, intent=intent, agent_config=config
            )
        except KeyError as err:
            return _error_outcome(catalog, last_rows, str(err), kind="rejected")

        err_outcome = _materialize_one(table, step)
        if err_outcome is not None:
            return err_outcome
        planned_ids.append(table.id)

    needed = intent_table_ids(intent) | _table_ids_from_mat_plan(
        mat_plan, config, dialect
    )
    for tid in sorted(needed):
        if tid in planned_ids:
            continue
        if any(t.name == tid for t in catalog.tables):
            continue
        table = config.try_get_table(tid)
        if table is None or not table.uses_duckdb:
            continue
        err_outcome = _materialize_one(table, step=None)
        if err_outcome is not None:
            return err_outcome

    return MaterializeOutcome(
        catalog=catalog,
        sample_rows=last_rows,
        error=None,
        error_kind="ok",
    )


__all__ = ["MaterializeOutcome", "materialize_tables"]
