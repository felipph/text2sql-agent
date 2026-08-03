"""Exportação CSV denormalizada em streaming (DuckDB COPY TO)."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from txt2sql.config import DEFAULT_EXPORT_DETECT_KEYWORDS, AgentConfig, ExportConfig, TableConfig
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.intent import IntentPlan
from txt2sql.shard_routing import _touched_table_ids

_SAFE_THREAD_RE = re.compile(r"[^a-zA-Z0-9_-]+")


@dataclass(frozen=True)
class ExportResult:
    path: Path
    url: str
    row_count: int
    truncated: bool
    filename: str


def build_export_url(base_url: str, filename: str) -> str:
    return f"{base_url.rstrip('/')}/{filename.lstrip('/')}"


def _safe_thread_id(thread_id: str) -> str:
    cleaned = _SAFE_THREAD_RE.sub("_", thread_id.strip()) or "default"
    return cleaned[:64]


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def export_denormalized_csv(
    *,
    session: DuckDBSession,
    select_sql: str,
    config: ExportConfig,
    thread_id: str,
) -> ExportResult:
    """Grava CSV via ``COPY ... TO`` (streaming no DuckDB) e retorna path + URL."""
    if not config.enabled:
        raise RuntimeError("export.enabled=false")
    out_dir = Path(config.dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{_safe_thread_id(thread_id)}_{uuid.uuid4().hex}.csv"
    path = out_dir / filename

    stripped = select_sql.strip().rstrip(";")
    count_rows = session.execute(
        f"SELECT COUNT(*) AS n FROM ({stripped}) AS _export_src"
    )
    total = int(count_rows[0]["n"]) if count_rows else 0
    truncated = total > config.max_rows
    limited_sql = stripped
    if truncated:
        limited_sql = (
            f"SELECT * FROM ({stripped}) AS _export_src LIMIT {int(config.max_rows)}"
        )

    abs_path = _sql_literal(str(path.resolve()))
    delim = _sql_literal(config.delimiter)
    copy_sql = (
        f"COPY ({limited_sql}) TO '{abs_path}' "
        f"(HEADER, DELIMITER '{delim}', FORMAT CSV)"
    )
    session.execute_statement(copy_sql)

    row_count = min(total, config.max_rows)
    url = build_export_url(config.base_url, filename)
    logger.info(
        "CSV exportado: file={} rows={} truncated={}",
        path,
        row_count,
        truncated,
    )
    return ExportResult(
        path=path,
        url=url,
        row_count=row_count,
        truncated=truncated,
        filename=filename,
    )


def cleanup_expired_exports(dir: Path | str, ttl_seconds: int) -> int:
    """Remove arquivos com mtime mais antigo que ``ttl_seconds``. Retorna qtde removida."""
    if ttl_seconds < 1:
        raise ValueError(f"ttl_seconds deve ser >= 1, recebido: {ttl_seconds}")
    root = Path(dir)
    if not root.is_dir():
        return 0
    cutoff = time.time() - ttl_seconds
    removed = 0
    for path in root.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError as exc:
            logger.warning("Falha ao remover export {}: {}", path, exc)
    return removed


def detect_wants_export(
    text: str,
    keywords: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Heurística textual de pedido de export (fallback se IntentPlan omitir).

    ``keywords=None`` usa defaults da lib; lista vazia desliga a heurística.
    """
    if keywords is not None and len(keywords) == 0:
        return False
    keys = tuple(keywords) if keywords is not None else DEFAULT_EXPORT_DETECT_KEYWORDS
    low = (text or "").lower()
    return any(k.lower() in low for k in keys)


def _columns_for_table(table: TableConfig) -> list[str]:
    if table.columns:
        return [c.name for c in table.columns]
    return ["*"]


def build_denormalized_select(
    intent: IntentPlan,
    config: AgentConfig,
    *,
    available_tables: set[str] | None = None,
) -> str:
    """Monta SELECT denormalizado (sem agregação) a partir de joins/entities.

    Usa apenas nomes lógicos DuckDB. Se ``available_tables`` for passado,
    restringe às tabelas já materializadas.
    """
    touched = sorted(_touched_table_ids(intent))
    if available_tables is not None:
        touched = [t for t in touched if t in available_tables]
    if not touched:
        raise ValueError("Nenhuma tabela disponível para export denormalizado.")

    # Prefer joins do intent; senão relationships cobrindo touched
    joins = list(intent.joins)
    if not joins and len(touched) >= 2:
        for rel in config.relationships:
            a, b = rel.from_ref.table, rel.to_ref.table
            if a in touched and b in touched:
                from txt2sql.intent import JoinClause, JoinOn

                joins.append(
                    JoinClause(
                        from_table_id=a,
                        to_table_id=b,
                        on=[
                            JoinOn(
                                from_column=rel.from_ref.column,
                                to_column=rel.to_ref.column,
                            )
                        ],
                    )
                )
                break

    aliases: dict[str, str] = {}
    select_parts: list[str] = []
    used_tables: list[str] = []

    def alias_for(table_id: str) -> str:
        if table_id not in aliases:
            aliases[table_id] = f"t{len(aliases)}"
            used_tables.append(table_id)
        return aliases[table_id]

    if joins:
        first = joins[0].from_table_id
        alias_for(first)
        for j in joins:
            alias_for(j.from_table_id)
            alias_for(j.to_table_id)
    else:
        alias_for(touched[0])

    for table_id in used_tables:
        table = config.try_get_table(table_id)
        alias = aliases[table_id]
        cols = _columns_for_table(table) if table is not None else ["*"]
        if cols == ["*"]:
            select_parts.append(f'{alias}.*')
        else:
            for col in cols:
                select_parts.append(f'{alias}."{col}" AS "{table_id}__{col}"')

    if not select_parts:
        select_parts.append("*")

    base_id = used_tables[0]
    base_alias = aliases[base_id]
    sql = f'SELECT {", ".join(select_parts)} FROM "{base_id}" AS {base_alias}'

    joined: set[str] = {base_id}
    for j in joins:
        # anexar o lado ainda não joined
        if j.to_table_id not in joined and j.from_table_id in joined:
            right, left = j.to_table_id, j.from_table_id
            on_pairs = [
                (f'{aliases[left]}."{p.from_column}"', f'{aliases[right]}."{p.to_column}"')
                for p in j.on
            ]
        elif j.from_table_id not in joined and j.to_table_id in joined:
            right, left = j.from_table_id, j.to_table_id
            on_pairs = [
                (f'{aliases[left]}."{p.to_column}"', f'{aliases[right]}."{p.from_column}"')
                for p in j.on
            ]
        else:
            continue
        on_sql = " AND ".join(f"{a} = {b}" for a, b in on_pairs) or "1=1"
        sql += f' LEFT JOIN "{right}" AS {aliases[right]} ON {on_sql}'
        joined.add(right)

    # Filtros não-discriminadores opcionais (eq/in simples) — best-effort
    where: list[str] = []
    for f in intent.filters:
        if f.table_id not in aliases:
            continue
        alias = aliases[f.table_id]
        col = f'"{f.column_id}"'
        if f.op == "eq" and f.value is not None:
            where.append(f"{alias}.{col} = '{_sql_literal(str(f.value))}'")
        elif f.op == "in" and isinstance(f.value, list) and f.value:
            vals = ", ".join(f"'{_sql_literal(str(v))}'" for v in f.value)
            where.append(f"{alias}.{col} IN ({vals})")
    if where:
        sql += " WHERE " + " AND ".join(where)

    return sql


__all__ = [
    "ExportResult",
    "build_denormalized_select",
    "build_export_url",
    "cleanup_expired_exports",
    "detect_wants_export",
    "export_denormalized_csv",
]
