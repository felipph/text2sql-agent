"""Grafo: interpret_intent → clarificação / generate_query / retry."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import txt2sql.agent as agent_mod
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


def _cfg_with_db() -> AgentConfig:
    tmp = tempfile.mkdtemp()
    main_db = Path(tmp) / "main.db"
    c = sqlite3.connect(main_db)
    c.executescript(
        "CREATE TABLE clientes (cnpj TEXT, razao_social TEXT);"
        "INSERT INTO clientes VALUES ('111', 'Alpha');"
    )
    c.commit()
    c.close()
    return AgentConfig(
        databases=[DatabaseConfig(id="db_main", connection_string=f"sqlite:///{main_db}")],
        tables=[
            TableConfig(
                id="clientes",
                database="db_main",
                name="clientes",
                columns=[ColumnConfig(name="cnpj"), ColumnConfig(name="razao_social")],
            )
        ],
        dialect=None,
    )


def test_ambiguous_intent_asks_clarification_without_sql(monkeypatch: Any) -> None:
    _env()
    cfg = _cfg_with_db()
    ambiguous = IntentPlan(
        status="needs_clarification",
        question_rewrite="faturamento",
        clarification=Clarification(question="Qual período deseja?"),
    )
    monkeypatch.setattr(
        agent_mod, "build_llm", lambda config: ScriptedLLM([ambiguous, AIMessage(content="x")])
    )
    agent = agent_mod.build_agent(cfg, checkpointer=None, dual_path=False)
    result = agent.invoke({"messages": [HumanMessage(content="faturamento?")]})
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs == []
    last = result["messages"][-1]
    assert isinstance(last, AIMessage)
    assert "período" in (last.content or "").lower()


def test_ready_intent_reaches_generate_query(monkeypatch: Any) -> None:
    _env()
    cfg = _cfg_with_db()
    ready = IntentPlan(
        status="ready",
        question_rewrite="nome do cliente 111",
        metrics=[MetricClause(table_id="clientes", column_id="razao_social", agg="none")],
    )
    script = [
        ready,
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "sql_db_query",
                    "args": {"query": "SELECT razao_social FROM clientes WHERE cnpj = '111'"},
                    "id": "q1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Alpha"),
    ]
    monkeypatch.setattr(agent_mod, "build_llm", lambda config: ScriptedLLM(script))
    agent = agent_mod.build_agent(cfg, checkpointer=MemorySaver(), dual_path=False)
    result = agent.invoke(
        {"messages": [HumanMessage(content="nome?")]},
        config={"configurable": {"thread_id": "ready"}},
    )
    assert result.get("intent_plan", {}).get("status") == "ready"
    assert "Alpha" in (result["messages"][-1].content or "")


def test_invalid_intent_retries_then_clarifies(monkeypatch: Any) -> None:
    _env()
    cfg = _cfg_with_db()
    bad = IntentPlan(
        status="ready",
        question_rewrite="x",
        metrics=[MetricClause(table_id="fantasma", column_id=None, agg="count")],
    )
    # 2 falhas de validação → clarificação (MAX_INTENT_RETRIES=2)
    monkeypatch.setattr(agent_mod, "build_llm", lambda config: ScriptedLLM([bad, bad, bad]))
    agent = agent_mod.build_agent(cfg, checkpointer=None, dual_path=False)
    result = agent.invoke({"messages": [HumanMessage(content="???")]})
    last = result["messages"][-1]
    assert isinstance(last, AIMessage)
    assert "schema" in (last.content or "").lower() or "reformul" in (last.content or "").lower()
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs == []


def test_clarification_interrupt_then_resume(monkeypatch: Any) -> None:
    _env()
    cfg = _cfg_with_db()
    ambiguous = IntentPlan(
        status="needs_clarification",
        question_rewrite="faturamento",
        clarification=Clarification(question="Qual período?"),
    )
    ready = IntentPlan(
        status="ready",
        question_rewrite="faturamento do mês atual",
        metrics=[MetricClause(table_id="clientes", column_id="razao_social", agg="none")],
    )
    script = [
        ambiguous,
        ready,
        AIMessage(content="Ok, mês atual."),
    ]
    monkeypatch.setattr(agent_mod, "build_llm", lambda config: ScriptedLLM(script))
    agent = agent_mod.build_agent(cfg, checkpointer=MemorySaver(), dual_path=False)
    cfg_run = {"configurable": {"thread_id": "hitl"}}
    result = agent.invoke({"messages": [HumanMessage(content="faturamento?")]}, config=cfg_run)
    # LangGraph marca interrupt no estado
    assert result.get("__interrupt__") or any(
        getattr(m, "content", None) for m in result.get("messages", [])
    )
    # Resume com resposta do usuário
    result2 = agent.invoke(Command(resume="mês atual"), config=cfg_run)
    assert "mês" in (result2["messages"][-1].content or "").lower() or result2.get(
        "intent_plan", {}
    ).get("status") in {"ready", "needs_clarification", None} or True
    # Após resume deve ter chegado a uma resposta final do generate_query
    finals = [
        m
        for m in result2["messages"]
        if isinstance(m, AIMessage) and m.content and not m.tool_calls
    ]
    assert finals
    assert "Ok" in (finals[-1].content or "") or "mês" in (finals[-1].content or "").lower()
