"""Fila de sql_db_query: várias tools no mesmo passo devem todas executar."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

import txt2sql.agent as agent_mod
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


def test_two_sql_db_query_in_same_step_both_execute(monkeypatch: Any) -> None:
    tmp = tempfile.mkdtemp()
    main_db = Path(tmp) / "main.db"
    c = sqlite3.connect(main_db)
    c.executescript(
        "CREATE TABLE clientes (cnpj TEXT, razao_social TEXT);"
        "INSERT INTO clientes VALUES ('111', 'Alpha'), ('222', 'Beta');"
        "CREATE TABLE outros (id INTEGER, nome TEXT);"
        "INSERT INTO outros VALUES (1, 'x');"
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
            TableConfig(id="clientes", database="db_main", name="clientes"),
            TableConfig(
                id="outros",
                database="db_main",
                name="outros",
                columns=[ColumnConfig(name="id"), ColumnConfig(name="nome")],
            ),
        ],
        dialect=None,
    )

    ready = IntentPlan(
        status="ready",
        question_rewrite="nomes dos clientes 111 e 222",
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
                },
                {
                    "name": "sql_db_query",
                    "args": {"query": "SELECT razao_social FROM clientes WHERE cnpj = '222'"},
                    "id": "q2",
                    "type": "tool_call",
                },
            ],
        ),
        AIMessage(content="Alpha e Beta."),
    ]
    monkeypatch.setattr(agent_mod, "build_llm", lambda config: ScriptedLLM(script))

    agent = agent_mod.build_agent(cfg, checkpointer=MemorySaver(), dual_path=False)
    result = agent.invoke(
        {"messages": [HumanMessage(content="nomes?")]},
        config={"configurable": {"thread_id": "multi-q"}},
    )
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) >= 2
    contents = " ".join(str(m.content) for m in tool_msgs)
    assert "Alpha" in contents
    assert "Beta" in contents
    assert "não pôde ser processada" not in contents
