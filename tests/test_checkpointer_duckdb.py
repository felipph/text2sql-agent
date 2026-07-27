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
from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    DatabaseConfig,
    DuckDBConfig,
    ShardingConfig,
    TableConfig,
)


class ScriptedLLM:
    def __init__(self, script: list[AIMessage]) -> None:
        self._script = script
        self._i = 0

    def bind_tools(self, tools: list[Any]) -> "ScriptedLLM":  # noqa: ARG002
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:  # noqa: ARG002
        msg = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return msg


def test_checkpointer_with_duckdb_session_does_not_raise(monkeypatch: Any) -> None:
    tmp = tempfile.mkdtemp()
    shard1 = Path(tmp) / "shard1.db"
    c = sqlite3.connect(shard1)
    c.executescript(
        "CREATE TABLE recebiveis_123 (cnpj TEXT, valor REAL, status TEXT);"
        "INSERT INTO recebiveis_123 VALUES "
        "('12345678000190', 100.0, 'pago'),"
        "('12345678000190', 75.0, 'pago');"
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
            DatabaseConfig(id="db_main", connection_string="sqlite:///:memory:"),
            DatabaseConfig(id="db_shard_1", connection_string=f"sqlite:///{shard1}"),
        ],
        tables=[
            TableConfig(
                id="recebiveis",
                database="db_main",
                name="recebiveis",
                sharding=ShardingConfig(
                    discriminator_column="cnpj",
                    resolver="playground.shard_resolver:resolve_cnpj_shard",
                ),
                columns=[
                    ColumnConfig(name="cnpj", description="CNPJ"),
                    ColumnConfig(name="valor", description="valor"),
                    ColumnConfig(name="status", description="status"),
                ],
                duckdb=DuckDBConfig(enabled=True, trigger="aggregation", fetch_limit=1000),
            )
        ],
        dialect=None,
    )

    script = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "resolve_shard",
                    "args": {
                        "table_id": "recebiveis",
                        "discriminator_value": "12345678000190",
                    },
                    "id": "c1",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "sql_db_query",
                    "args": {"query": "SELECT SUM(valor) AS total FROM recebiveis_123"},
                    "id": "c2",
                }
            ],
        ),
        AIMessage(content="Total 175."),
    ]
    monkeypatch.setattr(agent_mod, "build_llm", lambda config: ScriptedLLM(script))

    agent = agent_mod.build_agent(cfg, checkpointer=MemorySaver())
    result = agent.invoke(
        {"messages": [HumanMessage(content="soma?")]},
        config={"configurable": {"thread_id": "t-duckdb"}},
    )
    assert "175" in (result["messages"][-1].content or "")
