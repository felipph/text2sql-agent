"""Fan-in de shards: materializa bindings resolvidos numa tabela DuckDB lógica."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import inspect

from txt2sql.config import TableConfig
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.db.registry import DatabaseRegistry


def build_in_filter(column: str, values: list[str]) -> str:
    """Monta ``col IN ('a', 'b')`` com escape de aspas simples."""
    literals = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"{column} IN ({literals})"


def _physical_table_exists(engine: Any, table_name: str) -> bool:
    name = table_name.split(".")[-1]
    return inspect(engine).has_table(name)


@dataclass(frozen=True)
class FanInResult:
    """Resultado de um fan-in: tabela lógica materializada no DuckDB."""

    table_id: str
    row_count: int
    physical_tables: list[str] = field(default_factory=list)


def fan_in(
    *,
    session: DuckDBSession,
    table: TableConfig,
    registry: DatabaseRegistry,
    bindings: list[Any],  # list[ShardBinding]
) -> FanInResult:
    """Materializa todos os físicos dos bindings no nome lógico da tabela.

    Verifica existência física antes de materializar. Agrupa por
    ``(database_id, physical_table)`` e executa replace no primeiro grupo,
    append nos seguintes.

    Args:
        session: Sessão DuckDB ativa.
        table: Configuração lógica da tabela shardada.
        registry: Registry de engines de banco.
        bindings: Lista de :class:`~txt2sql.shard_routing.ShardBinding` já
            resolvidos (vindos de ``resolve_routing``).

    Returns:
        :class:`FanInResult` com ``row_count`` e lista de físicos tocados.

    Raises:
        ValueError: Tabela física ausente em algum shard.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for binding in bindings:
        key = (binding.database_id, binding.physical_table)
        groups.setdefault(key, []).append(binding.discriminator_value)

    # Verificação de existência física (gap que _fan_in_sharded_bindings não cobria).
    # Usa inspection engine (sem guardrail read-only) pois inspect() emite PRAGMA no SQLite.
    missing: list[str] = []
    for (db_id, physical), group_vals in groups.items():
        insp_engine = registry.get_inspection_engine(db_id)
        try:
            exists = _physical_table_exists(insp_engine, physical)
        except Exception as err:
            raise ValueError(
                f"Falha ao verificar tabela física {physical!r} em {db_id!r}: {err}"
            ) from err
        if not exists:
            missing.append(
                f"{physical} (banco={db_id}, discriminadores={group_vals})"
            )
    if missing:
        raise ValueError(
            "Tabela(s) física(s) inexistente(s) no shard — não é possível "
            "materializar. Ausentes: " + "; ".join(missing)
        )

    disc_col = table.sharding.discriminator_column if table.sharding else None
    physical_tables: list[str] = []
    first = True
    for (db_id, physical), values in groups.items():
        engine = registry.get_engine(db_id)
        filt = build_in_filter(disc_col, values) if disc_col else None
        logger.info(
            "Fan-in {!r}: banco={} físico={} valores={}",
            table.id,
            db_id,
            physical,
            values,
        )
        session.materialize(
            table,
            engine,
            physical_name=physical,
            filter_sql=filt,
            replace=first,
            append=not first,
        )
        physical_tables.append(physical)
        first = False

    count_rows = session.execute(f'SELECT COUNT(*) AS n FROM "{table.id}"')
    row_count = int(count_rows[0]["n"]) if count_rows else 0
    return FanInResult(
        table_id=table.id,
        row_count=row_count,
        physical_tables=physical_tables,
    )


__all__ = ["FanInResult", "build_in_filter", "fan_in"]
