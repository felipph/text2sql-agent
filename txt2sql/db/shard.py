"""Resolução determinística de shards + exposição como tool LangChain.

O :class:`ShardResolver` importa dinamicamente o callable configurado em
``sharding.resolver`` de cada tabela shardada, valida o :class:`ShardResult`
retornado (o ``database_id`` deve existir no registry) e expõe a operação como
uma ``StructuredTool`` (``resolve_shard``) para o toolkit do agente.

Regra de negócio central: **é proibido fan-out**. Se o discriminador não é
fornecido, o agente deve pedir ao usuário — nunca fazer broadcast.
"""

from __future__ import annotations

import json
from typing import Callable

from langchain_core.tools import StructuredTool
from loguru import logger
from pydantic import BaseModel, Field

from txt2sql.config import AgentConfig, ShardResult
from txt2sql.db.registry import DatabaseRegistry


class ResolveShardInput(BaseModel):
    """Argumentos do tool ``resolve_shard``."""

    table_id: str = Field(description="ID lógico da tabela shardada (ex.: 'recebiveis').")
    discriminator_value: str = Field(
        description=(
            "Valor do discriminador de shard. Obrigatório — nunca invente; "
            "se o usuário não forneceu, peça antes de chamar."
        )
    )


class ShardResolver:
    """Resolve o shard físico (banco + tabela) de uma tabela particionada.

    Args:
        config: Configuração do agente.
        registry: Registro de bancos (para validar o ``database_id`` resolvido).
    """

    def __init__(self, config: AgentConfig, registry: DatabaseRegistry) -> None:
        self._config = config
        self._registry = registry
        self._resolvers: dict[str, Callable[[str], ShardResult]] = {}
        self._load_resolvers()

    def _load_resolvers(self) -> None:
        """Importa dinamicamente os resolvers de todas as tabelas shardadas."""
        for table in self._config.sharded_tables:
            resolver = table.sharding.load_resolver()
            self._resolvers[table.id] = resolver
            logger.info(
                "Resolver de shard carregado para tabela {!r}: {}",
                table.id,
                table.sharding.resolver,
            )

    # ------------------------------------------------------------------ #
    # Resolução
    # ------------------------------------------------------------------ #
    def resolve(self, table_id: str, value: str) -> ShardResult:
        """Resolve o shard de uma tabela para um valor de discriminador.

        Args:
            table_id: ID lógico da tabela shardada.
            value: Valor do discriminador.

        Returns:
            O :class:`ShardResult` com ``database_id`` e ``table_name`` físicos.

        Raises:
            KeyError: Se a tabela não for shardada/registrada.
            ValueError: Se o resolver retornar um ``database_id`` inexistente
                ou um resultado de tipo inválido.
        """
        if table_id not in self._resolvers:
            raise KeyError(
                f"Tabela {table_id!r} não é shardada ou não tem resolver configurado."
            )
        if not value or not str(value).strip():
            raise ValueError(
                f"Discriminador vazio para tabela {table_id!r}. Peça o valor ao usuário; "
                "fan-out não é permitido."
            )

        result = self._resolvers[table_id](value)

        if not isinstance(result, ShardResult):
            raise ValueError(
                f"Resolver de {table_id!r} deve retornar ShardResult, "
                f"retornou {type(result).__name__}."
            )
        if not self._registry.has_database(result.database_id):
            raise ValueError(
                f"Resolver de {table_id!r} retornou database_id inexistente: "
                f"{result.database_id!r}."
            )

        logger.debug(
            "Shard resolvido: tabela={} valor={} -> banco={} tabela_fisica={}",
            table_id,
            value,
            result.database_id,
            result.table_name,
        )
        return result

    # ------------------------------------------------------------------ #
    # Tool LangChain
    # ------------------------------------------------------------------ #
    def build_tool(self, cache: dict[tuple[str, str], ShardResult] | None = None) -> StructuredTool:
        """Constrói a ``StructuredTool`` ``resolve_shard`` para o toolkit.

        Args:
            cache: Cache opcional ``{(table_id, value): ShardResult}`` do turno
                atual, preenchido a cada resolução para reuso/inspeção.

        Returns:
            A :class:`~langchain_core.tools.StructuredTool` pronta para o agente.
        """

        def _resolve_shard(table_id: str, discriminator_value: str) -> str:
            key = (table_id, discriminator_value)
            if cache is not None and key in cache:
                result = cache[key]
            else:
                result = self.resolve(table_id, discriminator_value)
                if cache is not None:
                    cache[key] = result
            return json.dumps(
                {"database_id": result.database_id, "table_name": result.table_name},
                ensure_ascii=False,
            )

        return StructuredTool.from_function(
            func=_resolve_shard,
            name="resolve_shard",
            description=(
                "Resolve o shard físico (banco de dados e nome real da tabela) de uma "
                "tabela particionada a partir do valor do discriminador. Chame ANTES de "
                "consultar qualquer tabela marcada como SHARDADA. NUNCA assuma o shard e "
                "NUNCA faça fan-out: se o discriminador não estiver disponível na pergunta, "
                "peça ao usuário. Retorna JSON {database_id, table_name}."
            ),
            args_schema=ResolveShardInput,
        )


__all__ = ["ShardResolver", "ResolveShardInput"]
