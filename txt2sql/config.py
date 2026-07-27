"""Configuração declarativa da biblioteca txt2sql.

Este módulo define todos os dataclasses que descrevem um agente Text-to-SQL
(bancos de dados, tabelas, sharding, DuckDB, relacionamentos, glossário e
parâmetros gerais) além da função :func:`load_config`, que carrega e valida um
arquivo YAML transformando-o em um :class:`AgentConfig` pronto para uso.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger


# ---------------------------------------------------------------------------
# Resultado da resolução de shard (parte da API pública)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ShardResult:
    """Resultado determinístico da resolução de um shard.

    Attributes:
        database_id: Identificador do banco físico onde a tabela reside.
            Deve referenciar um ``databases[].id`` declarado na configuração.
        table_name: Nome físico real da tabela naquele banco (já com sufixo
            de partição, quando aplicável).
    """

    database_id: str
    table_name: str


# ---------------------------------------------------------------------------
# Dataclasses de configuração
# ---------------------------------------------------------------------------
@dataclass
class ColumnConfig:
    """Descrição declarativa de uma coluna de tabela.

    Attributes:
        name: Nome físico da coluna.
        type: Tipo SQL da coluna (opcional, apenas informativo para o LLM).
        description: Texto negocial explicando o que a coluna representa.
            É injetado no system prompt como contexto semântico.
    """

    name: str
    type: str | None = None
    description: str | None = None


@dataclass
class ShardingConfig:
    """Configuração de sharding determinístico de uma tabela.

    Attributes:
        discriminator_column: Coluna cujo valor determina o shard físico.
        resolver: Caminho dotted importável no formato ``modulo.sub:funcao``
            apontando para um callable ``(str) -> ShardResult``.
    """

    discriminator_column: str
    resolver: str

    def load_resolver(self) -> Callable[[str], ShardResult]:
        """Importa dinamicamente o callable resolver configurado.

        Returns:
            O callable ``(discriminator_value: str) -> ShardResult``.

        Raises:
            ValueError: Se o formato do path for inválido.
            ImportError: Se o módulo não puder ser importado.
            AttributeError: Se a função não existir no módulo.
        """
        if ":" not in self.resolver:
            raise ValueError(
                f"resolver deve estar no formato 'modulo.sub:funcao', recebido: {self.resolver!r}"
            )
        module_path, func_name = self.resolver.split(":", 1)
        logger.debug("Importando resolver de shard: {}:{}", module_path, func_name)
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
        if not callable(func):
            raise TypeError(f"resolver {self.resolver!r} não é um callable")
        return func


@dataclass
class DuckDBConfig:
    """Configuração da camada DuckDB intermediária para uma tabela volumétrica.

    Attributes:
        enabled: Se ``True``, a tabela pode ser materializada no DuckDB.
        trigger: Gatilho de materialização. Um de ``"always"``,
            ``"aggregation"``, ``"order"`` ou ``"join"``.
        fetch_limit: Número máximo de linhas buscadas do banco de origem ao
            materializar (default 100_000).
    """

    enabled: bool = False
    trigger: str = "aggregation"
    fetch_limit: int = 100_000

    _VALID_TRIGGERS = ("always", "aggregation", "order", "join")

    def __post_init__(self) -> None:
        if self.trigger not in self._VALID_TRIGGERS:
            raise ValueError(
                f"trigger inválido: {self.trigger!r}. Válidos: {self._VALID_TRIGGERS}"
            )


@dataclass
class TableConfig:
    """Configuração de uma tabela lógica conhecida pelo agente.

    Attributes:
        id: Identificador lógico usado em referências internas e pelo LLM.
        database: ID do banco (referencia ``databases[].id``) usado como
            default/fallback (discovery de schema usa este banco).
        schema: Schema/namespace da tabela no banco (opcional).
        name: Nome físico/lógico da tabela.
        description: Texto negocial explicando o que a tabela representa.
            Injetado no system prompt e na saída do SchemaLoader.
        columns: Colunas declaradas. Se vazio, o schema é descoberto via
            reflection (SQLAlchemy inspect).
        sharding: Configuração de sharding, se a tabela for particionada.
        duckdb: Configuração da camada DuckDB, se aplicável.
        sample_rows: Número de linhas de amostra a incluir no schema para o LLM.
    """

    id: str
    database: str
    name: str
    schema: str | None = None
    description: str | None = None
    columns: list[ColumnConfig] = field(default_factory=list)
    sharding: ShardingConfig | None = None
    duckdb: DuckDBConfig | None = None
    sample_rows: int = 3

    @property
    def is_declarative(self) -> bool:
        """Indica se o schema desta tabela é declarativo (colunas no YAML)."""
        return bool(self.columns)

    @property
    def is_sharded(self) -> bool:
        """Indica se a tabela é shardada."""
        return self.sharding is not None

    @property
    def uses_duckdb(self) -> bool:
        """Indica se a tabela pode usar a camada DuckDB."""
        return self.duckdb is not None and self.duckdb.enabled

    @property
    def qualified_name(self) -> str:
        """Nome qualificado ``schema.name`` (ou apenas ``name``)."""
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass
class DatabaseConfig:
    """Configuração de uma conexão de banco de dados disponível ao agente.

    Attributes:
        id: Identificador lógico do banco.
        connection_string: String de conexão SQLAlchemy explícita.
        connection_env: Nome da env var de onde ler a connection string.
            Usado quando ``connection_string`` não é fornecida.
        read_only: Se ``True``, instala guardrail read-only no engine.
        connect_timeout: Timeout de conexão em segundos.
    """

    id: str
    connection_string: str | None = None
    connection_env: str | None = None
    read_only: bool = True
    connect_timeout: int = 10

    def resolve_connection_string(
        self, override_connections: dict[str, str] | None = None
    ) -> str:
        """Resolve a connection string final deste banco.

        A precedência é: override explícito > connection_string > env var.

        Args:
            override_connections: Mapa opcional ``{database_id: connection_string}``
                que sobrescreve o que estiver no YAML.

        Returns:
            A connection string resolvida.

        Raises:
            ValueError: Se nenhuma fonte de connection string estiver disponível.
        """
        if override_connections and self.id in override_connections:
            return override_connections[self.id]
        if self.connection_string:
            return self.connection_string
        if self.connection_env:
            value = os.environ.get(self.connection_env)
            if not value:
                raise ValueError(
                    f"Banco {self.id!r}: env var {self.connection_env!r} não definida"
                )
            return value
        raise ValueError(
            f"Banco {self.id!r}: forneça 'connection_string', 'connection_env' "
            "ou um override_connections."
        )


@dataclass
class ColumnRef:
    """Referência a uma coluna de uma tabela (usada em relacionamentos)."""

    table: str
    column: str
    schema: str | None = None


@dataclass
class RelationshipConfig:
    """Relacionamento (foreign key lógica) entre duas tabelas.

    Attributes:
        from_ref: Coluna de origem.
        to_ref: Coluna de destino.
        description: Descrição negocial do relacionamento.
    """

    from_ref: ColumnRef
    to_ref: ColumnRef
    description: str | None = None


@dataclass
class GlossaryEntry:
    """Entrada de glossário de negócio.

    Attributes:
        term: Termo de negócio.
        definition: Definição do termo.
    """

    term: str
    definition: str


@dataclass
class LLMConfig:
    """Configuração do provider LLM (Azure OpenAI).

    Todos os campos são opcionais; quando ausentes, :func:`txt2sql.llm.build_llm`
    recorre às env vars padrão do Azure OpenAI.
    """

    deployment: str | None = None
    model: str | None = None
    api_version: str | None = None
    azure_endpoint: str | None = None
    api_key: str | None = None
    temperature: float = 0.0


@dataclass
class AgentConfig:
    """Configuração completa de um agente Text-to-SQL.

    Attributes:
        databases: Lista de conexões de banco disponíveis.
        tables: Tabelas lógicas conhecidas pelo agente.
        relationships: Relacionamentos entre tabelas.
        glossary: Glossário de negócio.
        top_k: Número default de linhas retornadas por query.
        max_pages: Máximo de queries (páginas) executadas por turno.
        max_string_length: Truncamento de strings longas em resultados.
        read_only: Flag global de somente-leitura.
        sample_rows_in_table_info: Linhas de amostra no schema por default.
        custom_section: Texto livre anexado ao final do system prompt.
        dialect: Dialeto SQL principal (informado ao LLM e ao guardrail).
        max_shard_discriminators: Máximo de discriminadores por chamada
            ``materialize_sharded_table`` (fan-in multi-shard).
        llm: Configuração do provider LLM.
        override_connections: Overrides de connection string aplicados na carga.
    """

    databases: list[DatabaseConfig] = field(default_factory=list)
    tables: list[TableConfig] = field(default_factory=list)
    relationships: list[RelationshipConfig] = field(default_factory=list)
    glossary: list[GlossaryEntry] = field(default_factory=list)

    top_k: int = 20
    max_pages: int = 10
    max_string_length: int = 5000
    read_only: bool = True
    sample_rows_in_table_info: int = 3
    custom_section: str | None = None
    dialect: str | None = None
    max_shard_discriminators: int = 20

    llm: LLMConfig = field(default_factory=LLMConfig)
    override_connections: dict[str, str] = field(default_factory=dict)

    # -- índices auxiliares -------------------------------------------------
    def __post_init__(self) -> None:
        self._db_index: dict[str, DatabaseConfig] = {db.id: db for db in self.databases}
        self._table_index: dict[str, TableConfig] = {t.id: t for t in self.tables}
        self._validate()

    def _validate(self) -> None:
        """Valida integridade referencial da configuração."""
        if self.max_shard_discriminators < 1:
            raise ValueError(
                f"max_shard_discriminators deve ser >= 1, recebido: {self.max_shard_discriminators}"
            )
        if len(self._db_index) != len(self.databases):
            raise ValueError("IDs de databases duplicados na configuração")
        if len(self._table_index) != len(self.tables):
            raise ValueError("IDs de tables duplicados na configuração")
        for table in self.tables:
            if table.database not in self._db_index:
                raise ValueError(
                    f"Tabela {table.id!r} referencia database inexistente: {table.database!r}"
                )

    def get_database(self, database_id: str) -> DatabaseConfig:
        """Retorna a configuração de um banco por ID."""
        if database_id not in self._db_index:
            raise KeyError(f"database_id desconhecido: {database_id!r}")
        return self._db_index[database_id]

    def get_table(self, table_id: str) -> TableConfig:
        """Retorna a configuração de uma tabela por ID lógico."""
        if table_id not in self._table_index:
            raise KeyError(f"table_id desconhecido: {table_id!r}")
        return self._table_index[table_id]

    def try_get_table(self, table_id: str) -> TableConfig | None:
        """Retorna a tabela por ID ou ``None`` se não existir."""
        return self._table_index.get(table_id)

    @property
    def sharded_tables(self) -> list[TableConfig]:
        """Lista das tabelas shardadas."""
        return [t for t in self.tables if t.is_sharded]

    @property
    def duckdb_tables(self) -> list[TableConfig]:
        """Lista das tabelas que usam a camada DuckDB."""
        return [t for t in self.tables if t.uses_duckdb]


# ---------------------------------------------------------------------------
# Parsing do YAML
# ---------------------------------------------------------------------------
def _parse_columns(raw: list[dict[str, Any]] | None) -> list[ColumnConfig]:
    if not raw:
        return []
    return [
        ColumnConfig(
            name=c["name"],
            type=c.get("type"),
            description=c.get("description"),
        )
        for c in raw
    ]


def _parse_sharding(raw: dict[str, Any] | None) -> ShardingConfig | None:
    if not raw:
        return None
    return ShardingConfig(
        discriminator_column=raw["discriminator_column"],
        resolver=raw["resolver"],
    )


def _parse_duckdb(raw: dict[str, Any] | None) -> DuckDBConfig | None:
    if not raw:
        return None
    return DuckDBConfig(
        enabled=bool(raw.get("enabled", False)),
        trigger=raw.get("trigger", "aggregation"),
        fetch_limit=int(raw.get("fetch_limit", 100_000)),
    )


def _parse_column_ref(raw: dict[str, Any]) -> ColumnRef:
    return ColumnRef(
        table=raw["table"],
        column=raw["column"],
        schema=raw.get("schema"),
    )


def load_config(
    path: str | Path,
    override_connections: dict[str, str] | None = None,
) -> AgentConfig:
    """Carrega e valida um arquivo YAML de configuração de agente.

    Args:
        path: Caminho para o arquivo YAML.
        override_connections: Mapa opcional ``{database_id: connection_string}``
            que sobrescreve as connection strings declaradas/env do YAML. Útil
            para injetar credenciais montadas em tempo de execução.

    Returns:
        Um :class:`AgentConfig` totalmente populado e validado.

    Raises:
        FileNotFoundError: Se o arquivo não existir.
        ValueError: Se a configuração for inconsistente.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de configuração não encontrado: {path}")

    logger.info("Carregando configuração de agente de {}", path)
    with path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    # databases
    databases = [
        DatabaseConfig(
            id=db["id"],
            connection_string=db.get("connection_string"),
            connection_env=db.get("connection_env"),
            read_only=bool(db.get("read_only", True)),
            connect_timeout=int(db.get("connect_timeout", 10)),
        )
        for db in raw.get("databases", [])
    ]

    # tables
    tables = [
        TableConfig(
            id=t["id"],
            database=t["database"],
            name=t["name"],
            schema=t.get("schema"),
            description=t.get("description"),
            columns=_parse_columns(t.get("columns")),
            sharding=_parse_sharding(t.get("sharding")),
            duckdb=_parse_duckdb(t.get("duckdb")),
            sample_rows=int(t.get("sample_rows", 3)),
        )
        for t in raw.get("tables", [])
    ]

    # relationships
    relationships = [
        RelationshipConfig(
            from_ref=_parse_column_ref(r["from"]),
            to_ref=_parse_column_ref(r["to"]),
            description=r.get("description"),
        )
        for r in raw.get("relationships", [])
    ]

    # glossary
    glossary = [
        GlossaryEntry(term=g["term"], definition=g["definition"])
        for g in raw.get("glossary", [])
    ]

    # agent params
    agent_raw: dict[str, Any] = raw.get("agent", {})

    # llm params
    llm_raw: dict[str, Any] = raw.get("llm", {})
    llm = LLMConfig(
        deployment=llm_raw.get("deployment"),
        model=llm_raw.get("model"),
        api_version=llm_raw.get("api_version"),
        azure_endpoint=llm_raw.get("azure_endpoint"),
        api_key=llm_raw.get("api_key"),
        temperature=float(llm_raw.get("temperature", 0.0)),
    )

    config = AgentConfig(
        databases=databases,
        tables=tables,
        relationships=relationships,
        glossary=glossary,
        top_k=int(agent_raw.get("top_k", 20)),
        max_pages=int(agent_raw.get("max_pages", 10)),
        max_string_length=int(agent_raw.get("max_string_length", 5000)),
        read_only=bool(agent_raw.get("read_only", True)),
        sample_rows_in_table_info=int(agent_raw.get("sample_rows_in_table_info", 3)),
        custom_section=raw.get("custom_section"),
        dialect=raw.get("dialect"),
        max_shard_discriminators=int(agent_raw.get("max_shard_discriminators", 20)),
        llm=llm,
        override_connections=override_connections or {},
    )

    logger.info(
        "Configuração carregada: {} banco(s), {} tabela(s), {} shardada(s), {} com DuckDB",
        len(config.databases),
        len(config.tables),
        len(config.sharded_tables),
        len(config.duckdb_tables),
    )
    return config
