"""Checkpointer + DuckDBSession: sessão não deve ser serializada no checkpoint."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

import txt2sql.agent as agent_mod
from txt2sql.artifacts import SQLPlan, VerifyDecision
from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    DatabaseConfig,
    TableConfig,
)
from txt2sql.intent import IntentPlan, MetricClause


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


def test_checkpointer_with_session_does_not_raise_serialization(monkeypatch: Any) -> None:
    """Verifica que DuckDBSession (UntrackedValue) não causa erro de serialização."""
    tmp = tempfile.mkdtemp()
    main_db = Path(tmp) / "main.db"
    c = sqlite3.connect(main_db)
    c.executescript(
        "CREATE TABLE clientes (cnpj TEXT, razao_social TEXT);"
        "INSERT INTO clientes VALUES ('111', 'Alpha');"
    )
    c.commit()
    c.close()

    os.environ.update(
        AZURE_OPENAI_DEPLOYMENT="gpt-4o",
        AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com/",
        AZURE_OPENAI_API_KEY="dummy",
    )

    cfg = AgentConfig(
        databases=[
            DatabaseConfig(id="db_main", connection_string=f"sqlite:///{main_db}"),
        ],
        tables=[
            TableConfig(
                id="clientes",
                database="db_main",
                name="clientes",
                columns=[
                    ColumnConfig(name="cnpj", description="CNPJ"),
                    ColumnConfig(name="razao_social", description="Razão social"),
                ],
            )
        ],
        dialect=None,
    )

    ready = IntentPlan(
        status="ready",
        question_rewrite="razão social do cliente 111",
        metrics=[MetricClause(table_id="clientes", column_id="razao_social", agg="none")],
    )
    script = [
        ready,
        SQLPlan(sql="SELECT razao_social FROM clientes WHERE cnpj = '111'", dialect="duckdb"),
        VerifyDecision(action="answer", reason="ok"),
        "Alpha.",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))

    agent = agent_mod.build_agent(cfg, checkpointer=MemorySaver())
    # Não deve levantar erro de serialização (DuckDBSession é UntrackedValue)
    result = agent.invoke(
        {"messages": [HumanMessage(content="nome?")]},
        config={"configurable": {"thread_id": "t-duckdb"}},
    )
    assert result is not None
    last = result["messages"][-1]
    assert isinstance(last, AIMessage)
