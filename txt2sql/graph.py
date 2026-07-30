"""Grafo dual-path (simple | analytical) — MVP scaffold.

Topologia:

    START → init_state → interpret_intent → clarify | resolve_and_route
    resolve_and_route → simple: generate_sql → exec_source → verify → answer|generate_sql
                     → analytical: gate → plan_mat → materialize → check_mat
                       → gen_analytical_sql | plan_mat
                       → exec_duckdb → verify → answer|gen_analytical|gate
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.channels.untracked_value import UntrackedValue
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from loguru import logger
from pydantic import BaseModel

from txt2sql.artifacts import (
    Budget,
    DuckDBCatalog,
    DuckDBTableInfo,
    ExecutionResult,
    MaterializationPlan,
    MaterializationStep,
    ShardBinding,
    ShardRouting,
    SQLPlan,
    VerifyDecision,
)
from txt2sql.config import AgentConfig, TableConfig
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.db.fan_in import build_in_filter, fan_in
from txt2sql.db.registry import DatabaseRegistry, QueryTimeoutError
from txt2sql.db.schema import SchemaLoader
from txt2sql.db.session_store import DuckDBSessionStore
from txt2sql.intent import Clarification, IntentPlan, validate_intent
from txt2sql.llm import build_llm
from txt2sql.middleware import compact_result, result_from_rejection, result_from_timeout
from txt2sql.path_routing import route_execution
from txt2sql.policy import check_sql_plan
from txt2sql.prompts import Txt2SqlPromptBuilder
from txt2sql.query_routing import extract_table_names
from txt2sql.shard_routing import (
    ClarifyNeeded,
    ensure_discriminator_filters,
    resolve_routing,
)

MAX_INTENT_RETRIES = 2
CLARIFICATION_EXHAUSTED = (
    "Não consegui obter esclarecimentos suficientes para continuar. "
    "Reformule a pergunta com todos os detalhes necessários numa única mensagem."
)


class GateDecision(BaseModel):
    """Decisão do sufficiency_gate: reutilizar catálogo ou refresh."""

    action: Literal["reuse", "refresh"] = "refresh"


class MaterializationCheck(BaseModel):
    """Decisão pós-materialize: catálogo pronto para SQL analítico."""

    ready: bool = True
    reason: str = ""


class GraphState(MessagesState):
    """Estado do grafo dual-path."""

    intent_plan: dict[str, Any] | None
    intent_retries: int
    intent_route: str
    execution_path: str
    shard_routing: dict[str, Any] | None
    sql_plan: dict[str, Any] | None
    materialization_plan: dict[str, Any] | None
    last_result: dict[str, Any] | None
    executed_sql_history: list[str]
    verify_decision: dict[str, Any] | None
    duckdb_catalog: dict[str, Any]
    budget: dict[str, Any]
    gate_action: str
    mat_ready: bool
    partial: bool
    final_answer: str | None
    duckdb_session: Annotated[DuckDBSession | None, UntrackedValue]


def _dump_json(obj: Any, *, indent: int | None = None) -> str:
    if hasattr(obj, "model_dump"):
        payload = obj.model_dump(mode="json", by_alias=True)
    else:
        payload = obj
    return json.dumps(payload, ensure_ascii=False, indent=indent, default=str)


def _coerce_intent_plan(raw: Any) -> IntentPlan:
    if isinstance(raw, IntentPlan):
        return raw
    if isinstance(raw, dict):
        return IntentPlan.model_validate(raw)
    return IntentPlan.model_validate(raw)


def _budget(state: GraphState) -> Budget:
    raw = state.get("budget")
    return Budget.model_validate(raw) if raw else Budget()


def _shard_routing(state: GraphState) -> ShardRouting:
    raw = state.get("shard_routing")
    return ShardRouting.model_validate(raw) if raw else ShardRouting()


def _catalog(state: GraphState) -> DuckDBCatalog:
    raw = state.get("duckdb_catalog")
    return DuckDBCatalog.model_validate(raw) if raw else DuckDBCatalog()


def _intent_had_metrics(state: GraphState) -> bool:
    plan = state.get("intent_plan") or {}
    metrics = plan.get("metrics") or []
    return bool(metrics)


def _intent_table_ids(plan: IntentPlan) -> set[str]:
    """Tabelas lógicas tocadas pelo IntentPlan."""
    ids: set[str] = set()
    for f in plan.filters:
        ids.add(f.table_id)
    for m in plan.metrics:
        ids.add(m.table_id)
    for g in plan.group_by:
        ids.add(g.table_id)
    for j in plan.joins:
        ids.add(j.from_table_id)
        ids.add(j.to_table_id)
    for e in plan.entities:
        if e.table_id:
            ids.add(e.table_id)
    for o in plan.order_by:
        ids.add(o.table_id)
    return ids


def _table_ids_from_mat_plan(
    mat_plan: MaterializationPlan,
    agent_config: AgentConfig,
    dialect: str | None,
) -> set[str]:
    """Table ids citados em target_table / source_query do plano de materialização."""
    ids: set[str] = set()
    by_name: dict[str, str] = {}
    for table in agent_config.tables:
        by_name[table.id.lower()] = table.id
        by_name[table.name.lower()] = table.id
        by_name[table.qualified_name.lower()] = table.id

    for step in mat_plan.steps:
        target = (step.target_table or "").lower()
        if target in by_name:
            ids.add(by_name[target])
        for name in extract_table_names(step.source_query or "", dialect):
            if name in by_name:
                ids.add(by_name[name])
    return ids


def _catalog_covers_tables(
    catalog: DuckDBCatalog,
    table_ids: set[str],
    agent_config: AgentConfig,
) -> bool:
    """True se cada table_id do intent tem entrada no catálogo (id ou name)."""
    if not table_ids:
        return bool(catalog.tables)
    catalog_names = {t.name.lower() for t in catalog.tables}
    for tid in table_ids:
        names = {tid.lower()}
        table = agent_config.try_get_table(tid)
        if table is not None:
            names.add(table.name.lower())
            names.add(table.id.lower())
        if not names.intersection(catalog_names):
            return False
    return True


def _last_human_text(state: dict[str, Any]) -> str:
    """Conteúdo da última HumanMessage (para fallback de discriminador)."""
    for msg in reversed(state.get("messages") or []):
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, str):
                return content
            return str(content or "")
    return ""


def _resolve_step_table(
    step: MaterializationStep,
    *,
    shard: ShardRouting,
    intent: IntentPlan,
    agent_config: AgentConfig,
) -> TableConfig:
    """Resolve a :class:`TableConfig` lógica para um passo de materialização.

    O LLM pode inventar ``target_table`` (ex.: ``recebiveis_filtered_…``). O nome
    DuckDB efetivo é sempre ``table.id``; este helper só descobre *qual* tabela
    lógica materializar.

    Prioridade: binding explícito do step → ``target_table`` exato → prefixo do
    nome inventado → binding único do shard → único table_id do intent.
    """
    if step.shard_binding is not None:
        table = agent_config.try_get_table(step.shard_binding.table_id)
        if table is not None:
            return table

    table = agent_config.try_get_table(step.target_table)
    if table is not None:
        return table

    target_l = (step.target_table or "").lower()
    if target_l:
        for candidate in agent_config.tables:
            if target_l.startswith((candidate.id.lower(), candidate.name.lower())):
                return candidate

    if len(shard.bindings) == 1:
        table = agent_config.try_get_table(shard.bindings[0].table_id)
        if table is not None:
            return table

    intent_ids = sorted(_intent_table_ids(intent))
    if len(intent_ids) == 1:
        table = agent_config.try_get_table(intent_ids[0])
        if table is not None:
            return table

    known = ", ".join(sorted(t.id for t in agent_config.tables)) or "(nenhuma)"
    raise KeyError(
        f"Não foi possível mapear target_table={step.target_table!r} a um "
        f"table_id conhecido. IDs: {known}"
    )


def _bindings_for_table(
    table: TableConfig,
    shard: ShardRouting,
    step: MaterializationStep | None,
) -> list[ShardBinding]:
    """Bindings de shard para ``table`` — nunca usa binding de outra tabela."""
    if not table.is_sharded:
        return []
    if step is not None and step.shard_bindings:
        return [b for b in step.shard_bindings if b.table_id == table.id]
    matching = [b for b in shard.bindings if b.table_id == table.id]
    if matching:
        return matching
    if (
        step is not None
        and step.shard_binding is not None
        and step.shard_binding.table_id == table.id
    ):
        return [step.shard_binding]
    return []




def _append_provenance_footer(
    text: str,
    *,
    sql_history: list[str],
    assumptions: list[str],
    partial: bool,
    last_result: dict[str, Any],
) -> str:
    """Anexa bloco de proveniência se o LLM não incluiu '---'."""
    if "---" in text:
        return text
    lines: list[str] = []
    if sql_history:
        lines.append("SQL: " + "; ".join(sql_history))
    if assumptions:
        lines.append("Assunções: " + "; ".join(assumptions))
    lines.append(f"Parcial: {'sim' if partial else 'não'}")
    status = last_result.get("status") or ""
    warnings = last_result.get("warnings") or []
    if status:
        detail = status
        if warnings:
            detail += f" — {', '.join(str(w) for w in warnings)}"
        lines.append(f"Status: {detail}")
    elif warnings:
        lines.append(f"Avisos: {', '.join(str(w) for w in warnings)}")
    if not lines:
        return text
    return text.rstrip() + "\n\n---\n" + "\n".join(lines)


def _compact_from_state(
    state: GraphState,
    rows: list[dict[str, Any]],
    budget: Budget,
    *,
    session: DuckDBSession | None = None,
    expected_shape: str | None = None,
) -> ExecutionResult:
    return compact_result(
        rows,
        budget,
        session=session,
        expected_shape=expected_shape,  # type: ignore[arg-type]
        intent_had_metrics=_intent_had_metrics(state),
    )


def _resolve_database_id(
    config: AgentConfig,
    shard: ShardRouting,
    sql: str,
) -> str:
    if shard.mode == "single" and shard.bindings:
        return shard.bindings[0].database_id
    if config.databases:
        return config.databases[0].id
    return ""


def build_graph(
    config: AgentConfig,
    checkpointer: Any | None = None,
    *,
    session_store: DuckDBSessionStore | None = None,
) -> CompiledStateGraph:
    """Compila o grafo dual-path."""
    registry = DatabaseRegistry(config)
    schema_loader = SchemaLoader(config, registry)
    prompt_builder = Txt2SqlPromptBuilder(config)
    intent_prompt = prompt_builder.build_intent_prompt(schema_loader=schema_loader)
    has_checkpointer = checkpointer is not None

    if session_store is None:
        session_store = DuckDBSessionStore(Path(tempfile.mkdtemp(prefix="txt2sql_duckdb_")))

    llm = build_llm(config)
    # function_calling: schemas com Optionals/aninhados passam no Azure OpenAI;
    # json_schema estrito rejeita dict livre (ex. params) e alguns defaults.
    intent_llm = llm.with_structured_output(IntentPlan)
    sql_llm = llm.with_structured_output(SQLPlan, method="function_calling")
    mat_llm = llm.with_structured_output(MaterializationPlan, method="function_calling")
    mat_check_llm = llm.with_structured_output(
        MaterializationCheck, method="function_calling"
    )
    verify_llm = llm.with_structured_output(VerifyDecision, method="function_calling")
    gate_llm = llm.with_structured_output(GateDecision, method="function_calling")
    answer_llm = llm

    default_dialect = config.dialect

    def init_state(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        configurable = config.get("configurable") or {}
        thread_id = str(configurable.get("thread_id") or "default")
        session = session_store.get(thread_id)

        prev_catalog = state.get("duckdb_catalog")
        if prev_catalog:
            catalog_dump = prev_catalog
        else:
            catalog_dump = DuckDBCatalog().model_dump(by_alias=True)

        prev_budget = _budget(state)
        budget = Budget(
            total_rows_materialized=prev_budget.total_rows_materialized,
            clarification_count=0,
            max_clarifications=prev_budget.max_clarifications,
        )

        return {
            "intent_plan": None,
            "intent_retries": 0,
            "intent_route": "",
            "execution_path": "",
            "shard_routing": None,
            "sql_plan": None,
            "materialization_plan": None,
            "last_result": None,
            "executed_sql_history": [],
            "verify_decision": None,
            "duckdb_catalog": catalog_dump,
            "budget": budget.model_dump(),
            "gate_action": "",
            "mat_ready": False,
            "partial": False,
            "final_answer": None,
            "duckdb_session": session,
        }

    def interpret_intent(state: GraphState) -> dict[str, Any]:
        logger.info("interpret_intent: invocando LLM (retries={})", state.get("intent_retries", 0))
        messages = [SystemMessage(content=intent_prompt), *state["messages"]]
        plan: IntentPlan | None = None
        parse_error: str | None = None
        for attempt in range(2):
            try:
                plan = _coerce_intent_plan(intent_llm.invoke(messages))
                parse_error = None
                break
            except Exception as err:  # noqa: BLE001
                parse_error = str(err)
                logger.warning(
                    "interpret_intent: structured output falhou ({}/2): {}", attempt + 1, err
                )

        if plan is None:
            plan = IntentPlan(
                status="needs_clarification",
                question_rewrite="",
                clarification=Clarification(
                    question="Não entendi a pergunta. Pode reformular com mais detalhes?"
                ),
            )

        if plan.status == "needs_clarification":
            if plan.clarification is None or not plan.clarification.question.strip():
                plan = plan.model_copy(
                    update={
                        "clarification": Clarification(
                            question=(
                                "Pode esclarecer a pergunta? Faltam detalhes para mapear ao schema."
                            )
                        )
                    }
                )
            budget = _budget(state)
            if budget.exhausted("clarification_count"):
                return {
                    "intent_plan": plan.model_dump(),
                    "intent_route": "finish",
                    "final_answer": CLARIFICATION_EXHAUSTED,
                    "messages": [AIMessage(content=CLARIFICATION_EXHAUSTED)],
                }
            return {
                "intent_plan": plan.model_dump(),
                "intent_route": "ask_clarification",
            }

        validation = validate_intent(plan, schema_loader.get_column_index())
        if validation.ok:
            return {
                "intent_plan": plan.model_dump(),
                "intent_route": "resolve_and_route",
            }

        retries = int(state.get("intent_retries", 0)) + 1
        errors_txt = "; ".join(validation.errors) or (parse_error or "plan inválido")
        if retries >= MAX_INTENT_RETRIES:
            clarify = IntentPlan(
                status="needs_clarification",
                question_rewrite=plan.question_rewrite,
                clarification=Clarification(
                    question=(
                        "Não consegui mapear a pergunta ao schema. "
                        f"Problemas: {errors_txt}. Pode reformular ou esclarecer?"
                    )
                ),
            )
            return {
                "intent_plan": clarify.model_dump(),
                "intent_retries": retries,
                "intent_route": "ask_clarification",
            }

        feedback = SystemMessage(
            content=(
                "O IntentPlan anterior é inválido em relação ao schema. "
                f"Corrija e tente de novo. Erros: {errors_txt}"
            )
        )
        return {
            "messages": [feedback],
            "intent_plan": plan.model_dump(),
            "intent_retries": retries,
            "intent_route": "interpret_intent",
        }

    def ask_clarification(state: GraphState) -> dict[str, Any]:
        plan_raw = state.get("intent_plan") or {}
        clarification = plan_raw.get("clarification") or {}
        question = clarification.get("question") or "Pode esclarecer a pergunta?"
        options = clarification.get("options") or []
        logger.info("ask_clarification: {}", question[:120])

        budget = _budget(state)
        if budget.exhausted("clarification_count"):
            return {
                "intent_route": "finish",
                "final_answer": CLARIFICATION_EXHAUSTED,
                "messages": [AIMessage(content=CLARIFICATION_EXHAUSTED)],
            }

        budget = budget.model_copy(
            update={"clarification_count": budget.clarification_count + 1}
        )

        if has_checkpointer:
            answer = interrupt(
                {
                    "type": "clarification",
                    "question": question,
                    "options": options,
                }
            )
            return {
                "messages": [HumanMessage(content=str(answer))],
                "budget": budget.model_dump(),
                "final_answer": None,
            }

        text = question
        if options:
            text = question + "\nOpções: " + ", ".join(str(o) for o in options)
        return {
            "messages": [AIMessage(content=text)],
            "budget": budget.model_dump(),
            "final_answer": None,
        }

    def finish(state: GraphState) -> dict[str, Any]:
        """Encerra com ``final_answer`` já definido (ex.: clarificação esgotada)."""
        text = state.get("final_answer") or CLARIFICATION_EXHAUSTED
        return {"final_answer": text, "messages": [AIMessage(content=text)]}

    def resolve_and_route(state: GraphState) -> dict[str, Any]:
        plan = _coerce_intent_plan(state.get("intent_plan"))
        routing_result = resolve_routing(
            plan, config, extra_text=_last_human_text(state)
        )
        if isinstance(routing_result, ClarifyNeeded):
            clarify = plan.model_copy(
                update={
                    "status": "needs_clarification",
                    "clarification": Clarification(question=routing_result.question),
                }
            )
            budget = _budget(state)
            if budget.exhausted("clarification_count"):
                return {
                    "intent_plan": clarify.model_dump(),
                    "intent_route": "finish",
                    "final_answer": CLARIFICATION_EXHAUSTED,
                    "messages": [AIMessage(content=CLARIFICATION_EXHAUSTED)],
                }
            return {
                "intent_plan": clarify.model_dump(),
                "intent_route": "ask_clarification",
            }
        plan = ensure_discriminator_filters(plan, routing_result, config)
        path = route_execution(plan, routing_result, config)
        logger.info("resolve_and_route: path={} shard_mode={}", path, routing_result.mode)
        return {
            "intent_plan": plan.model_dump(),
            "shard_routing": routing_result.model_dump(),
            "execution_path": path,
            "intent_route": path,
        }

    def generate_sql(state: GraphState) -> dict[str, Any]:
        plan = _coerce_intent_plan(state.get("intent_plan"))
        shard = _shard_routing(state)
        context = (
            "Gere um SQLPlan (dialect=postgres) para executar na origem. "
            f"IntentPlan:\n{_dump_json(plan, indent=2)}\n"
            f"ShardRouting:\n{_dump_json(shard, indent=2)}"
        )
        last = state.get("last_result") or {}
        if last.get("status") == "rejected":
            context += f"\nErro anterior: {last.get('error')}"
        sql_plan = sql_llm.invoke([SystemMessage(content=context), *state["messages"]])
        if isinstance(sql_plan, dict):
            sql_plan = SQLPlan.model_validate(sql_plan)
        return {"sql_plan": sql_plan.model_dump()}

    def exec_source(state: GraphState) -> dict[str, Any]:
        sql_plan = SQLPlan.model_validate(state["sql_plan"])
        shard = _shard_routing(state)
        budget = _budget(state)
        decision = check_sql_plan(
            sql_plan,
            config=config,
            shard_routing=shard,
            path="simple",
            context="query",
            dialect=default_dialect,
        )
        if decision.status == "rejected":
            return {
                "last_result": result_from_rejection(decision.error or "rejeitado").model_dump(
                    by_alias=True
                )
            }

        database_id = _resolve_database_id(config, shard, decision.sql)
        try:
            rows = registry.execute(database_id, decision.sql)
        except QueryTimeoutError as err:
            return {"last_result": result_from_timeout(str(err)).model_dump(by_alias=True)}
        except Exception as err:  # noqa: BLE001
            return {
                "last_result": ExecutionResult(status="error", error=str(err)).model_dump(
                    by_alias=True
                ),
            }

        result = _compact_from_state(
            state,
            rows,
            budget,
            expected_shape=sql_plan.expected_shape,
        )
        history = list(state.get("executed_sql_history") or [])
        history.append(decision.sql)
        return {
            "last_result": result.model_dump(by_alias=True),
            "executed_sql_history": history,
        }

    def sufficiency_gate(state: GraphState) -> dict[str, Any]:
        catalog = _catalog(state)
        budget = _budget(state)
        if budget.exhausted("gate_visits"):
            return {"gate_action": "refresh", "budget": budget.model_dump()}
        plan = _coerce_intent_plan(state.get("intent_plan"))
        context = (
            "Decida se o DuckDBCatalog cobre o IntentPlan (reuse) ou precisa refresh.\n"
            f"IntentPlan:\n{_dump_json(plan)}\n"
            f"Catalog:\n{_dump_json(catalog)}"
        )
        gate = gate_llm.invoke([SystemMessage(content=context)])
        if isinstance(gate, dict):
            gate = GateDecision.model_validate(gate)
        elif not isinstance(gate, GateDecision):
            gate = GateDecision(action=getattr(gate, "action", "refresh"))
        return {
            "gate_action": gate.action,
            "budget": budget.model_copy(
                update={"gate_visits": budget.gate_visits + 1}
            ).model_dump(),
        }

    def plan_materialization(state: GraphState) -> dict[str, Any]:
        plan = _coerce_intent_plan(state.get("intent_plan"))
        shard = _shard_routing(state)
        logical_ids = sorted(_intent_table_ids(plan)) or [t.id for t in config.tables]
        context = (
            "Gere MaterializationPlan com extracts filtrados (sem agregação pesada na origem).\n"
            "IMPORTANTE: target_table DEVE ser exatamente um table_id lógico da config "
            f"({logical_ids}). Não invente nomes como 'recebiveis_filtered_…'.\n"
            f"IntentPlan:\n{_dump_json(plan, indent=2)}\n"
            f"ShardRouting:\n{_dump_json(shard, indent=2)}"
        )
        mat = mat_llm.invoke([SystemMessage(content=context)])
        if isinstance(mat, MaterializationPlan):
            pass
        elif isinstance(mat, dict):
            mat = MaterializationPlan.model_validate(mat)
        else:
            mat = MaterializationPlan.model_validate(mat)
        return {"materialization_plan": mat.model_dump()}

    def materialize(state: GraphState) -> dict[str, Any]:
        mat_plan = MaterializationPlan.model_validate(state["materialization_plan"])
        shard = _shard_routing(state)
        budget = _budget(state)
        catalog = _catalog(state)
        session = state.get("duckdb_session") or DuckDBSession()
        total_rows = 0
        last_rows: list[dict[str, Any]] = []
        intent = _coerce_intent_plan(state.get("intent_plan"))
        source_queries_by_table: dict[str, list[str]] = {}

        def _upsert_catalog(logical_name: str, rows: int, queries: list[str]) -> None:
            nonlocal catalog
            info = DuckDBTableInfo(
                name=logical_name,
                row_count=rows,
                source_queries=queries,
                shard_bindings=[
                    b for b in shard.bindings if b.table_id == logical_name
                ],
                materialized_at=datetime.now(UTC),
            )
            catalog = DuckDBCatalog(
                tables=[t for t in catalog.tables if t.name != logical_name] + [info]
            )

        def _materialize_one(
            table: TableConfig,
            step: MaterializationStep | None,
        ) -> str | None:
            """Materializa uma tabela. Retorna mensagem de erro ou None."""
            nonlocal total_rows, last_rows
            logical_name = table.id
            bindings = _bindings_for_table(table, shard, step)
            queries = (
                [step.source_query]
                if step is not None and step.source_query
                else source_queries_by_table.get(logical_name, [])
            )

            if table.is_sharded and len(bindings) >= 2:
                result = fan_in(
                    session=session,
                    table=table,
                    registry=registry,
                    bindings=bindings,
                )
                total_rows = result.row_count
                last_rows = session.execute(f'SELECT * FROM "{logical_name}" LIMIT 5')
                source_queries_by_table[logical_name] = queries or [
                    f"fan-in:{len(bindings)} bindings"
                ]
                _upsert_catalog(
                    logical_name, total_rows, source_queries_by_table[logical_name]
                )
                return None

            if table.is_sharded and len(bindings) == 1:
                binding = bindings[0]
                source_engine = registry.get_engine(binding.database_id)
                disc_col = table.sharding.discriminator_column if table.sharding else None
                filt = (
                    build_in_filter(disc_col, [binding.discriminator_value])
                    if disc_col
                    else None
                )
                session.materialize(
                    table,
                    source_engine,
                    physical_name=binding.physical_table,
                    filter_sql=filt,
                    replace=True,
                )
                last_rows = session.execute(f'SELECT * FROM "{logical_name}" LIMIT 5')
                count_rows = session.execute(
                    f'SELECT COUNT(*) AS n FROM "{logical_name}"'
                )
                total_rows = int(count_rows[0]["n"]) if count_rows else len(last_rows)
                source_queries_by_table[logical_name] = queries or [
                    f"SELECT * FROM {binding.physical_table}"
                ]
                _upsert_catalog(
                    logical_name, total_rows, source_queries_by_table[logical_name]
                )
                return None

            # Não-shardada (ou shardada sem binding — extract via SQL do plano)
            if step is not None and step.source_query.strip():
                extract_sql = step.source_query
            else:
                extract_sql = f"SELECT * FROM {table.qualified_name}"

            extract_plan = SQLPlan(
                sql=extract_sql,
                dialect=default_dialect or "postgres",
            )
            decision = check_sql_plan(
                extract_plan,
                config=config,
                shard_routing=shard,
                path="analytical",
                context="source_extract",
                dialect=default_dialect,
                max_rows=budget.max_rows_per_extract,
            )
            if decision.status == "rejected":
                return decision.error or "rejeitado"
            db_id = table.database
            try:
                rows = registry.execute(db_id, decision.sql)
            except QueryTimeoutError as err:
                return str(err)
            last_rows = rows
            total_rows = len(rows)
            _load_rows_into_duckdb(session, logical_name, rows, replace=True)
            source_queries_by_table[logical_name] = [decision.sql]
            _upsert_catalog(logical_name, total_rows, [decision.sql])
            return None

        planned_ids: list[str] = []
        for step in mat_plan.steps:
            try:
                table = _resolve_step_table(
                    step, shard=shard, intent=intent, agent_config=config
                )
            except KeyError as err:
                return {
                    "duckdb_session": session,
                    "last_result": result_from_rejection(str(err)).model_dump(
                        by_alias=True
                    ),
                }
            err = _materialize_one(table, step)
            if err is not None:
                if "timeout" in err.lower():
                    payload = result_from_timeout(err)
                else:
                    payload = result_from_rejection(err)
                return {
                    "duckdb_session": session,
                    "last_result": payload.model_dump(by_alias=True),
                }
            planned_ids.append(table.id)

        # Completa tabelas do intent / plano ausentes (ex.: JOIN com clientes)
        needed = _intent_table_ids(intent) | _table_ids_from_mat_plan(
            mat_plan, config, default_dialect
        )
        for tid in sorted(needed):
            if tid in planned_ids:
                continue
            if any(t.name == tid for t in catalog.tables):
                continue
            table = config.try_get_table(tid)
            if table is None or not table.uses_duckdb:
                continue
            err = _materialize_one(table, step=None)
            if err is not None:
                if "timeout" in err.lower():
                    payload = result_from_timeout(err)
                else:
                    payload = result_from_rejection(err)
                return {
                    "duckdb_session": session,
                    "last_result": payload.model_dump(by_alias=True),
                }

        mat_budget = budget.model_copy(update={"mat_loop_count": budget.mat_loop_count + 1})
        result = _compact_from_state(state, last_rows, mat_budget, session=session)
        return {
            "duckdb_session": session,
            "duckdb_catalog": catalog.model_dump(by_alias=True),
            "budget": mat_budget.model_dump(),
            "last_result": result.model_dump(by_alias=True),
        }

    def check_materialization(state: GraphState) -> dict[str, Any]:
        budget = _budget(state)
        if budget.exhausted("mat_loop_count"):
            return {"mat_ready": True, "partial": True}

        last = state.get("last_result") or {}
        last_status = last.get("status", "ok")
        if last_status in {"rejected", "timeout", "error"}:
            return {"mat_ready": False, "partial": False}

        plan = _coerce_intent_plan(state.get("intent_plan"))
        catalog = _catalog(state)
        table_ids = _intent_table_ids(plan)

        if _catalog_covers_tables(catalog, table_ids, config):
            return {"mat_ready": True, "partial": False}

        context = (
            "Avalie se o DuckDBCatalog cobre o IntentPlan para gerar SQL analítico.\n"
            f"IntentPlan:\n{_dump_json(plan, indent=2)}\n"
            f"Catalog:\n{_dump_json(catalog, indent=2)}"
        )
        decision = mat_check_llm.invoke([SystemMessage(content=context)])
        if isinstance(decision, dict):
            decision = MaterializationCheck.model_validate(decision)
        elif not isinstance(decision, MaterializationCheck):
            decision = MaterializationCheck(
                ready=bool(getattr(decision, "ready", True)),
                reason=str(getattr(decision, "reason", "")),
            )
        return {"mat_ready": decision.ready, "partial": False}

    def generate_analytical_sql(state: GraphState) -> dict[str, Any]:
        plan = _coerce_intent_plan(state.get("intent_plan"))
        catalog = _catalog(state)
        catalog_names = [t.name for t in catalog.tables] if catalog.tables else []
        context = (
            "Gere SQLPlan com dialect=duckdb.\n"
            "REGRAS OBRIGATÓRIAS:\n"
            f"- Use APENAS nomes lógicos do catálogo: {catalog_names}.\n"
            "- NUNCA use nomes físicos de shard (ex.: recebiveis_654, recebiveis_747).\n"
            "- O fan-in multi-shard já unificou os shards no nome lógico "
            "(ex.: `recebiveis`); não faça UNION de tabelas físicas.\n"
            "- JOINs entre tabelas do catálogo são válidos (tudo no DuckDB).\n"
            f"IntentPlan:\n{_dump_json(plan, indent=2)}\n"
            f"Catalog:\n{_dump_json(catalog)}"
        )
        last = state.get("last_result") or {}
        if last.get("status") == "rejected":
            context += f"\nErro anterior: {last.get('error')}"
        sql_plan = sql_llm.invoke([SystemMessage(content=context)])
        if isinstance(sql_plan, dict):
            sql_plan = SQLPlan.model_validate(sql_plan)
        return {"sql_plan": sql_plan.model_dump()}

    def exec_duckdb(state: GraphState) -> dict[str, Any]:
        sql_plan = SQLPlan.model_validate(state["sql_plan"])
        budget = _budget(state)
        shard = _shard_routing(state)
        session = state.get("duckdb_session")
        if session is None:
            return {
                "last_result": ExecutionResult(
                    status="error", error="Sessão DuckDB ausente"
                ).model_dump(by_alias=True),
            }

        decision = check_sql_plan(
            sql_plan,
            config=config,
            shard_routing=shard,
            path="analytical",
            context="query",
            dialect="duckdb",
            duckdb_catalog=_catalog(state),
        )
        if decision.status == "rejected":
            return {
                "last_result": result_from_rejection(decision.error or "rejeitado").model_dump(
                    by_alias=True
                )
            }

        try:
            rows = session.execute(decision.sql)
        except QueryTimeoutError as err:
            return {"last_result": result_from_timeout(str(err)).model_dump(by_alias=True)}
        except Exception as err:  # noqa: BLE001
            return {
                "last_result": ExecutionResult(status="error", error=str(err)).model_dump(
                    by_alias=True
                ),
            }

        result = _compact_from_state(
            state,
            rows,
            budget,
            session=session,
            expected_shape=sql_plan.expected_shape,
        )
        history = list(state.get("executed_sql_history") or [])
        history.append(decision.sql)
        return {
            "last_result": result.model_dump(by_alias=True),
            "executed_sql_history": history,
        }

    def verify(state: GraphState) -> dict[str, Any]:
        plan = _coerce_intent_plan(state.get("intent_plan"))
        last = state.get("last_result") or {}
        context = (
            "Avalie last_result vs IntentPlan. Retorne VerifyDecision.\n"
            "Se last_result.status for rejected/error/timeout por SQL inválido "
            "(ex. nomes físicos de shard no DuckDB), prefira refine_sql.\n"
            f"IntentPlan:\n{_dump_json(plan)}\n"
            f"last_result:\n{json.dumps(last, ensure_ascii=False, default=str)}"
        )
        decision = verify_llm.invoke([SystemMessage(content=context)])
        if isinstance(decision, dict):
            decision = VerifyDecision.model_validate(decision)
        budget = _budget(state)
        last_status = last.get("status") or ""
        # Não “responder o erro” no path analítico se ainda há budget para corrigir SQL
        path = state.get("execution_path") or "simple"
        if (
            path == "analytical"
            and decision.action == "answer"
            and last_status in {"rejected", "error", "timeout"}
            and not budget.exhausted("refine_count")
        ):
            decision = VerifyDecision(
                action="refine_sql",
                reason=(
                    decision.reason
                    or f"last_result.status={last_status}; tentando corrigir o SQL."
                ),
            )
        if decision.action == "refine_sql":
            budget = budget.model_copy(update={"refine_count": budget.refine_count + 1})
        return {
            "verify_decision": decision.model_dump(),
            "budget": budget.model_dump(),
        }

    def answer(state: GraphState) -> dict[str, Any]:
        plan = _coerce_intent_plan(state.get("intent_plan"))
        last = state.get("last_result") or {}
        sql_history = list(state.get("executed_sql_history") or [])
        partial = bool(state.get("partial", False))
        assumptions = list(plan.assumptions or [])
        context = (
            "Responda ao usuário em PT-BR com base no IntentPlan e last_result.\n"
            "Inclua no final um bloco separado por '---' com proveniência:\n"
            "- SQL executado\n"
            "- Assunções do intent\n"
            "- Parcial: sim/não\n"
            "- Status e avisos do last_result\n"
            f"IntentPlan:\n{_dump_json(plan)}\n"
            f"last_result:\n{json.dumps(last, ensure_ascii=False, default=str)}\n"
            f"executed_sql_history:\n{json.dumps(sql_history, ensure_ascii=False)}\n"
            f"partial: {partial}\n"
        )
        response = answer_llm.invoke([SystemMessage(content=context), *state["messages"]])
        text = (
            response if isinstance(response, str) else getattr(response, "content", str(response))
        )
        text = _append_provenance_footer(
            text,
            sql_history=sql_history,
            assumptions=assumptions,
            partial=partial,
            last_result=last,
        )
        return {
            "final_answer": text,
            "messages": [AIMessage(content=text)],
        }

    # ------------------------------------------------------------------ #
    # Roteadores
    # ------------------------------------------------------------------ #
    def route_after_intent(state: GraphState) -> str:
        route = state.get("intent_route") or "resolve_and_route"
        if route == "finish":
            return "finish"
        if route == "ask_clarification":
            return "ask_clarification"
        if route == "interpret_intent":
            return "interpret_intent"
        return "resolve_and_route"

    def route_after_resolve(state: GraphState) -> str:
        if state.get("intent_route") == "ask_clarification":
            return "ask_clarification"
        if state.get("intent_route") == "finish":
            return "finish"
        path = state.get("execution_path") or "simple"
        return "generate_sql" if path == "simple" else "sufficiency_gate"

    def route_after_clarification(state: GraphState) -> str:
        if state.get("intent_route") == "finish":
            return "finish"
        return "interpret_intent" if has_checkpointer else END

    def route_after_gate(state: GraphState) -> str:
        return (
            "generate_analytical_sql"
            if state.get("gate_action") == "reuse"
            else "plan_materialization"
        )

    def route_after_mat_check(state: GraphState) -> str:
        return "generate_analytical_sql" if state.get("mat_ready") else "plan_materialization"

    def route_after_verify(state: GraphState) -> str:
        raw = state.get("verify_decision") or {}
        action = raw.get("action", "answer")
        budget = _budget(state)
        if action == "refine_sql" and budget.exhausted("refine_count"):
            return "answer"
        if action == "answer":
            return "answer"
        if action == "data_gap":
            return "sufficiency_gate"
        path = state.get("execution_path") or "simple"
        return "generate_sql" if path == "simple" else "generate_analytical_sql"

    # ------------------------------------------------------------------ #
    # Montagem
    # ------------------------------------------------------------------ #
    graph = StateGraph(GraphState)
    graph.add_node("init_state", init_state)
    graph.add_node("interpret_intent", interpret_intent)
    graph.add_node("ask_clarification", ask_clarification)
    graph.add_node("finish", finish)
    graph.add_node("resolve_and_route", resolve_and_route)
    graph.add_node("generate_sql", generate_sql)
    graph.add_node("exec_source", exec_source)
    graph.add_node("verify", verify)
    graph.add_node("answer", answer)
    graph.add_node("sufficiency_gate", sufficiency_gate)
    graph.add_node("plan_materialization", plan_materialization)
    graph.add_node("materialize", materialize)
    graph.add_node("check_materialization", check_materialization)
    graph.add_node("generate_analytical_sql", generate_analytical_sql)
    graph.add_node("exec_duckdb", exec_duckdb)

    graph.add_edge(START, "init_state")
    graph.add_edge("init_state", "interpret_intent")
    graph.add_conditional_edges(
        "interpret_intent",
        route_after_intent,
        ["ask_clarification", "interpret_intent", "resolve_and_route", "finish"],
    )
    graph.add_conditional_edges(
        "ask_clarification",
        route_after_clarification,
        ["finish", "interpret_intent", END],
    )
    graph.add_edge("finish", END)
    graph.add_conditional_edges(
        "resolve_and_route",
        route_after_resolve,
        ["ask_clarification", "generate_sql", "sufficiency_gate", "finish"],
    )
    graph.add_edge("generate_sql", "exec_source")
    graph.add_edge("exec_source", "verify")
    graph.add_conditional_edges(
        "sufficiency_gate",
        route_after_gate,
        ["generate_analytical_sql", "plan_materialization"],
    )
    graph.add_edge("plan_materialization", "materialize")
    graph.add_conditional_edges(
        "check_materialization",
        route_after_mat_check,
        ["generate_analytical_sql", "plan_materialization"],
    )
    graph.add_edge("materialize", "check_materialization")
    graph.add_edge("generate_analytical_sql", "exec_duckdb")
    graph.add_edge("exec_duckdb", "verify")
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        ["answer", "generate_sql", "generate_analytical_sql", "sufficiency_gate"],
    )
    graph.add_edge("answer", END)

    compiled = graph.compile(checkpointer=checkpointer)
    logger.info("Grafo dual-path compilado.")
    return compiled


def _load_rows_into_duckdb(
    session: DuckDBSession,
    table_name: str,
    rows: list[dict[str, Any]],
    *,
    replace: bool = True,
) -> None:
    """Carrega linhas dict no DuckDB (MVP — helper local)."""
    conn = session._conn
    if replace:
        conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        session._materialized.discard(table_name)

    if not rows:
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" (placeholder VARCHAR)')
        session._materialized.add(table_name)
        return

    columns = list(rows[0].keys())
    col_defs = ", ".join(f'"{c}" VARCHAR' for c in columns)
    conn.execute(f'CREATE TABLE "{table_name}" ({col_defs})')
    placeholders = ", ".join(["?"] * len(columns))
    col_list = ", ".join(f'"{c}"' for c in columns)
    values = [tuple(row.get(c) for c in columns) for row in rows]
    conn.executemany(
        f'INSERT INTO "{table_name}" ({col_list}) VALUES ({placeholders})',
        values,
    )
    session._materialized.add(table_name)


__all__ = ["GateDecision", "GraphState", "MaterializationCheck", "build_graph"]
