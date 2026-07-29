"""Policy Gate (S5): validação composta antes da execução SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import sqlglot
from sqlglot import exp

from txt2sql.artifacts import DuckDBCatalog, ShardRouting, SQLPlan
from txt2sql.config import AgentConfig, ShardResult
from txt2sql.guardrail import ReadOnlyViolationError, validate_sql
from txt2sql.query_routing import TableRef, analyze_table_refs, routing_rejection_reason

_DEFAULT_MAX_ROWS = 500_000

_AGG_EXPRESSIONS: tuple[type[exp.Expression], ...] = (
    exp.Sum,
    exp.Count,
    exp.Avg,
    exp.Min,
    exp.Max,
)


@dataclass(frozen=True)
class PolicyDecision:
    status: Literal["ok", "rejected"]
    sql: str
    error: str | None = None


def _bindings_to_resolved_shards(
    shard_routing: ShardRouting,
) -> dict[tuple[str, str], ShardResult]:
    return {
        (b.table_id, b.discriminator_value): ShardResult(
            database_id=b.database_id,
            table_name=b.physical_table,
        )
        for b in shard_routing.bindings
    }


def _multi_materialized(
    shard_routing: ShardRouting,
    duckdb_catalog: DuckDBCatalog | None = None,
) -> dict[str, dict[str, Any]] | None:
    out: dict[str, dict[str, Any]] = {}
    if shard_routing.mode == "multi" and shard_routing.logical_table:
        out[shard_routing.logical_table] = {}
    if duckdb_catalog:
        for info in duckdb_catalog.tables:
            out[info.name] = {}
    return out or None


def _build_allowed_tables(
    config: AgentConfig,
    shard_routing: ShardRouting,
) -> list[str]:
    allowed: list[str] = []
    for table in config.tables:
        allowed.extend([table.id, table.name, table.qualified_name])
    for binding in shard_routing.bindings:
        allowed.append(binding.physical_table)
        table = config.get_table(binding.table_id)
        allowed.extend([table.id, table.name])
    if shard_routing.mode == "multi" and shard_routing.logical_table:
        table = config.get_table(shard_routing.logical_table)
        allowed.extend([table.id, table.name, table.qualified_name])
    return allowed


def _has_aggregation(sql: str, dialect: str | None) -> bool:
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:  # noqa: BLE001 — fail-closed
        return False
    if parsed.find(exp.Group):
        return True
    for agg_type in _AGG_EXPRESSIONS:
        if parsed.find(agg_type):
            return True
    return False


def _inject_limit_if_missing(sql: str, max_rows: int, dialect: str | None) -> str:
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:  # noqa: BLE001 — fallback: append seguro
        return f"{sql.rstrip()} LIMIT {max_rows}"
    if parsed.find(exp.Limit):
        return sql
    limited = parsed.limit(max_rows)
    return limited.sql(dialect=dialect)


def _source_extract_rejection(
    sql: str,
    config: AgentConfig,
    resolved_shards: dict[tuple[str, str], ShardResult],
    multi_materialized: dict[str, dict[str, Any]] | None,
    dialect: str | None,
) -> str | None:
    if not _has_aggregation(sql, dialect):
        return None
    refs = analyze_table_refs(
        sql, config, resolved_shards, multi_materialized, dialect
    )
    analytical = [
        r for r in refs if r.table is not None and r.table.requires_analytical
    ]
    if not analytical:
        return None
    names = ", ".join(f"`{r.name}`" for r in analytical)
    return (
        f"Agregação na origem não permitida para tabela(s) com force_analytical: "
        f"{names}. Use pushdown (extract filtrado) → DuckDB → análise agregada."
    )


def _physical_in_duckdb_rejection(
    refs: list[TableRef],
    shard_routing: ShardRouting,
    duckdb_catalog: DuckDBCatalog | None,
) -> str | None:
    """Rejeita nomes físicos de shard quando o lógico já está no DuckDB.

    O LLM às vezes gera ``UNION recebiveis_654 / recebiveis_747`` no dialect
    duckdb — essas tabelas não existem na sessão; o fan-in usa o nome lógico.
    """
    if duckdb_catalog is None or not duckdb_catalog.tables:
        return None

    catalog_names = {t.name.lower() for t in duckdb_catalog.tables}
    physical_to_logical: dict[str, str] = {}
    for binding in shard_routing.bindings:
        physical_to_logical[binding.physical_table.lower()] = binding.table_id
    for info in duckdb_catalog.tables:
        for binding in info.shard_bindings:
            physical_to_logical[binding.physical_table.lower()] = info.name

    bad: list[tuple[str, str]] = []
    for ref in refs:
        if ref.kind != "resolved_physical":
            continue
        logical = physical_to_logical.get(ref.name.lower())
        if logical and logical.lower() in catalog_names:
            bad.append((ref.name, logical))

    if not bad:
        return None

    pairs = ", ".join(f"`{phys}` → use `{logical}`" for phys, logical in bad)
    logicals = ", ".join(sorted({f"`{logical}`" for _, logical in bad}))
    return (
        f"SQL DuckDB referenciou nome(s) físico(s) de shard já materializado(s): "
        f"{pairs}. No path analítico use apenas o nome lógico do catálogo "
        f"({logicals}) — nunca UNION de tabelas físicas "
        f"(ex. recebiveis_654 UNION recebiveis_747)."
    )


def check_sql_plan(
    plan: SQLPlan,
    *,
    config: AgentConfig,
    shard_routing: ShardRouting,
    path: Literal["simple", "analytical"] = "simple",
    context: Literal["query", "source_extract"] = "query",
    max_rows: int | None = None,
    dialect: str | None = None,
    duckdb_catalog: DuckDBCatalog | None = None,
) -> PolicyDecision:
    effective_dialect = dialect or plan.dialect or config.dialect
    allowed_tables = _build_allowed_tables(config, shard_routing)

    try:
        sql = validate_sql(
            plan.sql,
            dialect=effective_dialect,
            allowed_tables=allowed_tables,
        )
    except ReadOnlyViolationError as err:
        return PolicyDecision(status="rejected", sql=plan.sql, error=str(err))

    resolved_shards = _bindings_to_resolved_shards(shard_routing)
    multi = _multi_materialized(shard_routing, duckdb_catalog)

    refs = analyze_table_refs(
        sql, config, resolved_shards, multi, effective_dialect
    )

    phys_err = _physical_in_duckdb_rejection(refs, shard_routing, duckdb_catalog)
    if phys_err is not None:
        return PolicyDecision(status="rejected", sql=sql, error=phys_err)

    routing_err = routing_rejection_reason(refs)
    if routing_err is not None:
        return PolicyDecision(status="rejected", sql=sql, error=routing_err)

    if context == "source_extract":
        extract_err = _source_extract_rejection(
            sql, config, resolved_shards, multi, effective_dialect
        )
        if extract_err is not None:
            return PolicyDecision(status="rejected", sql=sql, error=extract_err)

    effective_max_rows = max_rows if max_rows is not None else _DEFAULT_MAX_ROWS
    sql = _inject_limit_if_missing(sql, effective_max_rows, effective_dialect)

    return PolicyDecision(status="ok", sql=sql)
