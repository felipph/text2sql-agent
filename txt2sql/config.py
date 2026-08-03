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
        value_extractor: Opcional. Caminho dotted para
            ``(text: str) -> list[str]`` — fallback textual quando o IntentPlan
            omite o discriminador em ``filters``. Domínio fica no adapter do app.
    """

    discriminator_column: str
    resolver: str
    value_extractor: str | None = None

    def load_resolver(self) -> Callable[[str], ShardResult]:
        """Importa dinamicamente o callable resolver configurado.

        Returns:
            O callable ``(discriminator_value: str) -> ShardResult``.

        Raises:
            ValueError: Se o formato do path for inválido.
            ImportError: Se o módulo não puder ser importado.
            AttributeError: Se a função não existir no módulo.
        """
        return load_dotted_callable(self.resolver, label="resolver")

    def load_value_extractor(self) -> Callable[[str], list[str]] | None:
        """Importa o extractor textual opcional ``(text) -> list[str]``."""
        if not self.value_extractor:
            return None
        return load_dotted_callable(self.value_extractor, label="value_extractor")


def load_dotted_callable(path: str, *, label: str = "callable") -> Callable[..., Any]:
    """Importa um callable via caminho dotted ``modulo.sub:funcao``."""
    if ":" not in path:
        raise ValueError(
            f"{label} deve estar no formato 'modulo.sub:funcao', recebido: {path!r}"
        )
    module_path, func_name = path.split(":", 1)
    logger.debug("Importando {}: {}:{}", label, module_path, func_name)
    module = importlib.import_module(module_path)
    func = getattr(module, func_name)
    if not callable(func):
        raise TypeError(f"{label} {path!r} não é um callable")
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
        force_analytical: Se ``True``, obriga extract → DuckDB → análise.
            ``trigger: always`` é alias que força este flag para ``True``.
    """

    enabled: bool = False
    trigger: str = "aggregation"
    fetch_limit: int = 100_000
    force_analytical: bool = False

    _VALID_TRIGGERS = ("always", "aggregation", "order", "join")

    def __post_init__(self) -> None:
        if self.trigger not in self._VALID_TRIGGERS:
            raise ValueError(
                f"trigger inválido: {self.trigger!r}. Válidos: {self._VALID_TRIGGERS}"
            )
        if self.trigger == "always":
            self.force_analytical = True


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
    def requires_analytical(self) -> bool:
        """Indica se a tabela exige rota analítica via DuckDB."""
        return bool(self.duckdb and self.duckdb.enabled and self.duckdb.force_analytical)

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
        query_timeout: Timeout de execução SELECT em segundos; None herda o global do agente.
    """

    id: str
    connection_string: str | None = None
    connection_env: str | None = None
    read_only: bool = True
    connect_timeout: int = 10
    query_timeout: int | None = None

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
class ExportConfig:
    """Configuração de exportação CSV denormalizado.

    Attributes:
        enabled: Se ``True``, o grafo pode exportar CSV sob demanda.
        dir: Diretório em disco para gravar os arquivos.
        base_url: Prefixo HTTP para montar a URL de download (app serve os arquivos).
        ttl_seconds: Idade máxima dos arquivos (cleanup via ``cleanup_expired_exports``).
        delimiter: Separador de campos do CSV.
        max_rows: Teto de linhas no dump (truncamento + aviso).
    """

    enabled: bool = False
    dir: str = ""
    base_url: str = ""
    ttl_seconds: int = 86_400
    delimiter: str = ","
    max_rows: int = 500_000

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if not (self.dir or "").strip():
            raise ValueError("export.dir é obrigatório quando export.enabled=true")
        if not (self.base_url or "").strip():
            raise ValueError("export.base_url é obrigatório quando export.enabled=true")
        if self.ttl_seconds < 1:
            raise ValueError(f"export.ttl_seconds deve ser >= 1, recebido: {self.ttl_seconds}")
        if not (self.delimiter or "").strip():
            raise ValueError("export.delimiter não pode ser vazio")
        if self.max_rows < 1:
            raise ValueError(f"export.max_rows deve ser >= 1, recebido: {self.max_rows}")


@dataclass
class BudgetConfig:
    """Orçamentos do grafo (clarificação, refine, materialização, extracts)."""

    max_clarifications: int = 2
    max_refine: int = 3
    max_mat_loops: int = 3
    max_gate_visits: int = 2
    max_rows_per_extract: int = 500_000
    max_rows_materialized: int = 2_000_000

    def __post_init__(self) -> None:
        for name in (
            "max_clarifications",
            "max_refine",
            "max_mat_loops",
            "max_gate_visits",
            "max_rows_per_extract",
            "max_rows_materialized",
        ):
            value = int(getattr(self, name))
            if value < 1:
                raise ValueError(f"agent.budget.{name} deve ser >= 1, recebido: {value}")
            setattr(self, name, value)


@dataclass
class MessagesConfig:
    """Mensagens de UX configuráveis (defaults PT-BR genéricos)."""

    clarification_exhausted: str = (
        "Não consegui obter esclarecimentos suficientes para continuar. "
        "Reformule a pergunta com todos os detalhes necessários numa única mensagem."
    )
    export_disabled: str = "A exportação em CSV não está habilitada neste ambiente."
    export_no_data: str = (
        "Ainda não há dados reunidos para exportar. "
        "Faça primeiro a análise e, em seguida, peça o CSV da lista completa."
    )
    export_failed: str = (
        "Não foi possível gerar o CSV neste momento. Tente de novo em seguida."
    )
    export_download_hint: str = "Você pode baixar a lista completa aqui: {url}"
    export_truncated: str = (
        "A lista exportada está incompleta porque há um limite de linhas por "
        "exportação neste turno. Refine o recorte (valores de {discriminator}, "
        "período ou filtros) para obter o máximo possível."
    )
    partial_coverage: str = (
        "Para obter o máximo possível com essa limitação, refine a pergunta: "
        "delimite um conjunto menor de valores de {discriminator}, um período "
        "específico, ou peça um ranking top-N com recorte mais estreito."
    )
    answer_fallback_header: str = "Resultado da consulta:"


@dataclass
class PromptsConfig:
    """Trechos extras de prompt (inline no YAML)."""

    intent_extra: str = ""
    answer_rules: str = ""


DEFAULT_EXPORT_DETECT_KEYWORDS: tuple[str, ...] = (
    "exportar",
    "exporte",
    "baixar csv",
    "baixar planilha",
    "download csv",
    "gerar csv",
    "em csv",
    "para csv",
    "lista completa",
    "planilha",
    ".csv",
)

_REMOVED_AGENT_KEYS = {
    "top_k": "use agent.sample_rows (sample da resposta) e agent.query_max_rows (LIMIT do Policy Gate)",
    "max_pages": "removido — o grafo dual-path não pagina; ajuste agent.budget.*",
    "sample_rows_in_table_info": "use tables[].sample_rows",
}


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
        sample_rows: Linhas no sample de ``ExecutionResult`` (compact_result).
        query_max_rows: LIMIT injetado pelo Policy Gate quando a SQL não tem LIMIT.
        max_intent_retries: Retries de IntentPlan antes de clarificar.
        max_string_length: Truncamento de strings longas em amostras de schema.
        read_only: Flag global de somente-leitura.
        custom_section: Texto livre anexado ao final do system prompt SQL.
        dialect: Dialeto SQL principal (informado ao LLM e ao guardrail).
        max_shards: Máximo de shards físicos distintos no fan-in / resolve_routing.
        query_timeout: Timeout default de execução SELECT (segundos); 0 desliga.
        reuse_ttl_seconds: TTL de reuse do catálogo DuckDB (segundos).
        batch_size: Tamanho do ``fetchmany`` na materialização DuckDB.
        materialize_sample_rows: LIMIT do sample pós-materialize.
        budget: Orçamentos do grafo.
        messages: Mensagens de UX.
        prompts: Trechos extras de prompt.
        export_detect_keywords: Keywords de heurística de export (None = defaults).
        llm: Configuração do provider LLM.
        export: Exportação CSV sob demanda (opcional).
        override_connections: Overrides de connection string aplicados na carga.
    """

    databases: list[DatabaseConfig] = field(default_factory=list)
    tables: list[TableConfig] = field(default_factory=list)
    relationships: list[RelationshipConfig] = field(default_factory=list)
    glossary: list[GlossaryEntry] = field(default_factory=list)

    sample_rows: int = 20
    query_max_rows: int = 500_000
    max_intent_retries: int = 2
    max_string_length: int = 5000
    read_only: bool = True
    custom_section: str | None = None
    dialect: str | None = None
    max_shards: int = 20
    query_timeout: int = 30
    reuse_ttl_seconds: int = 1800
    batch_size: int = 5_000
    materialize_sample_rows: int = 5

    budget: BudgetConfig = field(default_factory=BudgetConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    export_detect_keywords: list[str] | None = None

    llm: LLMConfig = field(default_factory=LLMConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    override_connections: dict[str, str] = field(default_factory=dict)

    # -- índices auxiliares -------------------------------------------------
    def __post_init__(self) -> None:
        self._db_index: dict[str, DatabaseConfig] = {db.id: db for db in self.databases}
        self._table_index: dict[str, TableConfig] = {t.id: t for t in self.tables}
        self._validate()

    def _validate(self) -> None:
        """Valida integridade referencial da configuração."""
        for name in (
            "sample_rows",
            "query_max_rows",
            "max_intent_retries",
            "batch_size",
            "materialize_sample_rows",
        ):
            value = int(getattr(self, name))
            if value < 1:
                raise ValueError(f"agent.{name} deve ser >= 1, recebido: {value}")
        if self.max_shards < 1:
            raise ValueError(
                f"max_shards deve ser >= 1, recebido: {self.max_shards}"
            )
        if self.query_timeout < 0:
            raise ValueError(
                f"query_timeout deve ser >= 0, recebido: {self.query_timeout}"
            )
        for db in self.databases:
            if db.query_timeout is not None and db.query_timeout < 0:
                raise ValueError(
                    f"databases[{db.id!r}].query_timeout deve ser >= 0, "
                    f"recebido: {db.query_timeout}"
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

    def effective_query_timeout(self, database_id: str) -> int:
        """Timeout efetivo de execução (segundos) para um banco.

        Override por banco se definido; senão o global ``query_timeout``.
        ``0`` desliga o deadline.
        """
        db = self.get_database(database_id)
        if db.query_timeout is not None:
            return db.query_timeout
        return self.query_timeout

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
        value_extractor=raw.get("value_extractor"),
    )


def _parse_duckdb(raw: dict[str, Any] | None) -> DuckDBConfig | None:
    if not raw:
        return None
    trigger = raw.get("trigger", "aggregation")
    force = bool(raw.get("force_analytical", False))
    if trigger == "always":
        force = True
    return DuckDBConfig(
        enabled=bool(raw.get("enabled", False)),
        trigger=trigger,
        fetch_limit=int(raw.get("fetch_limit", 100_000)),
        force_analytical=force,
    )


def _parse_export_config(raw: dict[str, Any] | None) -> ExportConfig:
    if not raw:
        return ExportConfig()
    return ExportConfig(
        enabled=bool(raw.get("enabled", False)),
        dir=str(raw.get("dir") or ""),
        base_url=str(raw.get("base_url") or ""),
        ttl_seconds=int(raw.get("ttl_seconds", 86_400)),
        delimiter=str(raw.get("delimiter", ",")),
        max_rows=int(raw.get("max_rows", 500_000)),
    )


def _reject_removed_agent_keys(agent_raw: dict[str, Any]) -> None:
    found = [k for k in _REMOVED_AGENT_KEYS if k in agent_raw]
    if not found:
        return
    hints = "; ".join(f"{k} → {_REMOVED_AGENT_KEYS[k]}" for k in found)
    raise ValueError(
        f"Campos removidos em agent: {', '.join(found)}. Substitutos: {hints}"
    )


def _parse_budget_config(raw: dict[str, Any] | None) -> BudgetConfig:
    if not raw:
        return BudgetConfig()
    return BudgetConfig(
        max_clarifications=int(raw.get("max_clarifications", 2)),
        max_refine=int(raw.get("max_refine", 3)),
        max_mat_loops=int(raw.get("max_mat_loops", 3)),
        max_gate_visits=int(raw.get("max_gate_visits", 2)),
        max_rows_per_extract=int(raw.get("max_rows_per_extract", 500_000)),
        max_rows_materialized=int(raw.get("max_rows_materialized", 2_000_000)),
    )


def _parse_messages_config(raw: dict[str, Any] | None) -> MessagesConfig:
    base = MessagesConfig()
    if not raw:
        return base
    kwargs: dict[str, str] = {}
    for field_name in (
        "clarification_exhausted",
        "export_disabled",
        "export_no_data",
        "export_failed",
        "export_download_hint",
        "export_truncated",
        "partial_coverage",
        "answer_fallback_header",
    ):
        value = raw.get(field_name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            kwargs[field_name] = text
    return MessagesConfig(**{**base.__dict__, **kwargs}) if kwargs else base


def _parse_prompts_config(raw: dict[str, Any] | None) -> PromptsConfig:
    if not raw:
        return PromptsConfig()
    return PromptsConfig(
        intent_extra=str(raw.get("intent_extra") or ""),
        answer_rules=str(raw.get("answer_rules") or ""),
    )


def _parse_export_detect_keywords(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise TypeError("agent.export_detect_keywords deve ser uma lista de strings")
    return [str(x) for x in raw]


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
            query_timeout=(
                int(db["query_timeout"]) if db.get("query_timeout") is not None else None
            ),
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
    agent_raw: dict[str, Any] = raw.get("agent", {}) or {}
    if not isinstance(agent_raw, dict):
        raise TypeError("agent deve ser um mapeamento YAML")
    _reject_removed_agent_keys(agent_raw)

    # analytics (TTL / batch / sample pós-mat)
    analytics_raw: dict[str, Any] = raw.get("analytics") or {}
    if analytics_raw and not isinstance(analytics_raw, dict):
        raise TypeError("analytics deve ser um mapeamento YAML")
    reuse_ttl_seconds = int(analytics_raw.get("reuse_ttl_seconds", 1800))
    batch_size = int(analytics_raw.get("batch_size", 5_000))
    materialize_sample_rows = int(analytics_raw.get("materialize_sample_rows", 5))

    # llm params
    llm_raw: dict[str, Any] = raw.get("llm", {}) or {}
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
        sample_rows=int(agent_raw.get("sample_rows", 20)),
        query_max_rows=int(agent_raw.get("query_max_rows", 500_000)),
        max_intent_retries=int(agent_raw.get("max_intent_retries", 2)),
        max_string_length=int(agent_raw.get("max_string_length", 5000)),
        read_only=bool(agent_raw.get("read_only", True)),
        custom_section=raw.get("custom_section"),
        dialect=raw.get("dialect"),
        max_shards=int(agent_raw.get("max_shards", 20)),
        query_timeout=int(agent_raw.get("query_timeout", 30)),
        reuse_ttl_seconds=reuse_ttl_seconds,
        batch_size=batch_size,
        materialize_sample_rows=materialize_sample_rows,
        budget=_parse_budget_config(
            agent_raw.get("budget") if isinstance(agent_raw.get("budget"), dict) else None
        ),
        messages=_parse_messages_config(
            agent_raw.get("messages") if isinstance(agent_raw.get("messages"), dict) else None
        ),
        prompts=_parse_prompts_config(
            agent_raw.get("prompts") if isinstance(agent_raw.get("prompts"), dict) else None
        ),
        export_detect_keywords=_parse_export_detect_keywords(
            agent_raw.get("export_detect_keywords")
        ),
        llm=llm,
        export=_parse_export_config(
            agent_raw.get("export") if isinstance(agent_raw.get("export"), dict) else None
        ),
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
