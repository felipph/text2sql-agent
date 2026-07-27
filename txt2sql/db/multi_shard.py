"""Fan-in multi-shard: materializa vários discriminadores numa tabela DuckDB lógica."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from txt2sql.config import TableConfig
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.db.registry import DatabaseRegistry
from txt2sql.db.shard import ShardResolver


@dataclass(frozen=True)
class MultiMaterializeResult:
    """Resultado de uma materialização multi-shard."""

    table_id: str
    materialized_values: list[str]
    truncated: bool
    omitted_count: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "materialized_values": self.materialized_values,
            "truncated": self.truncated,
            "omitted_count": self.omitted_count,
            "message": self.message,
        }


def build_in_filter(column: str, values: list[str]) -> str:
    """Monta ``col IN ('a', 'b')`` com escape de aspas simples."""
    literals = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"{column} IN ({literals})"


def materialize_sharded_values(
    *,
    table: TableConfig,
    values: list[str],
    max_discriminators: int,
    resolver: ShardResolver,
    registry: DatabaseRegistry,
    session: DuckDBSession,
) -> MultiMaterializeResult:
    """Resolve, agrupa e materializa vários discriminadores no DuckDB.

    Raises:
        ValueError: Lista vazia, um único valor, ou tabela sem sharding/DuckDB.
    """
    if not table.is_sharded or not table.uses_duckdb:
        raise ValueError(
            f"Tabela {table.id!r} precisa ser shardada e com DuckDB habilitado."
        )
    if table.sharding is None:
        raise ValueError(f"Tabela {table.id!r} sem configuração de sharding.")

    cleaned = [str(v).strip() for v in values if v is not None and str(v).strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for v in cleaned:
        if v not in seen:
            seen.add(v)
            unique.append(v)

    if not unique:
        raise ValueError("Lista de discriminadores vazia. Peça os valores ao usuário.")
    if len(unique) == 1:
        raise ValueError(
            "Um único discriminador: use resolve_shard + sql_db_query (caminho single)."
        )

    truncated = len(unique) > max_discriminators
    omitted = max(0, len(unique) - max_discriminators)
    used = unique[:max_discriminators]

    groups: dict[tuple[str, str], list[str]] = {}
    for v in used:
        shard = resolver.resolve(table.id, v)
        key = (shard.database_id, shard.table_name)
        groups.setdefault(key, []).append(v)

    disc_col = table.sharding.discriminator_column
    first = True
    for (db_id, physical), group_vals in groups.items():
        engine = registry.get_engine(db_id)
        filt = build_in_filter(disc_col, group_vals)
        logger.info(
            "Fan-in {!r}: banco={} físico={} valores={}",
            table.id,
            db_id,
            physical,
            group_vals,
        )
        session.materialize(
            table_config=table,
            source_engine=engine,
            physical_name=physical,
            filter_sql=filt,
            replace=first,
            append=not first,
        )
        first = False

    if truncated:
        msg = (
            f"Limite max_shard_discriminators={max_discriminators} atingido; "
            f"{omitted} valor(es) omitido(s). Análise parcial com {len(used)} valor(es)."
        )
    else:
        msg = f"Materializados {len(used)} discriminador(es) em {table.id!r}."

    return MultiMaterializeResult(
        table_id=table.id,
        materialized_values=used,
        truncated=truncated,
        omitted_count=omitted,
        message=msg,
    )


__all__ = [
    "MultiMaterializeResult",
    "build_in_filter",
    "materialize_sharded_values",
]
