"""Roteamento fail-closed de queries multi-banco / shardadas.

Detecta referências a tabelas shardadas sem resolução prévia e JOINs
cross-database, rejeitando antes da execução no OLTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import sqlglot

from txt2sql.config import AgentConfig, ShardResult, TableConfig

RefKind = Literal[
    "non_sharded",
    "resolved_physical",
    "multi_logical",
    "unresolved_sharded",
    "unknown",
]


@dataclass(frozen=True)
class TableRef:
    """Uma referência a tabela encontrada na SQL."""

    name: str
    kind: RefKind
    table: TableConfig | None = None
    database_id: str | None = None


def extract_table_names(sql: str, dialect: str | None) -> list[str]:
    """Extrai nomes de tabela (e schema.tabela) referenciados na SQL."""
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:  # noqa: BLE001 — fail-closed: sem parse → lista vazia
        return []
    names: list[str] = []
    for tbl in parsed.find_all(sqlglot.exp.Table):
        names.append(tbl.name.lower())
        if tbl.db:
            names.append(f"{tbl.db}.{tbl.name}".lower())
    return names


def analyze_table_refs(
    sql: str,
    config: AgentConfig,
    resolved_shards: dict[tuple[str, str], ShardResult],
    multi_materialized: dict[str, dict[str, Any]] | None,
    dialect: str | None,
) -> list[TableRef]:
    """Classifica cada referência de tabela quanto a shard/banco."""
    multi = multi_materialized or {}
    sharded_by_name: dict[str, TableConfig] = {}
    for t in config.sharded_tables:
        sharded_by_name[t.id.lower()] = t
        sharded_by_name[t.name.lower()] = t
        if t.schema:
            sharded_by_name[f"{t.schema}.{t.name}".lower()] = t

    physical_shards: dict[str, tuple[TableConfig, str]] = {}
    for (table_id, _value), shard in resolved_shards.items():
        table = config.get_table(table_id)
        physical_shards[shard.table_name.lower()] = (table, shard.database_id)

    non_sharded: dict[str, tuple[TableConfig, str]] = {}
    for table in config.tables:
        if table.is_sharded:
            continue
        non_sharded[table.name.lower()] = (table, table.database)
        non_sharded[table.id.lower()] = (table, table.database)
        non_sharded[table.qualified_name.lower()] = (table, table.database)

    refs: list[TableRef] = []
    seen: set[str] = set()
    for name in extract_table_names(sql, dialect):
        if name in seen:
            continue
        seen.add(name)

        if name in physical_shards:
            table, db_id = physical_shards[name]
            refs.append(
                TableRef(name=name, kind="resolved_physical", table=table, database_id=db_id)
            )
            continue

        # nome lógico pós fan-in multi-shard
        matched_multi = False
        for tid in multi:
            table = config.get_table(tid)
            if name in {tid.lower(), table.name.lower(), table.id.lower()}:
                refs.append(
                    TableRef(
                        name=name,
                        kind="multi_logical",
                        table=table,
                        database_id=None,  # DuckDB
                    )
                )
                matched_multi = True
                break
        if matched_multi:
            continue

        if name in sharded_by_name:
            refs.append(
                TableRef(
                    name=name,
                    kind="unresolved_sharded",
                    table=sharded_by_name[name],
                    database_id=None,
                )
            )
            continue

        if name in non_sharded:
            table, db_id = non_sharded[name]
            refs.append(
                TableRef(name=name, kind="non_sharded", table=table, database_id=db_id)
            )
            continue

        refs.append(TableRef(name=name, kind="unknown"))

    return refs


def routing_rejection_reason(refs: list[TableRef]) -> str | None:
    """Retorna mensagem de rejeição fail-closed, ou ``None`` se a query pode seguir.

    Regras:
    * Nome lógico de tabela shardada sem ``resolve_shard`` /
      ``materialize_sharded_table`` → rejeitar.
    * Referências que implicam mais de um banco (ou DuckDB + OLTP) → rejeitar
      JOIN/consulta cross-database.
    """
    unresolved = [r for r in refs if r.kind == "unresolved_sharded"]
    if unresolved:
        names = ", ".join(f"`{r.name}`" for r in unresolved)
        return (
            f"Tabela(s) shardada(s) referenciada(s) pelo nome lógico sem "
            f"`resolve_shard` (1 discriminador → nome físico) nem "
            f"`materialize_sharded_table` (2+ discriminadores → nome lógico no "
            f"DuckDB): {names}. Não faça JOIN dessa tabela com outras no OLTP."
        )

    destinations: set[str] = set()
    for r in refs:
        if r.kind == "multi_logical":
            destinations.add("duckdb")
        elif r.kind in ("non_sharded", "resolved_physical") and r.database_id:
            destinations.add(r.database_id)
        # unknown: deixado para o guardrail de allowlist / erro de execução

    if len(destinations) > 1:
        detail = ", ".join(sorted(destinations))
        return (
            "Consulta/JOIN cross-database não é permitida. As tabelas "
            f"referenciadas resolvem para destinos distintos ({detail}). "
            "Consulte um banco (ou o DuckDB após materialize) por vez; "
            "correlacione resultados em passos separados se necessário."
        )

    return None
