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

from txt2sql.analytical_planning import (
    GateDecision,
    MaterializationCheck,
    build_materialization_plan,
    check_materialization_ready,
    run_sufficiency_gate,
)
from txt2sql.artifacts import (
    Budget,
    DuckDBCatalog,
    ExecutionResult,
    MaterializationPlan,
    ShardRouting,
    SQLPlan,
    VerifyDecision,
)
from txt2sql.config import AgentConfig
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.db.materialize import materialize_tables
from txt2sql.db.registry import DatabaseRegistry, QueryTimeoutError
from txt2sql.db.schema import SchemaLoader
from txt2sql.db.session_store import DuckDBSessionStore
from txt2sql.intent import Clarification, IntentPlan, validate_intent
from txt2sql.llm import build_llm
from txt2sql.middleware import compact_result, result_from_rejection, result_from_timeout
from txt2sql.path_routing import route_execution
from txt2sql.policy import check_sql_plan
from txt2sql.prompts import Txt2SqlPromptBuilder
from txt2sql.shard_routing import (
    ClarifyNeeded,
    ensure_discriminator_filters,
    missing_discriminator_filter_errors,
    resolve_routing,
)
from txt2sql.sufficiency import SufficiencyDecision, intent_table_ids

MAX_INTENT_RETRIES = 2
CLARIFICATION_EXHAUSTED = (
    "Não consegui obter esclarecimentos suficientes para continuar. "
    "Reformule a pergunta com todos os detalhes necessários numa única mensagem."
)


class GraphState(MessagesState):
    """Estado do grafo dual-path."""

    intent_plan: IntentPlan | None
    intent_retries: int
    intent_route: str
    execution_path: str
    shard_routing: ShardRouting | None
    sql_plan: SQLPlan | None
    materialization_plan: MaterializationPlan | None
    last_result: ExecutionResult | None
    executed_sql_history: list[str]
    verify_decision: VerifyDecision | None
    duckdb_catalog: DuckDBCatalog
    budget: Budget
    gate_action: str
    sufficiency_decision: SufficiencyDecision | None
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


def _coerce_model[T: BaseModel](raw: Any, model: type[T], *, default: T | None = None) -> T:
    """Normaliza instância ou dict (checkpoint) para modelo Pydantic."""
    if isinstance(raw, model):
        return raw
    if raw is None:
        if default is not None:
            return default
        return model()
    return model.model_validate(raw)


def _coerce_intent_plan(raw: Any) -> IntentPlan:
    if isinstance(raw, IntentPlan):
        return raw
    return IntentPlan.model_validate(raw)


def _budget(state: GraphState) -> Budget:
    return _coerce_model(state.get("budget"), Budget, default=Budget())


def _shard_routing(state: GraphState) -> ShardRouting:
    return _coerce_model(state.get("shard_routing"), ShardRouting, default=ShardRouting())


def _catalog(state: GraphState) -> DuckDBCatalog:
    return _coerce_model(state.get("duckdb_catalog"), DuckDBCatalog, default=DuckDBCatalog())


def _sql_plan(state: GraphState) -> SQLPlan | None:
    raw = state.get("sql_plan")
    if raw is None:
        return None
    return _coerce_model(raw, SQLPlan)


def _last_result(state: GraphState) -> ExecutionResult | None:
    raw = state.get("last_result")
    if raw is None:
        return None
    return _coerce_model(raw, ExecutionResult)


def _verify_decision(state: GraphState) -> VerifyDecision | None:
    raw = state.get("verify_decision")
    if raw is None:
        return None
    return _coerce_model(raw, VerifyDecision)


def _materialization_plan(state: GraphState) -> MaterializationPlan | None:
    raw = state.get("materialization_plan")
    if raw is None:
        return None
    return _coerce_model(raw, MaterializationPlan)


def _intent_had_metrics(state: GraphState) -> bool:
    raw = state.get("intent_plan")
    if raw is None:
        return False
    plan = _coerce_intent_plan(raw)
    return bool(plan.metrics)


def _intent_table_ids(plan: IntentPlan) -> set[str]:
    """Tabelas lógicas tocadas pelo IntentPlan."""
    return intent_table_ids(plan)


def _sufficiency_decision(state: GraphState) -> SufficiencyDecision | None:
    raw = state.get("sufficiency_decision")
    if raw is None:
        return None
    return _coerce_model(raw, SufficiencyDecision)

def _last_human_text(state: dict[str, Any]) -> str:
    """Conteúdo da última HumanMessage (para fallback de discriminador)."""
    for msg in reversed(state.get("messages") or []):
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, str):
                return content
            return str(content or "")
    return ""


def _append_provenance_footer(
    text: str,
    *,
    sql_history: list[str],
    assumptions: list[str],
    partial: bool,
    last_result: ExecutionResult | dict[str, Any] | None,
) -> str:
    """Anexa bloco de proveniência se o LLM não incluiu '---'."""
    if "---" in text:
        return text
    result: ExecutionResult | None = None
    if isinstance(last_result, ExecutionResult):
        result = last_result
    elif last_result:
        result = ExecutionResult.model_validate(last_result)
    lines: list[str] = []
    if sql_history:
        lines.append("SQL: " + "; ".join(sql_history))
    if assumptions:
        lines.append("Assunções: " + "; ".join(assumptions))
    lines.append(f"Parcial: {'sim' if partial else 'não'}")
    if result is not None:
        status = result.status or ""
        warnings = result.warnings or []
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
    agent_config = config
    registry = DatabaseRegistry(agent_config)
    schema_loader = SchemaLoader(agent_config, registry)
    prompt_builder = Txt2SqlPromptBuilder(agent_config)
    intent_prompt = prompt_builder.build_intent_prompt(schema_loader=schema_loader)
    logger.debug(f"Intent prompt: {intent_prompt}")
    has_checkpointer = checkpointer is not None

    if session_store is None:
        session_store = DuckDBSessionStore(Path(tempfile.mkdtemp(prefix="txt2sql_duckdb_")))

    llm = build_llm(agent_config)
    # function_calling: schemas com Optionals/aninhados passam no Azure OpenAI;
    # json_schema estrito rejeita dict livre (ex. params) e alguns defaults.
    intent_llm = llm.with_structured_output(IntentPlan)
    sql_llm = llm.with_structured_output(SQLPlan, method="function_calling")
    mat_llm = llm.with_structured_output(MaterializationPlan, method="function_calling")
    mat_check_llm = llm.with_structured_output(MaterializationCheck, method="function_calling")
    verify_llm = llm.with_structured_output(VerifyDecision, method="function_calling")
    gate_llm = llm.with_structured_output(GateDecision, method="function_calling")
    answer_llm = llm

    default_dialect = agent_config.dialect

    def _thread_id(run_config: RunnableConfig) -> str:
        configurable = run_config.get("configurable") or {}
        return str(configurable.get("thread_id") or "default")

    def _ensure_duckdb_session(state: GraphState, run_config: RunnableConfig) -> DuckDBSession:
        """Reconecta sessão file-backed (UntrackedValue some no resume HITL)."""
        session = state.get("duckdb_session")
        if session is not None:
            return session
        return session_store.get(_thread_id(run_config))

    def init_state(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = _thread_id(config)
        session = session_store.get(thread_id)

        prev_catalog = state.get("duckdb_catalog")
        catalog = (
            _coerce_model(prev_catalog, DuckDBCatalog, default=DuckDBCatalog())
            if prev_catalog
            else DuckDBCatalog()
        )

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
            "duckdb_catalog": catalog,
            "budget": budget,
            "gate_action": "",
            "sufficiency_decision": None,
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
                    "intent_plan": plan,
                    "intent_route": "finish",
                    "final_answer": CLARIFICATION_EXHAUSTED,
                    "messages": [AIMessage(content=CLARIFICATION_EXHAUSTED)],
                }
            return {
                "intent_plan": plan,
                "intent_route": "ask_clarification",
            }

        validation = validate_intent(plan, schema_loader.get_column_index())
        if not validation.ok:
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
                    "intent_plan": clarify,
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
                "intent_plan": plan,
                "intent_retries": retries,
                "intent_route": "interpret_intent",
            }

        # C: grounding do discriminador em filters — retry antes de clarificar
        disc_errors = missing_discriminator_filter_errors(plan, config)
        if disc_errors:
            retries = int(state.get("intent_retries", 0)) + 1
            if retries < MAX_INTENT_RETRIES:
                feedback = SystemMessage(
                    content=(
                        "O IntentPlan está ready mas falta o discriminador de shard "
                        "em filters. Corrija e tente de novo. " + " ".join(disc_errors)
                    )
                )
                return {
                    "messages": [feedback],
                    "intent_plan": plan,
                    "intent_retries": retries,
                    "intent_route": "interpret_intent",
                }
            # retries esgotados → resolve_and_route (value_extractor / ClarifyNeeded)

        return {
            "intent_plan": plan,
            "intent_route": "resolve_and_route",
        }

    def ask_clarification(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        plan = _coerce_intent_plan(state.get("intent_plan"))
        clarification = plan.clarification
        question = (
            clarification.question
            if clarification and clarification.question.strip()
            else "Pode esclarecer a pergunta?"
        )
        options = list(clarification.options) if clarification else []
        logger.info("ask_clarification: {}", question[:120])

        budget = _budget(state)
        if budget.exhausted("clarification_count"):
            return {
                "intent_route": "finish",
                "final_answer": CLARIFICATION_EXHAUSTED,
                "messages": [AIMessage(content=CLARIFICATION_EXHAUSTED)],
            }

        budget = budget.model_copy(update={"clarification_count": budget.clarification_count + 1})

        text = question
        if options:
            text = question + "\nOpções: " + ", ".join(str(o) for o in options)

        if has_checkpointer:
            answer = interrupt(
                {
                    "type": "clarification",
                    "question": question,
                    "options": options,
                }
            )
            # Resume não passa por init_state — rehidrata UntrackedValue.
            # Persiste pergunta+resposta no histórico (interrupt sozinho não grava AIMessage).
            return {
                "messages": [
                    AIMessage(content=text),
                    HumanMessage(content=str(answer)),
                ],
                "budget": budget,
                "final_answer": None,
                "duckdb_session": _ensure_duckdb_session(state, config),
            }

        return {
            "messages": [AIMessage(content=text)],
            "budget": budget,
            "final_answer": None,
        }

    def finish(state: GraphState) -> dict[str, Any]:
        """Encerra com ``final_answer`` já definido (ex.: clarificação esgotada)."""
        text = state.get("final_answer") or CLARIFICATION_EXHAUSTED
        return {"final_answer": text, "messages": [AIMessage(content=text)]}

    def resolve_and_route(state: GraphState) -> dict[str, Any]:
        plan = _coerce_intent_plan(state.get("intent_plan"))
        routing_result = resolve_routing(
            plan,
            config,
            extra_text=_last_human_text(state),
            registry=registry,
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
                    "intent_plan": clarify,
                    "intent_route": "finish",
                    "final_answer": CLARIFICATION_EXHAUSTED,
                    "messages": [AIMessage(content=CLARIFICATION_EXHAUSTED)],
                }
            return {
                "intent_plan": clarify,
                "intent_route": "ask_clarification",
            }
        plan = ensure_discriminator_filters(plan, routing_result, config)
        path = route_execution(plan, routing_result, config)
        logger.info("resolve_and_route: path={} shard_mode={}", path, routing_result.mode)
        return {
            "intent_plan": plan,
            "shard_routing": routing_result,
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
        last = _last_result(state)
        if last is not None and last.status == "rejected":
            context += f"\nErro anterior: {last.error}"
        sql_plan = sql_llm.invoke([SystemMessage(content=context), *state["messages"]])
        if isinstance(sql_plan, dict):
            sql_plan = SQLPlan.model_validate(sql_plan)
        return {"sql_plan": sql_plan}

    def exec_source(state: GraphState) -> dict[str, Any]:
        sql_plan = _sql_plan(state)
        if sql_plan is None:
            return {"last_result": ExecutionResult(status="error", error="sql_plan ausente")}
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
            return {"last_result": result_from_rejection(decision.error or "rejeitado")}

        database_id = _resolve_database_id(config, shard, decision.sql)
        try:
            rows = registry.execute(database_id, decision.sql)
        except QueryTimeoutError as err:
            return {"last_result": result_from_timeout(str(err))}
        except Exception as err:  # noqa: BLE001
            return {
                "last_result": ExecutionResult(status="error", error=str(err)),
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
            "last_result": result,
            "executed_sql_history": history,
        }

    def sufficiency_gate(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        catalog = _catalog(state)
        budget = _budget(state)
        session = _ensure_duckdb_session(state, config)
        plan = _coerce_intent_plan(state.get("intent_plan"))
        shard = _shard_routing(state)

        def _gate_llm_fallback(decision: SufficiencyDecision) -> Literal["reuse", "refresh"]:
            context = (
                "Decida se o DuckDBCatalog cobre o IntentPlan (reuse) ou precisa refresh.\n"
                f"Diagnóstico determinístico:\n{_dump_json(decision.reasons)}\n"
                f"IntentPlan:\n{_dump_json(plan)}\n"
                f"Catalog:\n{_dump_json(catalog)}"
            )
            logger.debug("System Prompt do sufficiency_gate (fallback LLM): {}", context)
            gate = gate_llm.invoke([SystemMessage(content=context)])
            if isinstance(gate, dict):
                gate = GateDecision.model_validate(gate)
            elif not isinstance(gate, GateDecision):
                gate = GateDecision(action=getattr(gate, "action", "refresh"))
            return gate.action if gate.action in {"reuse", "refresh"} else "refresh"

        gate_action, decision, budget = run_sufficiency_gate(
            intent=plan,
            shard=shard,
            catalog=catalog,
            config=agent_config,
            budget=budget,
            dialect=default_dialect,
            llm_fallback=_gate_llm_fallback,
        )
        return {
            "gate_action": gate_action,
            "sufficiency_decision": decision,
            "budget": budget,
            "duckdb_session": session,
        }

    def plan_materialization(state: GraphState) -> dict[str, Any]:
        plan = _coerce_intent_plan(state.get("intent_plan"))
        shard = _shard_routing(state)
        catalog = _catalog(state)
        decision = _sufficiency_decision(state)

        def _mat_llm_fallback() -> MaterializationPlan:
            logical_ids = sorted(_intent_table_ids(plan)) or [t.id for t in config.tables]
            gaps_blob = ""
            if decision is not None and decision.gaps:
                gaps_blob = f"Gaps (materializar só o necessário):\n{_dump_json(decision.gaps)}\n"
            context = (
                "Gere MaterializationPlan com extracts filtrados (sem agregação pesada na origem).\n"
                "IMPORTANTE: target_table DEVE ser exatamente um table_id lógico da config "
                f"({logical_ids}). Não invente nomes como 'recebiveis_filtered_…'.\n"
                f"{gaps_blob}"
                f"IntentPlan:\n{_dump_json(plan, indent=2)}\n"
                f"ShardRouting:\n{_dump_json(shard, indent=2)}"
            )
            mat = mat_llm.invoke([SystemMessage(content=context)])
            if isinstance(mat, MaterializationPlan):
                return mat
            if isinstance(mat, dict):
                return MaterializationPlan.model_validate(mat)
            return MaterializationPlan.model_validate(mat)

        mat = build_materialization_plan(
            intent=plan,
            shard=shard,
            catalog=catalog,
            config=agent_config,
            decision=decision,
            llm_fallback=_mat_llm_fallback,
        )
        return {"materialization_plan": mat}

    def materialize(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        mat_plan = _materialization_plan(state)
        if mat_plan is None:
            return {
                "last_result": ExecutionResult(status="error", error="materialization_plan ausente")
            }
        shard = _shard_routing(state)
        budget = _budget(state)
        catalog = _catalog(state)
        session = _ensure_duckdb_session(state, config)
        intent = _coerce_intent_plan(state.get("intent_plan"))

        outcome = materialize_tables(
            mat_plan=mat_plan,
            intent=intent,
            shard=shard,
            catalog=catalog,
            session=session,
            registry=registry,
            config=agent_config,
            max_rows_per_extract=budget.max_rows_per_extract,
            dialect=default_dialect,
        )
        if outcome.error_kind != "ok":
            if outcome.error_kind == "timeout":
                payload = result_from_timeout(outcome.error or "timeout")
            else:
                payload = result_from_rejection(outcome.error or "rejeitado")
            return {
                "duckdb_session": session,
                "last_result": payload,
            }

        mat_budget = budget.model_copy(update={"mat_loop_count": budget.mat_loop_count + 1})
        result = _compact_from_state(state, outcome.sample_rows, mat_budget, session=session)
        return {
            "duckdb_session": session,
            "duckdb_catalog": outcome.catalog,
            "budget": mat_budget,
            "last_result": result,
        }

    def check_materialization(state: GraphState) -> dict[str, Any]:
        budget = _budget(state)
        last = _last_result(state)
        last_status = last.status if last is not None else "ok"
        plan = _coerce_intent_plan(state.get("intent_plan"))
        catalog = _catalog(state)
        shard = _shard_routing(state)

        def _mat_check_llm_fallback(decision: SufficiencyDecision) -> bool:
            context = (
                "Avalie se o DuckDBCatalog cobre o IntentPlan para gerar SQL analítico.\n"
                f"Diagnóstico determinístico:\n{_dump_json(decision.reasons)}\n"
                f"IntentPlan:\n{_dump_json(plan, indent=2)}\n"
                f"Catalog:\n{_dump_json(catalog, indent=2)}"
            )
            check = mat_check_llm.invoke([SystemMessage(content=context)])
            if isinstance(check, dict):
                check = MaterializationCheck.model_validate(check)
            elif not isinstance(check, MaterializationCheck):
                check = MaterializationCheck(
                    ready=bool(getattr(check, "ready", True)),
                    reason=str(getattr(check, "reason", "")),
                )
            return check.ready

        mat_ready, partial, decision = check_materialization_ready(
            intent=plan,
            shard=shard,
            catalog=catalog,
            config=agent_config,
            budget=budget,
            last_status=last_status,
            dialect=default_dialect,
            llm_fallback=_mat_check_llm_fallback,
        )
        out: dict[str, Any] = {"mat_ready": mat_ready, "partial": partial}
        if decision is not None:
            out["sufficiency_decision"] = decision
        return out

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
        last = _last_result(state)
        if last is not None and last.status == "rejected":
            context += f"\nErro anterior: {last.error}"
        sql_plan = sql_llm.invoke([SystemMessage(content=context)])
        if isinstance(sql_plan, dict):
            sql_plan = SQLPlan.model_validate(sql_plan)
        return {"sql_plan": sql_plan}

    def exec_duckdb(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        sql_plan = _sql_plan(state)
        if sql_plan is None:
            return {
                "last_result": ExecutionResult(status="error", error="sql_plan ausente"),
            }
        budget = _budget(state)
        shard = _shard_routing(state)
        session = _ensure_duckdb_session(state, config)

        decision = check_sql_plan(
            sql_plan,
            config=agent_config,
            shard_routing=shard,
            path="analytical",
            context="query",
            dialect="duckdb",
            duckdb_catalog=_catalog(state),
        )
        if decision.status == "rejected":
            return {
                "last_result": result_from_rejection(decision.error or "rejeitado"),
                "duckdb_session": session,
            }

        try:
            rows = session.execute(decision.sql)
        except QueryTimeoutError as err:
            return {
                "last_result": result_from_timeout(str(err)),
                "duckdb_session": session,
            }
        except Exception as err:  # noqa: BLE001
            return {
                "last_result": ExecutionResult(status="error", error=str(err)),
                "duckdb_session": session,
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
            "last_result": result,
            "executed_sql_history": history,
            "duckdb_session": session,
        }

    def verify(state: GraphState) -> dict[str, Any]:
        plan = _coerce_intent_plan(state.get("intent_plan"))
        last = _last_result(state)
        context = (
            "Avalie last_result vs IntentPlan. Retorne VerifyDecision.\n"
            "Se last_result.status for rejected/error/timeout por SQL inválido "
            "(ex. nomes físicos de shard no DuckDB), prefira refine_sql.\n"
            f"IntentPlan:\n{_dump_json(plan)}\n"
            f"last_result:\n{_dump_json(last)}"
        )
        decision = verify_llm.invoke([SystemMessage(content=context)])
        if isinstance(decision, dict):
            decision = VerifyDecision.model_validate(decision)
        budget = _budget(state)
        last_status = last.status if last is not None else ""
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
                    decision.reason or f"last_result.status={last_status}; tentando corrigir o SQL."
                ),
            )
        if decision.action == "refine_sql":
            budget = budget.model_copy(update={"refine_count": budget.refine_count + 1})
        return {
            "verify_decision": decision,
            "budget": budget,
        }

    def answer(state: GraphState) -> dict[str, Any]:
        plan = _coerce_intent_plan(state.get("intent_plan"))
        last = _last_result(state)
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
            f"last_result:\n{_dump_json(last)}\n"
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
        decision = _verify_decision(state)
        action = decision.action if decision is not None else "answer"
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


__all__ = ["GraphState", "build_graph"]
