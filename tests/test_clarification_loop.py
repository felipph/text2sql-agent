"""Regressão: teto de clarificações HITL (sem heurísticas de domínio)."""

from __future__ import annotations

import os
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from txt2sql.agent import build_agent
from txt2sql.artifacts import Budget, SQLPlan, VerifyDecision
from txt2sql.config import AgentConfig, ColumnConfig, DatabaseConfig, TableConfig
from txt2sql.intent import Clarification, IntentPlan, MetricClause


class ScriptedLLM:
    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self._i = 0

    def bind_tools(self, tools: list[Any]) -> ScriptedLLM:
        return self

    def with_structured_output(self, schema: Any, **_kwargs: Any) -> ScriptedLLM:
        return self

    def invoke(self, messages: list[Any]) -> Any:
        msg = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return msg


def _env() -> None:
    os.environ.update(
        AZURE_OPENAI_DEPLOYMENT="gpt-4o",
        AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com/",
        AZURE_OPENAI_API_KEY="dummy",
    )


def _cfg() -> AgentConfig:
    return AgentConfig(
        databases=[
            DatabaseConfig(id="db_main", connection_string="sqlite:///:memory:")
        ],
        tables=[
            TableConfig(
                id="clientes",
                database="db_main",
                name="clientes",
                columns=[
                    ColumnConfig(name="cnpj"),
                    ColumnConfig(name="razao_social"),
                ],
            )
        ],
        dialect=None,
    )


def _interrupt_question(result: dict[str, Any]) -> str | None:
    for item in result.get("__interrupt__") or []:
        value = getattr(item, "value", None) or item
        if isinstance(value, dict) and value.get("type") == "clarification":
            return str(value.get("question") or "")
    return None


def test_clarification_budget_stops_loop(monkeypatch: Any) -> None:
    """Após max_clarifications interrupts, encerra sem novo HITL."""
    _env()
    cfg = _cfg()
    clarify = IntentPlan(
        status="needs_clarification",
        question_rewrite="consulta vaga",
        clarification=Clarification(question="Pode detalhar a pergunta?"),
    )
    monkeypatch.setattr(
        "txt2sql.graph.build_llm", lambda config: ScriptedLLM([clarify])
    )
    agent = build_agent(cfg, checkpointer=MemorySaver())
    thread = {"configurable": {"thread_id": "clarify-budget"}}

    max_c = Budget().max_clarifications
    r = agent.invoke(
        {"messages": [HumanMessage(content="mostre os dados")]},
        config=thread,
    )
    assert _interrupt_question(r)

    for i in range(max_c - 1):
        r = agent.invoke(Command(resume=f"ainda vago {i}"), config=thread)
        assert _interrupt_question(r), f"esperava interrupt na rodada {i + 2}"

    r_final = agent.invoke(Command(resume="ainda sem detalhe"), config=thread)
    assert _interrupt_question(r_final) is None
    assert r_final.get("final_answer")
    assert "esclarecimentos" in (r_final.get("final_answer") or "").lower()


def test_clarification_question_persists_in_messages(monkeypatch: Any) -> None:
    """Pergunta HITL deve entrar no histórico como AIMessage (não só no interrupt)."""
    _env()
    cfg = _cfg()
    question = "Qual o período desejado para a consulta?"
    ambiguous = IntentPlan(
        status="needs_clarification",
        question_rewrite="faturamento",
        clarification=Clarification(question=question),
    )
    ready = IntentPlan(
        status="ready",
        question_rewrite="faturamento do mês atual",
        metrics=[MetricClause(table_id="clientes", column_id="razao_social", agg="none")],
    )
    scripted = ScriptedLLM(
        [
            ambiguous,
            ready,
            SQLPlan(sql="SELECT razao_social FROM clientes LIMIT 10", dialect="duckdb"),
            VerifyDecision(action="answer", reason="ok"),
            "Resposta final.",
        ]
    )
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: scripted)
    agent = build_agent(cfg, checkpointer=MemorySaver())
    thread = {"configurable": {"thread_id": "clarify-hist"}}

    r1 = agent.invoke(
        {"messages": [HumanMessage(content="faturamento?")]},
        config=thread,
    )
    assert _interrupt_question(r1) == question

    r2 = agent.invoke(Command(resume="mês atual"), config=thread)
    msgs = list(r2.get("messages") or [])
    ai_clarify = [
        m
        for m in msgs
        if isinstance(m, AIMessage) and question in str(m.content)
    ]
    assert ai_clarify, "esperava AIMessage com a pergunta de clarificação no histórico"
    humans = [m for m in msgs if isinstance(m, HumanMessage)]
    assert any("mês atual" in str(m.content) for m in humans)

    # garante ordem pergunta AI → resposta Human no histórico
    clarify_idx = next(
        i for i, m in enumerate(msgs) if isinstance(m, AIMessage) and question in str(m.content)
    )
    answer_idx = next(
        i
        for i, m in enumerate(msgs)
        if isinstance(m, HumanMessage) and "mês atual" in str(m.content)
    )
    assert clarify_idx < answer_idx
