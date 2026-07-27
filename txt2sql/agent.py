"""Grafo LangGraph do agente Text-to-SQL (redesenhado).

Este módulo constrói o :class:`~langgraph.graph.state.CompiledStateGraph` que
orquestra o agente. As dependências (registry de bancos, loader de schema,
resolver de shard e a fábrica de sessões DuckDB) são injetadas nos *closures*
dos nós por :func:`build_agent`.

Fluxo de nós:

    START → init_turn → route_discovery
              ├─[schema não carregado]→ load_schema → generate_query
              └─[schema carregado]───────────────────→ generate_query
    generate_query
        ├─[sem tool_calls]───────────────────────────────────────→ END
        ├─[resolve_shard]→ run_resolve_shard ─────────────────────→ generate_query
        ├─[materialize_sharded_table]→ run_materialize_sharded ───→ generate_query
        ├─[sql_db_schema]→ get_schema ────────────────────────────→ generate_query
        └─[sql_db_query] → check_query → route_execution
                                ├─[duckdb]→ materialize_duckdb → run_duckdb_query → generate_query
                                └─[direto]───────────────────→ run_query ─────────→ generate_query
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import sqlglot
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.channels.untracked_value import UntrackedValue
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.graph.state import CompiledStateGraph
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import exc as sa_exc

from txt2sql.config import AgentConfig, ShardResult, TableConfig
from txt2sql.db.duckdb_layer import DuckDBSession, needs_duckdb
from txt2sql.db.multi_shard import materialize_sharded_values
from txt2sql.db.registry import DatabaseRegistry
from txt2sql.db.schema import SchemaLoader
from txt2sql.db.shard import ShardResolver
from txt2sql.guardrail import ReadOnlyViolationError, validate_sql
from txt2sql.llm import build_llm
from txt2sql.prompts import Txt2SqlPromptBuilder
from txt2sql.query_routing import analyze_table_refs, routing_rejection_reason


# --------------------------------------------------------------------------- #
# Estado do agente
# --------------------------------------------------------------------------- #
class AgentState(MessagesState):
    """Estado do grafo, estendendo o histórico de mensagens.

    Attributes:
        page_count: Número de queries de dados executadas no turno.
        schema_loaded: Indica se o schema já foi carregado neste fluxo.
        duckdb_session: Sessão DuckDB efêmera do turno (ou ``None``).
            Não é checkpointada (``UntrackedValue``) — não é msgpack-serializável
            e é recriada em ``init_turn`` a cada invoke.
        resolved_shards: Cache de shards resolvidos no turno.
        multi_materialized: Metadados de fan-in multi-shard por ``table_id``.
        pending_query: Query validada aguardando roteamento/execução.
    """

    page_count: int
    schema_loaded: bool
    duckdb_session: Annotated[DuckDBSession | None, UntrackedValue]
    resolved_shards: dict[tuple[str, str], ShardResult]
    multi_materialized: dict[str, dict[str, Any]]
    pending_query: dict[str, Any] | None


# --------------------------------------------------------------------------- #
# Schemas das tools expostas ao LLM
# --------------------------------------------------------------------------- #
class SqlSchemaInput(BaseModel):
    """Argumentos do tool ``sql_db_schema``."""

    table_names: str = Field(
        description="Lista de IDs lógicos de tabelas separados por vírgula (ex.: 'clientes,recebiveis')."
    )


class SqlQueryInput(BaseModel):
    """Argumentos do tool ``sql_db_query``."""

    query: str = Field(description="Uma única query SELECT a ser executada.")


class MaterializeShardedInput(BaseModel):
    """Argumentos do tool ``materialize_sharded_table``."""

    table_id: str = Field(description="ID lógico da tabela shardada (ex.: 'recebiveis').")
    discriminator_values: list[str] = Field(
        description=(
            "Lista com 2 ou mais valores do discriminador (ex.: CNPJs). Não use com 0 ou 1 valor."
        )
    )


def _noop(*args: Any, **kwargs: Any) -> str:  # pragma: no cover - nunca chamado
    """Placeholder: as tools são interceptadas pelos nós do grafo."""
    return ""


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _truncate(value: Any, max_len: int) -> str:
    """Converte um valor em string, truncando se exceder ``max_len``."""
    s = str(value)
    return s if len(s) <= max_len else s[:max_len] + "…"


def _rows_to_text(rows: list[dict[str, Any]], max_len: int, top_k: int) -> str:
    """Formata linhas de resultado como texto compacto para o LLM."""
    if not rows:
        return "[]  (nenhuma linha retornada)"
    limited = rows[:top_k]
    out = [{k: _truncate(v, max_len) for k, v in row.items()} for row in limited]
    suffix = "" if len(rows) <= top_k else f"\n... (+{len(rows) - top_k} linha(s) omitida(s))"
    return json.dumps(out, ensure_ascii=False, default=str) + suffix


def _answer_unhandled_tool_calls(message: AIMessage, handled_ids: set[str]) -> list[ToolMessage]:
    """Gera ToolMessages de erro para tool_calls não tratadas.

    Garante que todo ``tool_call`` do modelo receba uma resposta (requisito da
    API de chat), evitando erros de protocolo mesmo em chamadas simultâneas.
    """
    extra: list[ToolMessage] = []
    for tc in message.tool_calls:
        if tc["id"] not in handled_ids:
            extra.append(
                ToolMessage(
                    content=f"Ferramenta {tc['name']!r} não pôde ser processada neste passo.",
                    tool_call_id=tc["id"],
                )
            )
    return extra


def _last_ai_message(state: AgentState) -> AIMessage:
    """Retorna a última mensagem de IA do estado."""
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            return msg
    raise RuntimeError("Nenhuma AIMessage encontrada no estado.")


# --------------------------------------------------------------------------- #
# Construção do agente
# --------------------------------------------------------------------------- #
def build_agent(
    config: AgentConfig,
    checkpointer: Any | None = None,
) -> CompiledStateGraph:
    """Constrói e compila o grafo LangGraph do agente Text-to-SQL.

    Args:
        config: Configuração do agente.
        checkpointer: Checkpointer externo do LangGraph (opcional). A biblioteca
            NÃO gerencia checkpointer internamente — é responsabilidade do caller
            fornecer um (ex.: ``MemorySaver`` ou ``AsyncPostgresSaver``).

    Returns:
        O grafo compilado, pronto para ``invoke``/``stream``.
    """
    registry = DatabaseRegistry(config)
    schema_loader = SchemaLoader(config, registry)
    shard_resolver = ShardResolver(config, registry) if config.sharded_tables else None
    prompt_builder = Txt2SqlPromptBuilder(config)
    system_prompt = prompt_builder.build()

    llm = build_llm(config)

    # -- tools expostas ao LLM ------------------------------------------- #
    schema_tool = StructuredTool.from_function(
        func=_noop,
        name="sql_db_schema",
        description=(
            "Retorna o schema (colunas e amostras) das tabelas indicadas. "
            "Passe os IDs lógicos separados por vírgula."
        ),
        args_schema=SqlSchemaInput,
    )
    query_tool = StructuredTool.from_function(
        func=_noop,
        name="sql_db_query",
        description=(
            "Executa uma única query SELECT e retorna as linhas. A query é "
            "roteada internamente para o banco correto ou para a camada analítica."
        ),
        args_schema=SqlQueryInput,
    )
    tools: list[StructuredTool] = [schema_tool, query_tool]
    # a tool resolve_shard usa o cache do estado; recriada por turno abaixo.
    if shard_resolver is not None:
        tools.append(shard_resolver.build_tool(cache=None))

    multi_shard_tables = [t for t in config.sharded_tables if t.uses_duckdb]
    if multi_shard_tables and shard_resolver is not None:
        tools.append(
            StructuredTool.from_function(
                func=_noop,
                name="materialize_sharded_table",
                description=(
                    "Materializa 2+ discriminadores de uma tabela shardada numa "
                    "única tabela DuckDB (nome lógico). Use quando a análise "
                    "cruzar vários valores do discriminador. Depois consulte com "
                    "sql_db_query usando o nome lógico. NÃO use com 0 ou 1 valor."
                ),
                args_schema=MaterializeShardedInput,
            )
        )

    llm_with_tools = llm.bind_tools(tools)

    default_dialect = config.dialect

    # ------------------------------------------------------------------ #
    # Índice físico->tabela para roteamento de execução
    # ------------------------------------------------------------------ #
    def _build_physical_index(
        resolved_shards: dict[tuple[str, str], ShardResult],
        multi_materialized: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, tuple[TableConfig, str]]:
        """Mapa {nome_físico_lower: (TableConfig, database_id)}."""
        index: dict[str, tuple[TableConfig, str]] = {}
        for table in config.tables:
            if table.is_sharded:
                continue
            index[table.name.lower()] = (table, table.database)
            index[table.qualified_name.lower()] = (table, table.database)
        for (table_id, _value), shard in resolved_shards.items():
            table = config.get_table(table_id)
            index[shard.table_name.lower()] = (table, shard.database_id)
        for table_id in multi_materialized or {}:
            table = config.get_table(table_id)
            index[table_id.lower()] = (table, table.database)
            index[table.name.lower()] = (table, table.database)
        return index

    def _resolve_target(
        sql: str,
        resolved_shards: dict[tuple[str, str], ShardResult],
        multi_materialized: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, TableConfig | None, str | None]:
        """Determina (database_id, table_config, physical_name) alvo de uma query.

        Retorna o banco da primeira tabela reconhecida. ``table_config`` e
        ``physical_name`` referem-se a uma tabela que use DuckDB, se houver.
        """
        index = _build_physical_index(resolved_shards, multi_materialized)
        try:
            parsed = sqlglot.parse_one(sql, dialect=default_dialect)
        except Exception:  # noqa: BLE001
            parsed = None

        referenced: list[str] = []
        if parsed is not None:
            for tbl in parsed.find_all(sqlglot.exp.Table):
                referenced.append(tbl.name.lower())
                if tbl.db:
                    referenced.append(f"{tbl.db}.{tbl.name}".lower())

        database_id: str | None = None
        duck_table: TableConfig | None = None
        duck_physical: str | None = None
        multi = multi_materialized or {}
        for name in referenced:
            if name in index:
                table, db_id = index[name]
                if database_id is None:
                    database_id = db_id
                if table.uses_duckdb and duck_table is None:
                    duck_table = table
                    # nome lógico pós fan-in: sem reescrita físico→lógico
                    if table.id in multi or table.id.lower() == name:
                        duck_physical = None
                    else:
                        duck_physical = name
        if database_id is None:
            # fallback: primeiro banco declarado
            database_id = config.databases[0].id if config.databases else ""
        return database_id, duck_table, duck_physical

    # ------------------------------------------------------------------ #
    # Nós
    # ------------------------------------------------------------------ #
    def init_turn(state: AgentState) -> dict[str, Any]:
        """Inicializa o turno: zera contadores e cria a sessão DuckDB efêmera."""
        logger.info("init_turn: iniciando novo turno")
        # fecha sessão anterior remanescente, se houver
        prev = state.get("duckdb_session")
        if prev is not None:
            prev.close()
        session = DuckDBSession() if config.duckdb_tables else None
        return {
            "page_count": 0,
            "schema_loaded": False,
            "duckdb_session": session,
            "resolved_shards": {},
            "multi_materialized": {},
            "pending_query": None,
        }

    def load_schema(state: AgentState) -> dict[str, Any]:
        """Carrega o schema (declarativo ou discovery) e injeta no contexto."""
        logger.info("load_schema: carregando schema de todas as tabelas")
        table_ids = schema_loader.get_all_table_names()
        schema_text = schema_loader.get_schema_for(table_ids, include_samples=True)
        msg = SystemMessage(
            content=("Tabelas disponíveis (use os IDs lógicos abaixo):\n\n" + schema_text)
        )
        return {"messages": [msg], "schema_loaded": True}

    def generate_query(state: AgentState) -> dict[str, Any]:
        """Invoca o LLM (com tools) para produzir tool calls ou a resposta final."""
        logger.info("generate_query: invocando LLM (page_count={})", state.get("page_count", 0))
        messages = [SystemMessage(content=system_prompt), *state["messages"]]
        response = llm_with_tools.invoke(messages)
        return {"messages": [response]}

    def run_resolve_shard(state: AgentState) -> dict[str, Any]:
        """Executa as tool calls ``resolve_shard`` e devolve os resultados."""
        ai = _last_ai_message(state)
        resolved = dict(state.get("resolved_shards", {}))
        tool_messages: list[ToolMessage] = []
        handled: set[str] = set()

        for tc in ai.tool_calls:
            if tc["name"] != "resolve_shard":
                continue
            handled.add(tc["id"])
            args = tc["args"]
            table_id = args.get("table_id", "")
            value = args.get("discriminator_value", "")
            try:
                assert shard_resolver is not None
                result = shard_resolver.resolve(table_id, value)
                resolved[(table_id, value)] = result
                content = json.dumps(
                    {"database_id": result.database_id, "table_name": result.table_name},
                    ensure_ascii=False,
                )
            except Exception as err:  # noqa: BLE001
                logger.warning("resolve_shard falhou: {}", err)
                content = f"ERRO ao resolver shard: {err}"
            tool_messages.append(ToolMessage(content=content, tool_call_id=tc["id"]))

        tool_messages.extend(_answer_unhandled_tool_calls(ai, handled))
        return {"messages": tool_messages, "resolved_shards": resolved}

    def run_materialize_sharded(state: AgentState) -> dict[str, Any]:
        """Executa ``materialize_sharded_table`` (fan-in multi-shard no DuckDB)."""
        ai = _last_ai_message(state)
        multi = dict(state.get("multi_materialized") or {})
        resolved = dict(state.get("resolved_shards") or {})
        session = state.get("duckdb_session")
        if session is None:
            session = DuckDBSession()
        tool_messages: list[ToolMessage] = []
        handled: set[str] = set()

        for tc in ai.tool_calls:
            if tc["name"] != "materialize_sharded_table":
                continue
            handled.add(tc["id"])
            args = tc["args"]
            table_id = args.get("table_id", "")
            values = args.get("discriminator_values") or []
            try:
                if shard_resolver is None:
                    raise ValueError("Nenhuma tabela shardada configurada.")
                table = config.get_table(table_id)
                result = materialize_sharded_values(
                    table=table,
                    values=list(values),
                    max_discriminators=config.max_shard_discriminators,
                    resolver=shard_resolver,
                    registry=registry,
                    session=session,
                )
                for v in result.materialized_values:
                    shard = shard_resolver.resolve(table_id, v)
                    resolved[(table_id, v)] = shard
                multi[table_id] = {
                    "values": result.materialized_values,
                    "truncated": result.truncated,
                    "omitted_count": result.omitted_count,
                    "message": result.message,
                }
                content = json.dumps(result.to_dict(), ensure_ascii=False)
            except Exception as err:  # noqa: BLE001
                logger.warning("materialize_sharded_table falhou: {}", err)
                content = f"ERRO ao materializar shards: {err}"
            tool_messages.append(ToolMessage(content=content, tool_call_id=tc["id"]))

        tool_messages.extend(_answer_unhandled_tool_calls(ai, handled))
        return {
            "messages": tool_messages,
            "multi_materialized": multi,
            "resolved_shards": resolved,
            "duckdb_session": session,
        }

    def get_schema(state: AgentState) -> dict[str, Any]:
        """Executa as tool calls ``sql_db_schema`` e devolve o schema pedido."""
        ai = _last_ai_message(state)
        tool_messages: list[ToolMessage] = []
        handled: set[str] = set()

        for tc in ai.tool_calls:
            if tc["name"] != "sql_db_schema":
                continue
            handled.add(tc["id"])
            raw = tc["args"].get("table_names", "")
            ids = [x.strip() for x in raw.split(",") if x.strip()]
            blocks: list[str] = []
            for tid in ids:
                if config.try_get_table(tid) is None:
                    blocks.append(f"Tabela {tid!r} desconhecida.")
                else:
                    blocks.append(schema_loader.get_table_info(tid))
            content = "\n\n".join(blocks) if blocks else "Nenhuma tabela informada."
            tool_messages.append(ToolMessage(content=content, tool_call_id=tc["id"]))

        tool_messages.extend(_answer_unhandled_tool_calls(ai, handled))
        return {"messages": tool_messages}

    def check_query(state: AgentState) -> dict[str, Any]:
        """Valida a query do tool ``sql_db_query`` (guardrail fail-closed)."""
        ai = _last_ai_message(state)
        query_tc = next((tc for tc in ai.tool_calls if tc["name"] == "sql_db_query"), None)

        # responde qualquer tool call não-query com erro (mantém protocolo)
        extra = _answer_unhandled_tool_calls(ai, {query_tc["id"]} if query_tc else set())
        if query_tc is None:
            return {"messages": extra, "pending_query": None}

        sql = query_tc["args"].get("query", "")
        page_count = state.get("page_count", 0)
        if page_count >= config.max_pages:
            msg = ToolMessage(
                content=(
                    f"Limite de {config.max_pages} consultas por turno atingido. "
                    "Responda ao usuário com os dados já obtidos."
                ),
                tool_call_id=query_tc["id"],
            )
            return {"messages": [msg, *extra], "pending_query": None}

        allowed = [t.name for t in config.tables] + [
            s.table_name for s in state.get("resolved_shards", {}).values()
        ]
        multi = state.get("multi_materialized") or {}
        for tid in multi:
            allowed.append(tid)
            allowed.append(config.get_table(tid).name)
        try:
            validate_sql(sql, dialect=default_dialect, allowed_tables=allowed)
        except ReadOnlyViolationError as err:
            logger.warning("check_query: query rejeitada: {}", err)
            msg = ToolMessage(
                content=f"Query REJEITADA pelo guardrail: {err}. Corrija e tente novamente.",
                tool_call_id=query_tc["id"],
            )
            return {"messages": [msg, *extra], "pending_query": None}

        refs = analyze_table_refs(
            sql,
            config,
            state.get("resolved_shards", {}),
            multi,
            default_dialect,
        )
        routing_err = routing_rejection_reason(refs)
        if routing_err is not None:
            logger.warning("check_query: rejeitada por roteamento: {}", routing_err)
            msg = ToolMessage(
                content=f"Query REJEITADA pelo roteador: {routing_err}",
                tool_call_id=query_tc["id"],
            )
            return {"messages": [msg, *extra], "pending_query": None}

        database_id, duck_table, duck_physical = _resolve_target(
            sql, state.get("resolved_shards", {}), multi
        )
        use_duckdb = False
        if duck_table is not None:
            if duck_table.id in multi:
                use_duckdb = True
            else:
                use_duckdb = needs_duckdb(duck_table, sql)
        pending = {
            "sql": sql,
            "tool_call_id": query_tc["id"],
            "database_id": database_id,
            "use_duckdb": use_duckdb,
            "duck_table_id": duck_table.id if duck_table else None,
            "duck_physical": duck_physical,
        }
        logger.info("check_query: query aprovada (db={}, duckdb={})", database_id, use_duckdb)
        return {"messages": extra, "pending_query": pending}

    def run_query(state: AgentState) -> dict[str, Any]:
        """Executa a query diretamente no banco de origem."""
        pending = state["pending_query"]
        assert pending is not None
        sql = pending["sql"]
        database_id = pending["database_id"]
        logger.info("run_query: executando no banco {!r}", database_id)
        try:
            rows = registry.execute(database_id, sql)
            content = _rows_to_text(rows, config.max_string_length, config.top_k)
        except (sa_exc.SQLAlchemyError, ReadOnlyViolationError) as err:
            logger.warning("run_query: erro de execução: {}", err)
            content = f"ERRO ao executar a query: {err}"
        msg = ToolMessage(content=content, tool_call_id=pending["tool_call_id"])
        return {
            "messages": [msg],
            "page_count": state.get("page_count", 0) + 1,
            "pending_query": None,
        }

    def materialize_duckdb(state: AgentState) -> dict[str, Any]:
        """Materializa a tabela volumétrica no DuckDB a partir da origem."""
        pending = state["pending_query"]
        assert pending is not None
        session = state.get("duckdb_session")
        if session is None:
            logger.warning("materialize_duckdb: sessão ausente; criando sob demanda")
            session = DuckDBSession()

        table = config.get_table(pending["duck_table_id"])
        if session.is_materialized(table.id):
            logger.info("materialize_duckdb: {!r} já materializada; pulando", table.id)
            return {"duckdb_session": session}

        source_engine = registry.get_engine(pending["database_id"])
        physical = pending.get("duck_physical")
        try:
            session.materialize(
                table_config=table,
                source_engine=source_engine,
                physical_name=physical,
            )
        except Exception as err:  # noqa: BLE001
            logger.error("materialize_duckdb: falha ao materializar: {}", err)
            msg = ToolMessage(
                content=f"ERRO ao preparar a camada analítica: {err}",
                tool_call_id=pending["tool_call_id"],
            )
            return {"messages": [msg], "pending_query": None, "duckdb_session": session}
        return {"duckdb_session": session}

    def run_duckdb_query(state: AgentState) -> dict[str, Any]:
        """Executa a query analítica no DuckDB (reescrevendo nome físico→lógico)."""
        pending = state["pending_query"]
        assert pending is not None
        session = state["duckdb_session"]
        table = config.get_table(pending["duck_table_id"])
        physical = pending.get("duck_physical")

        rewritten = _rewrite_for_duckdb(pending["sql"], physical, table.id, default_dialect)
        logger.info("run_duckdb_query: executando query analítica no DuckDB")
        try:
            rows = session.execute(rewritten)
            content = _rows_to_text(rows, config.max_string_length, config.top_k)
            meta = (state.get("multi_materialized") or {}).get(table.id)
            if meta and meta.get("truncated"):
                content = f"{content}\n\nAVISO: {meta.get('message', '')}"
        except Exception as err:  # noqa: BLE001
            logger.warning("run_duckdb_query: erro: {}", err)
            content = f"ERRO ao executar a análise: {err}"
        msg = ToolMessage(content=content, tool_call_id=pending["tool_call_id"])
        return {
            "messages": [msg],
            "page_count": state.get("page_count", 0) + 1,
            "pending_query": None,
        }

    # ------------------------------------------------------------------ #
    # Roteadores (edges condicionais)
    # ------------------------------------------------------------------ #
    def route_discovery(state: AgentState) -> str:
        return "generate_query" if state.get("schema_loaded") else "load_schema"

    def route_after_generate(state: AgentState) -> str:
        ai = _last_ai_message(state)
        if not ai.tool_calls:
            return END
        names = {tc["name"] for tc in ai.tool_calls}
        if "materialize_sharded_table" in names:
            return "run_materialize_sharded"
        if "resolve_shard" in names:
            return "run_resolve_shard"
        if "sql_db_schema" in names:
            return "get_schema"
        if "sql_db_query" in names:
            return "check_query"
        # tool desconhecida — responde erro e volta a gerar
        return "run_resolve_shard" if shard_resolver else "check_query"

    def route_execution(state: AgentState) -> str:
        pending = state.get("pending_query")
        if pending is None:
            # query rejeitada/limite: volta a gerar para o LLM reagir
            return "generate_query"
        return "materialize_duckdb" if pending["use_duckdb"] else "run_query"

    # ------------------------------------------------------------------ #
    # Montagem do grafo
    # ------------------------------------------------------------------ #
    graph = StateGraph(AgentState)
    graph.add_node("init_turn", init_turn)
    graph.add_node("load_schema", load_schema)
    graph.add_node("generate_query", generate_query)
    graph.add_node("run_resolve_shard", run_resolve_shard)
    graph.add_node("run_materialize_sharded", run_materialize_sharded)
    graph.add_node("get_schema", get_schema)
    graph.add_node("check_query", check_query)
    graph.add_node("materialize_duckdb", materialize_duckdb)
    graph.add_node("run_duckdb_query", run_duckdb_query)
    graph.add_node("run_query", run_query)

    graph.add_edge(START, "init_turn")
    graph.add_conditional_edges("init_turn", route_discovery, ["load_schema", "generate_query"])
    graph.add_edge("load_schema", "generate_query")
    graph.add_conditional_edges(
        "generate_query",
        route_after_generate,
        [
            "run_resolve_shard",
            "run_materialize_sharded",
            "get_schema",
            "check_query",
            END,
        ],
    )
    graph.add_edge("run_resolve_shard", "generate_query")
    graph.add_edge("run_materialize_sharded", "generate_query")
    graph.add_edge("get_schema", "generate_query")
    graph.add_conditional_edges(
        "check_query",
        route_execution,
        ["materialize_duckdb", "run_query", "generate_query"],
    )
    graph.add_edge("materialize_duckdb", "run_duckdb_query")
    graph.add_edge("run_duckdb_query", "generate_query")
    graph.add_edge("run_query", "generate_query")

    compiled = graph.compile(checkpointer=checkpointer)
    logger.info("Agente Text-to-SQL compilado com sucesso.")
    return compiled


def _rewrite_for_duckdb(
    sql: str, physical_name: str | None, logical_name: str, dialect: str | None
) -> str:
    """Reescreve referências ao nome físico para o nome lógico (tabela DuckDB).

    Args:
        sql: Query original gerada pelo LLM.
        physical_name: Nome físico usado na query (pode ser ``schema.tabela``).
        logical_name: Nome lógico da tabela materializada no DuckDB.
        dialect: Dialeto de origem para o parse.

    Returns:
        A query reescrita no dialeto DuckDB.
    """
    if not physical_name:
        return sql
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:  # noqa: BLE001
        return sql

    target = physical_name.split(".")[-1].lower()

    def _transform(node: sqlglot.exp.Expression) -> sqlglot.exp.Expression:
        if isinstance(node, sqlglot.exp.Table) and node.name.lower() == target:
            return sqlglot.exp.Table(this=sqlglot.exp.to_identifier(logical_name))
        return node

    tree = tree.transform(_transform)
    return tree.sql(dialect="duckdb")


__all__ = ["AgentState", "build_agent"]
