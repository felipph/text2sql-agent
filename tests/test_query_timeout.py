"""Timeout de execução em sql_db_query (config + registry + agente)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

import txt2sql.agent as agent_mod
from txt2sql.config import AgentConfig, ColumnConfig, DatabaseConfig, TableConfig, load_config
from txt2sql.db.registry import DatabaseRegistry, QueryTimeoutError
from txt2sql.intent import IntentPlan, MetricClause


def test_load_config_query_timeout_default(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db_main\n"
        "    connection_string: 'sqlite:///:memory:'\n"
        "tables: []\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.query_timeout == 30
    assert cfg.databases[0].query_timeout is None


def test_load_config_query_timeout_override(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db_main\n"
        "    connection_string: 'sqlite:///:memory:'\n"
        "    query_timeout: 60\n"
        "agent:\n"
        "  query_timeout: 15\n"
        "tables: []\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.query_timeout == 15
    assert cfg.databases[0].query_timeout == 60


def test_load_config_query_timeout_zero_allowed(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db_main\n"
        "    connection_string: 'sqlite:///:memory:'\n"
        "agent:\n"
        "  query_timeout: 0\n"
        "tables: []\n",
        encoding="utf-8",
    )
    assert load_config(p).query_timeout == 0


def test_load_config_query_timeout_negative_rejected(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db_main\n"
        "    connection_string: 'sqlite:///:memory:'\n"
        "agent:\n"
        "  query_timeout: -1\n"
        "tables: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="query_timeout"):
        load_config(p)


def test_load_config_db_query_timeout_negative_rejected(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db_main\n"
        "    connection_string: 'sqlite:///:memory:'\n"
        "    query_timeout: -5\n"
        "tables: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="query_timeout"):
        load_config(p)


def test_effective_query_timeout_inheritance() -> None:
    cfg = AgentConfig(
        databases=[DatabaseConfig(id="db_a", connection_string="sqlite:///:memory:")],
        query_timeout=30,
    )
    assert cfg.effective_query_timeout("db_a") == 30

    cfg2 = AgentConfig(
        databases=[
            DatabaseConfig(
                id="db_b",
                connection_string="sqlite:///:memory:",
                query_timeout=60,
            )
        ],
        query_timeout=30,
    )
    assert cfg2.effective_query_timeout("db_b") == 60


def _registry_with_timeout(query_timeout: int) -> DatabaseRegistry:
    cfg = AgentConfig(
        databases=[
            DatabaseConfig(
                id="db_main",
                connection_string="sqlite:///:memory:",
                read_only=False,
            )
        ],
        query_timeout=query_timeout,
    )
    return DatabaseRegistry(cfg)


def test_execute_raises_query_timeout() -> None:
    registry = _registry_with_timeout(1)

    def slow_execute(*args: Any, **kwargs: Any) -> Any:
        time.sleep(3)
        raise AssertionError("não deveria completar")

    fake_conn = MagicMock()
    fake_conn.execute.side_effect = slow_execute
    engine = MagicMock()
    engine.connect.return_value = fake_conn  # timeout>0: not a CM
    registry._engines["db_main"] = engine

    start = time.monotonic()
    with pytest.raises(QueryTimeoutError, match="timeout") as exc:
        registry.execute("db_main", "SELECT 1")
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"execute bloqueou {elapsed:.2f}s; bug de shutdown wait=True"
    assert exc.value.database_id == "db_main"
    assert exc.value.timeout == 1
    assert fake_conn.invalidate.called or fake_conn.close.called


def test_execute_timeout_disabled_completes() -> None:
    registry = _registry_with_timeout(0)

    result_proxy = MagicMock()
    result_proxy.keys.return_value = ["x"]
    result_proxy.fetchall.return_value = [(1,)]

    fake_conn = MagicMock()
    fake_conn.execute.return_value = result_proxy

    class _CM:
        def __enter__(self) -> Any:
            return fake_conn

        def __exit__(self, *args: object) -> bool:
            return False

    engine = MagicMock()
    engine.connect.return_value = _CM()
    registry._engines["db_main"] = engine

    rows = registry.execute("db_main", "SELECT 1")
    assert rows == [{"x": 1}]


def test_execute_within_timeout_returns_rows() -> None:
    registry = _registry_with_timeout(5)

    result_proxy = MagicMock()
    result_proxy.keys.return_value = ["n"]
    result_proxy.fetchall.return_value = [(42,)]

    fake_conn = MagicMock()
    fake_conn.execute.return_value = result_proxy

    engine = MagicMock()
    engine.connect.return_value = fake_conn  # timeout>0: not a CM
    registry._engines["db_main"] = engine

    rows = registry.execute("db_main", "SELECT 42")
    assert rows == [{"n": 42}]


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


def test_execute_queries_timeout_becomes_tool_message(monkeypatch: Any) -> None:
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
            DatabaseConfig(
                id="db_main",
                connection_string=f"sqlite:///{main_db}",
            )
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
        query_timeout=1,
    )

    def boom(self: Any, database_id: str, sql: str) -> list[dict[str, Any]]:
        raise QueryTimeoutError(database_id, 1)

    ready = IntentPlan(
        status="ready",
        question_rewrite="nome do cliente 111",
        metrics=[
            MetricClause(
                table_id="clientes",
                column_id="razao_social",
                agg="none",
            )
        ],
    )
    from txt2sql.artifacts import SQLPlan, VerifyDecision

    script = [
        ready,
        SQLPlan(sql="SELECT razao_social FROM clientes WHERE cnpj = '111'", dialect="duckdb"),
        VerifyDecision(action="answer", reason="timeout"),
        "Não consegui a tempo.",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    monkeypatch.setattr(DatabaseRegistry, "execute", boom)

    agent = agent_mod.build_agent(cfg, checkpointer=MemorySaver())
    result = agent.invoke(
        {"messages": [HumanMessage(content="nome?")]},
        config={"configurable": {"thread_id": "timeout-q"}},
    )
    # Dual-path: timeout vira last_result com status "timeout"
    last_result = result.get("last_result")
    status = (
        last_result.status
        if hasattr(last_result, "status")
        else (last_result or {}).get("status")
    )
    assert status == "timeout", f"last_result={last_result}"
