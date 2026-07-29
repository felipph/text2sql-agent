"""Carregamento de schema — declarativo (YAML) OU discovery (SQLAlchemy).

O :class:`SchemaLoader` produz uma descrição textual de cada tabela no formato
que o LLM espera (DDL simplificado + linhas de amostra), escolhendo entre:

* **Declarativo**: quando a tabela declara ``columns`` no YAML — não toca o banco.
* **Discovery**: quando não há colunas declaradas — reflete via ``inspect()``.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import inspect, text

from txt2sql.config import AgentConfig, TableConfig
from txt2sql.db.registry import DatabaseRegistry


class SchemaLoader:
    """Gera informação de schema por tabela para consumo do LLM.

    Args:
        config: Configuração do agente.
        registry: Registro de bancos (usado no modo discovery).
    """

    def __init__(self, config: AgentConfig, registry: DatabaseRegistry) -> None:
        self._config = config
        self._registry = registry
        self._cache: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Nomes lógicos
    # ------------------------------------------------------------------ #
    def get_all_table_names(self) -> list[str]:
        """Retorna os nomes lógicos (IDs) de todas as tabelas conhecidas."""
        return [t.id for t in self._config.tables]

    # ------------------------------------------------------------------ #
    # Info por tabela
    # ------------------------------------------------------------------ #
    def get_table_info(self, table_id: str, include_samples: bool = True) -> str:
        """Retorna a descrição textual de schema de uma tabela.

        Args:
            table_id: ID lógico da tabela.
            include_samples: Se ``True``, tenta incluir linhas de amostra
                (apenas no modo discovery; ignorado no declarativo).

        Returns:
            Texto formatado (DDL simplificado + amostras) pronto para o prompt.
        """
        cache_key = f"{table_id}:{include_samples}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        table = self._config.get_table(table_id)
        if table.is_declarative:
            info = self._declarative_info(table)
        else:
            info = self._discovery_info(table, include_samples=include_samples)

        self._cache[cache_key] = info
        return info

    def get_schema_for(self, table_ids: list[str], include_samples: bool = True) -> str:
        """Retorna a descrição concatenada de schema para várias tabelas."""
        blocks = [self.get_table_info(tid, include_samples=include_samples) for tid in table_ids]
        return "\n\n".join(blocks)

    def get_column_index(self) -> dict[str, set[str]]:
        """Índice ``{table_id: {colunas}}`` para validação de intent.

        * Declarativo: nomes em ``TableConfig.columns``.
        * Discovery: ``inspect.get_columns``; falha de reflexão → set vazio
          (fail-closed para refs de coluna nessa tabela).
        """
        index: dict[str, set[str]] = {}
        for table in self._config.tables:
            cols = self.list_columns(table.id)
            index[table.id] = {c["name"] for c in cols}
        return index

    def list_columns(self, table_id: str) -> list[dict[str, str]]:
        """Lista colunas com nome e tipo (e description se declarativa).

        Returns:
            Lista de dicts ``{name, type, description?}``. Em falha de discovery,
            lista vazia (fail-closed).
        """
        table = self._config.get_table(table_id)
        if table.is_declarative:
            return [
                {
                    "name": col.name,
                    "type": col.type or "",
                    "description": col.description or "",
                }
                for col in table.columns
            ]

        engine = self._registry.get_inspection_engine(table.database)
        inspector = inspect(engine)
        try:
            raw = inspector.get_columns(table.name, schema=table.schema)
        except Exception as err:  # noqa: BLE001
            logger.warning("list_columns: falha ao refletir {!r}: {}", table.id, err)
            return []

        out: list[dict[str, str]] = []
        for col in raw:
            out.append(
                {
                    "name": col["name"],
                    "type": str(col.get("type") or ""),
                    "description": "",
                }
            )
        return out

    # ------------------------------------------------------------------ #
    # Modo declarativo
    # ------------------------------------------------------------------ #
    def _declarative_info(self, table: TableConfig) -> str:
        """Monta a descrição de schema a partir das colunas declaradas no YAML."""
        logger.debug("Schema declarativo para tabela {!r}", table.id)
        lines: list[str] = []
        lines.append(f"Tabela: {table.id}  (física: {table.qualified_name}, banco: {table.database})")
        if table.description:
            lines.append(f"  Descrição: {table.description}")
        if table.is_sharded:
            lines.append(
                f"  [SHARDADA] discriminador='{table.sharding.discriminator_column}' — "
                "use resolve_shard antes de consultar."
            )
        lines.append("Colunas:")
        for col in table.columns:
            type_part = f" {col.type}" if col.type else ""
            desc_part = f"  -- {col.description}" if col.description else ""
            lines.append(f"  - {col.name}{type_part}{desc_part}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Modo discovery
    # ------------------------------------------------------------------ #
    def _discovery_info(self, table: TableConfig, include_samples: bool) -> str:
        """Reflete o schema real da tabela no banco via SQLAlchemy inspect()."""
        logger.debug("Discovery de schema para tabela {!r}", table.id)
        engine = self._registry.get_inspection_engine(table.database)
        inspector = inspect(engine)

        try:
            columns = inspector.get_columns(table.name, schema=table.schema)
        except Exception as err:  # noqa: BLE001 - reflection pode falhar por permissão
            logger.warning("Falha ao refletir {!r}: {}", table.id, err)
            lines = [
                f"Tabela: {table.id} (física: {table.qualified_name}, banco: {table.database})",
            ]
            if table.description:
                lines.append(f"  Descrição: {table.description}")
            lines.append(f"  [schema indisponível: {err}]")
            return "\n".join(lines)

        lines: list[str] = []
        lines.append(
            f"Tabela: {table.id}  (física: {table.qualified_name}, banco: {table.database})"
        )
        if table.description:
            lines.append(f"  Descrição: {table.description}")
        if table.is_sharded:
            lines.append(
                f"  [SHARDADA] discriminador='{table.sharding.discriminator_column}' — "
                "use resolve_shard antes de consultar."
            )
        lines.append("Colunas:")
        for col in columns:
            nullable = "" if col.get("nullable", True) else " NOT NULL"
            lines.append(f"  - {col['name']} {col['type']}{nullable}")

        # chaves primárias
        try:
            pk = inspector.get_pk_constraint(table.name, schema=table.schema)
            pk_cols = pk.get("constrained_columns") or []
            if pk_cols:
                lines.append(f"Primary key: {', '.join(pk_cols)}")
        except Exception as err:  # noqa: BLE001
            logger.debug("PK indisponível para {!r}: {}", table.id, err)

        if include_samples and table.sample_rows > 0:
            samples = self._fetch_samples(table)
            if samples:
                lines.append(f"Amostra ({len(samples)} linha(s)):")
                for row in samples:
                    lines.append(f"  {row}")

        return "\n".join(lines)

    def _fetch_samples(self, table: TableConfig) -> list[dict[str, Any]]:
        """Busca algumas linhas de amostra respeitando o guardrail read-only."""
        engine = self._registry.get_inspection_engine(table.database)
        limit = table.sample_rows
        # usa TOP para T-SQL, LIMIT para os demais
        dialect = self._registry.dialect_of(table.database)
        if dialect == "tsql":
            sql = f"SELECT TOP {limit} * FROM {table.qualified_name}"
        else:
            sql = f"SELECT * FROM {table.qualified_name} LIMIT {limit}"
        try:
            with engine.connect() as conn:
                result = conn.execute(text(sql))
                cols = list(result.keys())
                rows = result.fetchall()
                truncated: list[dict[str, Any]] = []
                for row in rows:
                    d = {}
                    for c, v in zip(cols, row):
                        s = str(v)
                        if len(s) > self._config.max_string_length:
                            s = s[: self._config.max_string_length] + "…"
                        d[c] = s
                    truncated.append(d)
                return truncated
        except Exception as err:  # noqa: BLE001
            logger.warning("Falha ao buscar amostras de {!r}: {}", table.id, err)
            return []


__all__ = ["SchemaLoader"]
