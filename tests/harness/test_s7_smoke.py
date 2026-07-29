"""Harness S7 — smoke offline do domínio playground (clientes/recebíveis).

Registra métricas por pergunta: ``success``, ``path``, ``rejected``.
Usa ``ScriptedLLM`` + ``dual_path=True`` (sem LLM/DB live).

Full S7 com LLM real, latência e banco do playground é trabalho futuro;
testes marcados ``@pytest.mark.skip`` podem ser habilitados quando houver credenciais.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from txt2sql.agent import build_agent
from txt2sql.artifacts import (
    MaterializationPlan,
    MaterializationStep,
    SQLPlan,
    VerifyDecision,
)
from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    DatabaseConfig,
    DuckDBConfig,
    ShardingConfig,
    TableConfig,
)
from txt2sql.intent import FilterClause, IntentPlan, MetricClause

S7_METRIC_KEYS = frozenset({"success", "path", "rejected"})


@dataclass(frozen=True)
class S7Question:
    """Pergunta placeholder do domínio playground."""

    id: str
    prompt: str
    description: str


S7_QUESTIONS: tuple[S7Question, ...] = (
    S7Question(
        id="clientes_razao_social",
        prompt="Qual a razão social do cliente CNPJ 111?",
        description="lookup simples em clientes (path simple)",
    ),
    S7Question(
        id="recebiveis_total_cnpj",
        prompt="Qual o total de recebíveis do CNPJ 12345678000190?",
        description="shard + force_analytical (path analytical)",
    ),
    S7Question(
        id="clientes_delete_rejected",
        prompt="Apague todos os clientes",
        description="DML rejeitado pelo policy gate",
    ),
)


class GateDecision:
    """Decisão stub do sufficiency_gate (reuse / refresh)."""

    def __init__(self, action: str) -> None:
        self.action = action


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


def _cfg_clientes_db() -> AgentConfig:
    tmp = tempfile.mkdtemp()
    main_db = Path(tmp) / "main.db"
    conn = sqlite3.connect(main_db)
    conn.executescript(
        "CREATE TABLE clientes (cnpj TEXT, razao_social TEXT);"
        "INSERT INTO clientes VALUES ('111', 'Alpha');"
    )
    conn.commit()
    conn.close()
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


def _cfg_recebiveis_analytical() -> AgentConfig:
    tmp = tempfile.mkdtemp()
    shard1 = Path(tmp) / "shard1.db"
    conn = sqlite3.connect(shard1)
    conn.executescript(
        "CREATE TABLE recebiveis_123 (cnpj TEXT, valor REAL, status TEXT);"
        "INSERT INTO recebiveis_123 VALUES "
        "('12345678000190', 100.0, 'pago'),"
        "('12345678000190', 50.0, 'pendente'),"
        "('12345678000190', 25.0, 'pago');"
    )
    conn.commit()
    conn.close()
    return AgentConfig(
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
                    resolver="examples.shard_resolver_example:resolve_cnpj_shard",
                ),
                columns=[
                    ColumnConfig(name="cnpj"),
                    ColumnConfig(name="valor"),
                    ColumnConfig(name="status"),
                ],
                duckdb=DuckDBConfig(enabled=True, force_analytical=True, fetch_limit=1000),
            )
        ],
        dialect=None,
    )


def collect_s7_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Extrai métricas mínimas do estado final do grafo dual-path."""
    last = result.get("last_result") or {}
    status = last.get("status")
    rejected = status == "rejected"
    success = status == "ok" and bool(result.get("final_answer"))
    return {
        "success": success,
        "path": result.get("execution_path") or "",
        "rejected": rejected,
    }


def assert_s7_metrics_shape(metrics: dict[str, Any]) -> None:
    assert set(metrics.keys()) == S7_METRIC_KEYS
    assert isinstance(metrics["success"], bool)
    assert isinstance(metrics["path"], str)
    assert isinstance(metrics["rejected"], bool)


def test_s7_simple_path_clientes_offline(monkeypatch: Any) -> None:
    """S7 offline: pergunta clientes → path simple com sucesso."""
    _env()
    cfg = _cfg_clientes_db()
    q = S7_QUESTIONS[0]
    ready = IntentPlan(
        status="ready",
        question_rewrite="razão social do cliente 111",
        metrics=[MetricClause(table_id="clientes", column_id="razao_social", agg="none")],
    )
    script = [
        ready,
        SQLPlan(sql="SELECT razao_social FROM clientes WHERE cnpj = '111'", dialect="postgres"),
        VerifyDecision(action="answer", reason="ok"),
        "A razão social do cliente 111 é Alpha.",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=None, dual_path=True)
    result = agent.invoke({"messages": [HumanMessage(content=q.prompt)]})
    metrics = collect_s7_metrics(result)

    assert_s7_metrics_shape(metrics)
    assert metrics["success"] is True
    assert metrics["path"] == "simple"
    assert metrics["rejected"] is False


def test_s7_analytical_path_recebiveis_offline(monkeypatch: Any) -> None:
    """S7 offline: recebíveis shardados + force_analytical → path analytical."""
    _env()
    cfg = _cfg_recebiveis_analytical()
    q = S7_QUESTIONS[1]
    ready = IntentPlan(
        status="ready",
        question_rewrite="total recebíveis do CNPJ",
        filters=[
            FilterClause(
                table_id="recebiveis",
                column_id="cnpj",
                op="eq",
                value="12345678000190",
            )
        ],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    mat_plan = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="SELECT cnpj, valor, status FROM recebiveis_123",
                target_table="recebiveis",
                mode="replace",
            )
        ],
        rationale="extract shard",
    )
    script = [
        ready,
        GateDecision("refresh"),
        mat_plan,
        SQLPlan(sql="SELECT SUM(valor) AS total FROM recebiveis", dialect="duckdb"),
        VerifyDecision(action="answer", reason="ok"),
        "O total de recebíveis é R$ 175,00.",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=MemorySaver(), dual_path=True)
    result = agent.invoke(
        {"messages": [HumanMessage(content=q.prompt)]},
        config={"configurable": {"thread_id": "s7-analytical"}},
    )
    metrics = collect_s7_metrics(result)

    assert_s7_metrics_shape(metrics)
    assert metrics["success"] is True
    assert metrics["path"] == "analytical"
    assert metrics["rejected"] is False


def test_s7_rejected_sql_offline(monkeypatch: Any) -> None:
    """S7 offline: DML rejeitado — success=False, rejected=True."""
    _env()
    cfg = _cfg_clientes_db()
    q = S7_QUESTIONS[2]
    ready = IntentPlan(
        status="ready",
        question_rewrite="apagar clientes",
        metrics=[MetricClause(table_id="clientes", column_id="cnpj", agg="none")],
    )
    script = [
        ready,
        SQLPlan(sql="DELETE FROM clientes", dialect="postgres"),
        VerifyDecision(action="answer", reason="SQL rejeitado pelo policy gate"),
        "Não foi possível executar a operação solicitada.",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=None, dual_path=True)
    result = agent.invoke({"messages": [HumanMessage(content=q.prompt)]})
    metrics = collect_s7_metrics(result)

    assert_s7_metrics_shape(metrics)
    assert metrics["success"] is False
    assert metrics["rejected"] is True


@pytest.mark.skip(reason="requires live LLM/DB — full S7 futuro")
def test_s7_live_playground_smoke() -> None:
    """Placeholder para harness S7 com playground/config.yaml e LLM real."""
    pytest.skip("full S7 com LLM/DB live ainda não implementado")
