"""Lookup determinístico de discriminadores via tabela não-shardada."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from txt2sql.artifacts import DuckDBCatalog
from txt2sql.config import AgentConfig, TableConfig
from txt2sql.intent import FilterClause, IntentPlan
from txt2sql.shard_routing import (
    _discriminator_values_from_filters,
    _touched_table_ids,
)


@dataclass(frozen=True)
class LookupSource:
    lookup_table_id: str
    lookup_column: str
    sharded_table_id: str
    discriminator_column: str


@dataclass(frozen=True)
class LookupResult:
    values: list[str]
    truncated_by_fetch: bool
    source_sql: str
    from_cache: bool
    error: str | None = None


def _preferred_table_ids(intent: IntentPlan) -> set[str]:
    ids: set[str] = set()
    for e in intent.entities:
        if e.table_id:
            ids.add(e.table_id)
    for j in intent.joins:
        ids.add(j.from_table_id)
        ids.add(j.to_table_id)
    return ids


def _candidates_for_sharded(
    sharded: TableConfig,
    config: AgentConfig,
) -> list[LookupSource]:
    assert sharded.sharding is not None
    disc = sharded.sharding.discriminator_column
    out: list[LookupSource] = []

    def consider(sharded_side: str, other: Any) -> None:
        if sharded_side != disc:
            return
        other_table = config.try_get_table(other.table)
        if other_table is None or other_table.is_sharded:
            return
        out.append(
            LookupSource(
                lookup_table_id=other_table.id,
                lookup_column=other.column,
                sharded_table_id=sharded.id,
                discriminator_column=disc,
            )
        )

    for rel in config.relationships:
        if rel.from_ref.table == sharded.id:
            consider(rel.from_ref.column, rel.to_ref)
        if rel.to_ref.table == sharded.id:
            consider(rel.to_ref.column, rel.from_ref)
    return out


def find_lookup_source(
    intent: IntentPlan,
    config: AgentConfig,
) -> LookupSource | None:
    """Encontra fonte não-shardada para discriminador ausente em filters."""
    preferred = _preferred_table_ids(intent)
    candidates: list[LookupSource] = []

    for table_id in sorted(_touched_table_ids(intent)):
        table = config.try_get_table(table_id)
        if table is None or not table.is_sharded or table.sharding is None:
            continue
        disc = table.sharding.discriminator_column
        if _discriminator_values_from_filters(intent, table_id, disc):
            continue
        candidates.extend(_candidates_for_sharded(table, config))

    if not candidates:
        return None

    preferred_hits = [c for c in candidates if c.lookup_table_id in preferred]
    pool = preferred_hits or candidates
    # ordem estável por lookup_table_id
    pool = sorted(pool, key=lambda c: (c.lookup_table_id, c.sharded_table_id))
    return pool[0]


def inject_discriminator_filter(
    intent: IntentPlan,
    source: LookupSource,
    values: list[str],
) -> IntentPlan:
    """Injeta FilterClause(eq|in) com os valores descobertos no lookup."""
    unique = list(dict.fromkeys(str(v) for v in values if str(v).strip()))
    if not unique:
        return intent
    if len(unique) == 1:
        clause = FilterClause(
            table_id=source.sharded_table_id,
            column_id=source.discriminator_column,
            op="eq",
            value=unique[0],
        )
    else:
        clause = FilterClause(
            table_id=source.sharded_table_id,
            column_id=source.discriminator_column,
            op="in",
            value=unique,
        )
    return intent.model_copy(update={"filters": [*intent.filters, clause]})


def _fetch_limit(table: TableConfig) -> int:
    if table.duckdb is not None:
        return max(1, table.duckdb.fetch_limit)
    return 100_000


def _build_distinct_sql(table: TableConfig, column: str, limit: int) -> str:
    # LIMIT limit+1 para detectar truncamento barato
    return (
        f'SELECT DISTINCT "{column}" AS "{column}" '
        f"FROM {table.qualified_name} "
        f"WHERE \"{column}\" IS NOT NULL "
        f"LIMIT {limit + 1}"
    )


def _rows_to_values(rows: list[dict[str, Any]], column: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        raw = row.get(column)
        if raw is None and len(row) == 1:
            raw = next(iter(row.values()))
        if raw is None:
            continue
        s = str(raw).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def run_discriminator_lookup(
    source: LookupSource,
    *,
    config: AgentConfig,
    registry: Any,
    duckdb_session: Any | None = None,
    catalog: DuckDBCatalog | None = None,
) -> LookupResult:
    """Executa DISTINCT do discriminador na lookup table (cache DuckDB ou origem)."""
    table = config.try_get_table(source.lookup_table_id)
    if table is None:
        return LookupResult(
            values=[],
            truncated_by_fetch=False,
            source_sql="",
            from_cache=False,
            error=f"Tabela lookup {source.lookup_table_id!r} inexistente.",
        )

    limit = _fetch_limit(table)
    col = source.lookup_column
    sql = _build_distinct_sql(table, col, limit)

    catalog_has = False
    if catalog is not None:
        catalog_has = any(t.name == source.lookup_table_id for t in catalog.tables)

    use_cache = (
        duckdb_session is not None
        and catalog_has
        and getattr(duckdb_session, "is_materialized", lambda _n: False)(
            source.lookup_table_id
        )
    )

    try:
        if use_cache:
            # nome lógico no DuckDB
            duck_sql = (
                f'SELECT DISTINCT "{col}" AS "{col}" '
                f'FROM "{source.lookup_table_id}" '
                f'WHERE "{col}" IS NOT NULL '
                f"LIMIT {limit + 1}"
            )
            rows = duckdb_session.execute(duck_sql)
            from_cache = True
            sql = duck_sql
        else:
            rows = registry.execute(table.database, sql)
            from_cache = False
    except Exception as exc:  # noqa: BLE001 — surface as clarify signal
        return LookupResult(
            values=[],
            truncated_by_fetch=False,
            source_sql=sql,
            from_cache=False,
            error=(
                f"Falha ao obter discriminadores de {source.lookup_table_id!r}: {exc}"
            ),
        )

    values = _rows_to_values(list(rows or []), col)
    truncated = len(values) > limit
    if truncated:
        values = values[:limit]

    if not values:
        return LookupResult(
            values=[],
            truncated_by_fetch=False,
            source_sql=sql,
            from_cache=from_cache,
            error=(
                f"Nenhum valor de {source.lookup_column!r} encontrado em "
                f"{source.lookup_table_id!r}."
            ),
        )

    return LookupResult(
        values=values,
        truncated_by_fetch=truncated,
        source_sql=sql,
        from_cache=from_cache,
    )


__all__ = [
    "LookupResult",
    "LookupSource",
    "find_lookup_source",
    "inject_discriminator_filter",
    "run_discriminator_lookup",
]
