"""Camada DuckDB intermediária — efêmera por turno.

Para tabelas volumétricas marcadas com ``duckdb.enabled: true``, evitamos rodar
agregações/ordenações/joins diretamente no banco transacional produtivo. Em vez
disso:

1. Buscamos as linhas brutas do banco de origem (``SELECT *`` simples, com
   ``fetch_limit``), em lotes.
2. Materializamos esses lotes em uma tabela DuckDB *in-memory* com o mesmo nome
   lógico.
3. Re-executamos a query analítica original contra o DuckDB.

O :class:`DuckDBSession` é criado no início do turno e destruído ao final
(efêmero) para evitar dados stale entre turnos.
"""

from __future__ import annotations

import re
from typing import Any

import duckdb
from loguru import logger
from sqlalchemy import Engine, text

from txt2sql.config import TableConfig

# Padrões usados para detectar operações analíticas na query.
_RE_GROUP_BY = re.compile(r"\bgroup\s+by\b", re.IGNORECASE)
_RE_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)
_RE_JOIN = re.compile(r"\bjoin\b", re.IGNORECASE)
_RE_AGG_FUNC = re.compile(
    r"\b(count|sum|avg|min|max|stddev|variance|median|group_concat|array_agg)\s*\(",
    re.IGNORECASE,
)

BATCH_SIZE = 5_000


def needs_duckdb(table_config: TableConfig, sql: str) -> bool:
    """Decide se a query em uma tabela deve ser roteada pelo DuckDB.

    Args:
        table_config: Configuração da tabela (com o gatilho DuckDB).
        sql: A query analítica gerada pelo LLM.

    Returns:
        ``True`` se a tabela usa DuckDB e o gatilho configurado casa com a query.
    """
    if not table_config.uses_duckdb:
        return False

    trigger = table_config.duckdb.trigger
    if trigger == "always":
        return True
    if trigger == "aggregation":
        return bool(_RE_GROUP_BY.search(sql) or _RE_AGG_FUNC.search(sql))
    if trigger == "order":
        return bool(_RE_ORDER_BY.search(sql))
    if trigger == "join":
        return bool(_RE_JOIN.search(sql))
    return False


class DuckDBSession:
    """Sessão DuckDB in-memory efêmera para materialização por turno."""

    def __init__(self, database: str = ":memory:") -> None:
        self._conn: duckdb.DuckDBPyConnection = duckdb.connect(database=database)
        self._materialized: set[str] = set()
        self._result_seq: int = 0
        if database == ":memory:":
            logger.debug("DuckDBSession criada (in-memory)")
        else:
            logger.debug("DuckDBSession criada ({})", database)

    # ------------------------------------------------------------------ #
    # Materialização
    # ------------------------------------------------------------------ #
    def is_materialized(self, logical_name: str) -> bool:
        """Indica se a tabela lógica já foi materializada neste turno."""
        return logical_name in self._materialized

    def materialize(
        self,
        table_config: TableConfig,
        source_engine: Engine,
        physical_name: str | None = None,
        filter_sql: str | None = None,
        *,
        append: bool = False,
        replace: bool = False,
    ) -> None:
        """Materializa as linhas brutas de uma tabela de origem no DuckDB.

        Args:
            table_config: Configuração da tabela volumétrica.
            source_engine: Engine SQLAlchemy do banco de origem.
            physical_name: Nome físico real da tabela na origem (para shards).
                Se ``None``, usa ``table_config.qualified_name``.
            filter_sql: Cláusula ``WHERE`` opcional (sem a palavra ``WHERE``)
                para reduzir o volume trazido do banco de origem.
            append: Se ``True`` e a tabela lógica já existe, apenas insere linhas.
            replace: Se ``True``, descarta a tabela lógica existente antes de
                materializar de novo.

        A tabela DuckDB criada usa o nome lógico ``table_config.id`` para que a
        query analítica original (reescrita para o nome lógico) funcione.
        """
        if append and replace:
            raise ValueError("append e replace são mutuamente exclusivos")

        logical_name = table_config.id
        if replace and logical_name in self._materialized:
            self._conn.execute(f'DROP TABLE IF EXISTS "{logical_name}"')
            self._materialized.discard(logical_name)
            logger.debug("Tabela {!r} removida para replace", logical_name)

        already = logical_name in self._materialized
        if already and not append:
            logger.debug("Tabela {!r} já materializada; pulando", logical_name)
            return

        source_name = physical_name or table_config.qualified_name
        fetch_limit = table_config.duckdb.fetch_limit if table_config.duckdb else 100_000

        where_part = f" WHERE {filter_sql}" if filter_sql else ""
        select_sql = f"SELECT * FROM {source_name}{where_part} LIMIT {fetch_limit}"

        logger.info(
            "Materializando {!r} no DuckDB a partir de {!r} (limit={}, append={})",
            logical_name,
            source_name,
            fetch_limit,
            append and already,
        )

        total_rows = 0
        with source_engine.connect() as conn:
            result = conn.execute(text(select_sql))
            columns = list(result.keys())
            first_batch = [tuple(r) for r in result.fetchmany(BATCH_SIZE)]

            if already and append:
                if first_batch:
                    self._insert_batch(logical_name, columns, first_batch)
                    total_rows += len(first_batch)
                    while True:
                        batch = [tuple(r) for r in result.fetchmany(BATCH_SIZE)]
                        if not batch:
                            break
                        self._insert_batch(logical_name, columns, batch)
                        total_rows += len(batch)
            elif not first_batch:
                self._create_empty_table(logical_name, columns)
            else:
                self._conn.execute(
                    f'CREATE TABLE "{logical_name}" ({self._infer_schema(columns, first_batch)})'
                )
                self._insert_batch(logical_name, columns, first_batch)
                total_rows += len(first_batch)

                while True:
                    batch = [tuple(r) for r in result.fetchmany(BATCH_SIZE)]
                    if not batch:
                        break
                    self._insert_batch(logical_name, columns, batch)
                    total_rows += len(batch)

        self._materialized.add(logical_name)
        logger.info("Tabela {!r} materializada com {} linha(s)", logical_name, total_rows)

    def _create_empty_table(self, name: str, columns: list[str]) -> None:
        """Cria tabela DuckDB vazia com colunas VARCHAR."""
        col_defs = ", ".join(f'"{c}" VARCHAR' for c in columns)
        self._conn.execute(f'CREATE TABLE "{name}" ({col_defs})')

    def _insert_batch(
        self, name: str, columns: list[str], rows: list[tuple[Any, ...]]
    ) -> None:
        """Insere um lote de linhas na tabela DuckDB."""
        if not rows:
            return
        col_defs = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(["?"] * len(columns))
        self._conn.executemany(
            f'INSERT INTO "{name}" ({col_defs}) VALUES ({placeholders})',
            rows,
        )

    @staticmethod
    def _infer_schema(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
        """Infere um schema DuckDB simples a partir das primeiras linhas."""
        import datetime
        from decimal import Decimal

        def duck_type(value: Any) -> str:
            if isinstance(value, bool):
                return "BOOLEAN"
            if isinstance(value, int):
                return "BIGINT"
            if isinstance(value, (float, Decimal)):
                return "DOUBLE"
            if isinstance(value, datetime.datetime):
                return "TIMESTAMP"
            if isinstance(value, datetime.date):
                return "DATE"
            return "VARCHAR"

        # usa a primeira linha não-nula por coluna para inferir tipo
        types: list[str] = []
        for idx, col in enumerate(columns):
            col_type = "VARCHAR"
            for row in rows:
                if idx < len(row) and row[idx] is not None:
                    col_type = duck_type(row[idx])
                    break
            types.append(f'"{col}" {col_type}')
        return ", ".join(types)

    def store_result_rows(self, rows: list[dict[str, Any]]) -> str:
        """Persiste linhas completas de um resultado truncado; retorna ref ``duckdb://...``."""
        self._result_seq += 1
        table_name = f"result_{self._result_seq}"
        if not rows:
            self._conn.execute(
                f'CREATE TABLE "{table_name}" (placeholder VARCHAR)'
            )
        else:
            columns = list(rows[0].keys())
            col_defs = ", ".join(f'"{c}" VARCHAR' for c in columns)
            self._conn.execute(f'CREATE TABLE "{table_name}" ({col_defs})')
            placeholders = ", ".join(["?"] * len(columns))
            col_list = ", ".join(f'"{c}"' for c in columns)
            values = [tuple(row.get(c) for c in columns) for row in rows]
            self._conn.executemany(
                f'INSERT INTO "{table_name}" ({col_list}) VALUES ({placeholders})',
                values,
            )
        return f"duckdb://{table_name}"

    # ------------------------------------------------------------------ #
    # Execução analítica
    # ------------------------------------------------------------------ #
    def execute(self, sql: str) -> list[dict[str, Any]]:
        """Executa a query analítica contra o DuckDB e retorna linhas como dicts.

        Args:
            sql: Query (referenciando os nomes lógicos das tabelas materializadas).

        Returns:
            Lista de dicionários ``{coluna: valor}``.
        """
        logger.debug("Executando query no DuckDB: {}", sql)
        cursor = self._conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]

    # ------------------------------------------------------------------ #
    # Ciclo de vida
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """Fecha a conexão DuckDB e descarta todos os dados do turno."""
        try:
            self._conn.close()
        finally:
            self._materialized.clear()
            logger.debug("DuckDBSession fechada (dados do turno descartados)")


__all__ = ["DuckDBSession", "needs_duckdb"]
