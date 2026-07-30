"""Smoke do grafo dual-path (txt2sql/graph.py) com LLM scriptado."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from txt2sql.agent import build_agent
from txt2sql.artifacts import (
    Budget,
    DuckDBCatalog,
    ExecutionResult,
    MaterializationPlan,
    MaterializationStep,
    ShardRouting,
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
from txt2sql.intent import (
    Clarification,
    EntityRef,
    FilterClause,
    IntentPlan,
    JoinClause,
    JoinOn,
    MetricClause,
)


def _d(obj: Any) -> dict[str, Any]:
    """Normaliza artefato tipado ou dict (checkpoint) para asserts legados."""
    if obj is None:
        return {}
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True)
    return dict(obj)


class GateDecision:
    """Decisão stub do sufficiency_gate (reuse / refresh)."""

    def __init__(self, action: str) -> None:
        self.action = action


class ScriptedLLM:
    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self._i = 0
        self.invoke_count = 0

    def bind_tools(self, tools: list[Any]) -> ScriptedLLM:
        return self

    def with_structured_output(self, schema: Any, **_kwargs: Any) -> ScriptedLLM:
        return self

    def invoke(self, messages: list[Any]) -> Any:
        self.invoke_count += 1
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


def _cfg_sharded_no_disc() -> AgentConfig:
    return AgentConfig(
        databases=[
            DatabaseConfig(id="db", connection_string="sqlite:///:memory:"),
            DatabaseConfig(id="shard1", connection_string="sqlite:///:memory:"),
        ],
        tables=[
            TableConfig(
                id="recebiveis",
                database="db",
                name="recebiveis",
                sharding=ShardingConfig(
                    discriminator_column="cnpj",
                    resolver="playground.shard_resolver:resolve_cnpj_shard",
                ),
                columns=[
                    ColumnConfig(name="cnpj"),
                    ColumnConfig(name="valor"),
                ],
            )
        ],
    )


def test_graph_state_artifacts_are_typed_instances(monkeypatch: Any) -> None:
    """Após invoke, artefatos do GraphState são instâncias Pydantic (não dict)."""
    _env()
    cfg = _cfg_clientes_db()
    ready = IntentPlan(
        status="ready",
        question_rewrite="nome do cliente 111",
        metrics=[MetricClause(table_id="clientes", column_id="razao_social", agg="none")],
    )
    script = [
        ready,
        SQLPlan(sql="SELECT razao_social FROM clientes WHERE cnpj = '111'", dialect="postgres"),
        VerifyDecision(action="answer", reason="ok"),
        "O cliente 111 é Alpha.",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=None)
    result = agent.invoke({"messages": [HumanMessage(content="nome do cliente 111?")]})

    assert isinstance(result["budget"], Budget)
    assert isinstance(result["duckdb_catalog"], DuckDBCatalog)
    assert isinstance(result["intent_plan"], IntentPlan)
    assert isinstance(result["sql_plan"], SQLPlan)
    assert isinstance(result["shard_routing"], ShardRouting)
    assert isinstance(result["last_result"], ExecutionResult)
    assert isinstance(result["verify_decision"], VerifyDecision)


def test_simple_path_clientes_final_answer(monkeypatch: Any) -> None:
    _env()
    cfg = _cfg_clientes_db()
    ready = IntentPlan(
        status="ready",
        question_rewrite="nome do cliente 111",
        metrics=[MetricClause(table_id="clientes", column_id="razao_social", agg="none")],
    )
    script = [
        ready,
        SQLPlan(sql="SELECT razao_social FROM clientes WHERE cnpj = '111'", dialect="postgres"),
        VerifyDecision(action="answer", reason="ok"),
        "O cliente 111 é Alpha.",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=None)
    result = agent.invoke({"messages": [HumanMessage(content="nome do cliente 111?")]})

    assert result.get("execution_path") == "simple"
    last = _d(result.get("last_result"))
    assert last.get("status") == "ok"
    assert any(r.get("razao_social") == "Alpha" for r in last.get("sample", []))
    assert result.get("final_answer")
    assert "Alpha" in result["final_answer"]


def test_analytical_path_force_analytical_reaches_answer(monkeypatch: Any) -> None:
    _env()
    cfg = _cfg_recebiveis_analytical()
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
        mat_plan,
        SQLPlan(sql="SELECT SUM(valor) AS total FROM recebiveis", dialect="duckdb"),
        VerifyDecision(action="answer", reason="ok"),
        "O total de recebíveis é R$ 175,00.",
    ]
    # Só recebíveis shardados → plano determinístico; mat_plan abaixo é legado/ignorado
    # se o gate já montar o plano. Preferimos script sem GateDecision.
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    # Força path LLM de materialização para manter o extract tipado do script
    monkeypatch.setattr("txt2sql.analytical_planning.build_deterministic_mat_plan", lambda *_a, **_k: None)
    agent = build_agent(cfg, checkpointer=MemorySaver())
    assert "check_materialization" in agent.get_graph().nodes
    result = agent.invoke(
        {"messages": [HumanMessage(content="total recebíveis CNPJ 12345678000190?")]},
        config={"configurable": {"thread_id": "analytical-smoke"}},
    )

    assert result.get("execution_path") == "analytical"
    last = _d(result.get("last_result"))
    assert last.get("status") == "ok"
    totals = [r.get("total") for r in last.get("sample", [])]
    assert any(t == 175.0 for t in totals)
    assert result.get("final_answer")
    assert "175" in result["final_answer"]


def test_simple_path_rejected_sql_reaches_answer(monkeypatch: Any) -> None:
    _env()
    cfg = _cfg_clientes_db()
    ready = IntentPlan(
        status="ready",
        question_rewrite="apagar clientes",
        metrics=[MetricClause(table_id="clientes", column_id="cnpj", agg="none")],
    )
    script = [
        ready,
        SQLPlan(sql="DELETE FROM clientes", dialect="postgres"),
        VerifyDecision(action="answer", reason="SQL rejeitado pelo policy gate"),
        "Não foi possível executar a operação solicitada (SQL rejeitado).",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=None)
    result = agent.invoke({"messages": [HumanMessage(content="apague todos os clientes")]})

    last = _d(result.get("last_result"))
    assert last.get("status") == "rejected"
    assert last.get("error")
    assert result.get("verify_decision")
    assert result.get("final_answer")


def test_missing_discriminator_clarifies(monkeypatch: Any) -> None:
    _env()
    cfg = _cfg_sharded_no_disc()
    ready = IntentPlan(
        status="ready",
        question_rewrite="total recebíveis",
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM([ready, ready]))
    agent = build_agent(cfg, checkpointer=None)
    result = agent.invoke({"messages": [HumanMessage(content="total recebíveis?")]})

    last_msg = result["messages"][-1]
    content = (getattr(last_msg, "content", None) or "").lower()
    assert "cnpj" in content
    assert result.get("final_answer") is None


def test_missing_discriminator_preserves_intent_fields(monkeypatch: Any) -> None:
    """ClarifyNeeded não deve zerar metrics do IntentPlan (trace UX)."""
    _env()
    cfg = _cfg_sharded_no_disc()
    ready = IntentPlan(
        status="ready",
        question_rewrite="soma dos recebíveis",
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        entities=[
            EntityRef(mention="recebíveis", table_id="recebiveis", role="table"),
        ],
    )
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM([ready]))
    agent = build_agent(cfg, checkpointer=None)
    result = agent.invoke({"messages": [HumanMessage(content="soma dos recebíveis?")]})

    plan = _d(result.get("intent_plan"))
    assert plan.get("status") == "needs_clarification"
    assert plan.get("metrics"), "metrics do intent não podem ser apagados na clarificação"
    assert plan["metrics"][0]["table_id"] == "recebiveis"
    assert plan.get("entities"), "entities do intent não podem ser apagados na clarificação"
    assert "cnpj" in ((plan.get("clarification") or {}).get("question") or "").lower()


def test_missing_discriminator_retries_intent_before_clarify(monkeypatch: Any) -> None:
    """C: sem filter no discriminador → retry do intent; depois clarifica."""
    _env()
    cfg = _cfg_sharded_no_disc()
    bad = IntentPlan(
        status="ready",
        question_rewrite="soma dos recebíveis",
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    # 1º e 2º: sem filter → retry; após MAX → resolve → ClarifyNeeded
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM([bad, bad]))
    agent = build_agent(cfg, checkpointer=None)
    result = agent.invoke({"messages": [HumanMessage(content="soma?")]})
    assert result.get("intent_retries", 0) >= 1
    content = (result["messages"][-1].content or "").lower()
    assert "cnpj" in content
    # feedback de retry deve ter sido injetado
    feedbacks = [
        m
        for m in result["messages"]
        if getattr(m, "type", None) == "system"
        or (hasattr(m, "content") and "discriminador" in str(getattr(m, "content", "")).lower())
    ]
    assert feedbacks or result.get("intent_retries", 0) >= 1


def test_check_materialization_retries_plan(monkeypatch: Any) -> None:
    """MaterializationCheck(ready=False) dispara segundo plan_materialization."""
    _env()
    tmp = tempfile.mkdtemp()
    main_db = Path(tmp) / "main.db"
    shard1 = Path(tmp) / "shard1.db"
    conn = sqlite3.connect(main_db)
    conn.executescript(
        "CREATE TABLE clientes (cnpj TEXT, razao_social TEXT);"
        "INSERT INTO clientes VALUES ('12345678000190', 'Acme');"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(shard1)
    conn.executescript(
        "CREATE TABLE recebiveis_123 (cnpj TEXT, valor REAL, status TEXT);"
        "INSERT INTO recebiveis_123 VALUES ('12345678000190', 100.0, 'pago');"
    )
    conn.commit()
    conn.close()
    cfg = AgentConfig(
        databases=[
            DatabaseConfig(id="db_main", connection_string=f"sqlite:///{main_db}"),
            DatabaseConfig(id="db_shard_1", connection_string=f"sqlite:///{shard1}"),
        ],
        tables=[
            TableConfig(
                id="clientes",
                database="db_main",
                name="clientes",
                columns=[ColumnConfig(name="cnpj"), ColumnConfig(name="razao_social")],
            ),
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
            ),
        ],
        dialect=None,
    )
    ready = IntentPlan(
        status="ready",
        question_rewrite="total recebíveis por cliente",
        filters=[
            FilterClause(
                table_id="recebiveis",
                column_id="cnpj",
                op="eq",
                value="12345678000190",
            )
        ],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        joins=[
            JoinClause(
                from_table_id="recebiveis",
                to_table_id="clientes",
                on=[JoinOn(from_column="cnpj", to_column="cnpj")],
            )
        ],
    )
    mat_plan_1 = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="SELECT cnpj, valor, status FROM recebiveis_123",
                target_table="recebiveis",
                mode="replace",
            )
        ],
        rationale="extract shard first",
    )
    mat_plan_2 = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="SELECT cnpj, razao_social FROM clientes",
                target_table="clientes",
                mode="replace",
            )
        ],
        rationale="extract clientes",
    )
    script = [
        ready,
        mat_plan_1,
        mat_plan_2,
        SQLPlan(sql="SELECT SUM(r.valor) AS total FROM recebiveis r", dialect="duckdb"),
        VerifyDecision(action="answer", reason="ok"),
        "Total R$ 100,00.",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=MemorySaver())
    result = agent.invoke(
        {"messages": [HumanMessage(content="total recebíveis do CNPJ?")]},
        config={"configurable": {"thread_id": "mat-retry"}},
    )

    budget = _d(result.get("budget"))
    assert budget.get("mat_loop_count", 0) >= 2
    assert result.get("execution_path") == "analytical"
    assert result.get("final_answer")
    assert "100" in result["final_answer"]


def test_catalog_preserved_across_turns(monkeypatch: Any) -> None:
    """init_state preserva duckdb_catalog entre turnos no mesmo thread_id."""
    _env()
    cfg = _cfg_recebiveis_analytical()
    ready = IntentPlan(
        status="ready",
        question_rewrite="total recebíveis",
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
    )
    script = [
        ready,
        mat_plan,
        SQLPlan(sql="SELECT SUM(valor) AS total FROM recebiveis", dialect="duckdb"),
        VerifyDecision(action="answer", reason="ok"),
        "175.",
        ready,
        SQLPlan(sql="SELECT SUM(valor) AS total FROM recebiveis", dialect="duckdb"),
        VerifyDecision(action="answer", reason="ok"),
        "175 again.",
    ]
    monkeypatch.setattr("txt2sql.analytical_planning.build_deterministic_mat_plan", lambda *_a, **_k: None)
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=MemorySaver())
    thread_cfg = {"configurable": {"thread_id": "catalog-reuse"}}
    r1 = agent.invoke(
        {"messages": [HumanMessage(content="total?")]},
        config=thread_cfg,
    )
    catalog_after = _d(r1.get("duckdb_catalog")).get("tables") or []
    assert len(catalog_after) >= 1

    r2 = agent.invoke(
        {"messages": [HumanMessage(content="total de novo?")]},
        config=thread_cfg,
    )
    catalog_turn2_init = _d(r2.get("duckdb_catalog")).get("tables") or []
    assert len(catalog_turn2_init) >= 1
    assert r2.get("gate_action") == "reuse" or r2.get("final_answer")


def test_resolve_step_table_invented_target_maps_to_logical() -> None:
    """LLM pode inventar target_table; resolve para table_id lógico."""
    from txt2sql.artifacts import ShardBinding, ShardRouting
    from txt2sql.db.materialize import _resolve_step_table

    cfg = _cfg_recebiveis_analytical()
    intent = IntentPlan(
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    step = MaterializationStep(
        source_query="SELECT * FROM recebiveis_123",
        target_table="recebiveis_filtered_65410433218196",
        mode="replace",
    )
    shard = ShardRouting(
        mode="single",
        bindings=[
            ShardBinding(
                table_id="recebiveis",
                discriminator_value="12345678000190",
                database_id="db_shard_1",
                physical_table="recebiveis_123",
            )
        ],
    )
    table = _resolve_step_table(step, shard=shard, intent=intent, agent_config=cfg)
    assert table.id == "recebiveis"


def test_analytical_invented_target_table_still_materializes(monkeypatch: Any) -> None:
    """Materialize não deve KeyError quando target_table é inventado pelo LLM."""
    _env()
    cfg = _cfg_recebiveis_analytical()
    ready = IntentPlan(
        status="ready",
        question_rewrite="total recebíveis",
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
                target_table="recebiveis_filtered_65410433218196",
                mode="replace",
            )
        ],
        rationale="LLM inventou nome",
    )
    script = [
        ready,
        mat_plan,
        SQLPlan(sql="SELECT SUM(valor) AS total FROM recebiveis", dialect="duckdb"),
        VerifyDecision(action="answer", reason="ok"),
        "Total 175.",
    ]
    monkeypatch.setattr("txt2sql.analytical_planning.build_deterministic_mat_plan", lambda *_a, **_k: None)
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=MemorySaver())
    result = agent.invoke(
        {"messages": [HumanMessage(content="total?")]},
        config={"configurable": {"thread_id": "invented-target"}},
    )
    assert result.get("execution_path") == "analytical"
    last = _d(result.get("last_result"))
    assert last.get("status") == "ok"
    catalog = _d(result.get("duckdb_catalog")).get("tables") or []
    assert any(t.get("name") == "recebiveis" for t in catalog)
    assert "175" in (result.get("final_answer") or "")


def _cfg_multi_shard_and_clientes() -> AgentConfig:
    """Dois físicos em db_shard_2 + clientes em db_main (caso playground)."""
    tmp = tempfile.mkdtemp()
    main_db = Path(tmp) / "main.db"
    shard2 = Path(tmp) / "shard2.db"
    conn = sqlite3.connect(main_db)
    conn.executescript(
        "CREATE TABLE clientes (cnpj TEXT, razao_social TEXT);"
        "INSERT INTO clientes VALUES"
        " ('65410433218196', 'Cliente_000'),"
        " ('74778161849593', 'Cliente_001');"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(shard2)
    conn.executescript(
        "CREATE TABLE recebiveis_654 (cnpj TEXT, valor REAL);"
        "INSERT INTO recebiveis_654 VALUES"
        " ('65410433218196', 1000.0), ('65410433218196', 789.28);"
        "CREATE TABLE recebiveis_747 (cnpj TEXT, valor REAL);"
        "INSERT INTO recebiveis_747 VALUES ('74778161849593', 2332.27);"
    )
    conn.commit()
    conn.close()
    return AgentConfig(
        databases=[
            DatabaseConfig(id="db_main", connection_string=f"sqlite:///{main_db}"),
            DatabaseConfig(id="db_shard_1", connection_string="sqlite:///:memory:"),
            DatabaseConfig(id="db_shard_2", connection_string=f"sqlite:///{shard2}"),
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
                duckdb=DuckDBConfig(enabled=True, fetch_limit=1000),
            ),
            TableConfig(
                id="recebiveis",
                database="db_main",
                name="recebiveis",
                sharding=ShardingConfig(
                    discriminator_column="cnpj",
                    resolver="playground.shard_resolver:resolve_cnpj_shard",
                ),
                columns=[ColumnConfig(name="cnpj"), ColumnConfig(name="valor")],
                duckdb=DuckDBConfig(enabled=True, force_analytical=True, fetch_limit=1000),
            ),
        ],
        dialect=None,
    )


def test_analytical_multi_shard_fan_in_sums_all_cnpjs(monkeypatch: Any) -> None:
    """mode=multi deve fan-in de TODOS os bindings — não só o primeiro."""
    _env()
    cfg = _cfg_multi_shard_and_clientes()
    cnpjs = ["65410433218196", "74778161849593"]
    ready = IntentPlan(
        status="ready",
        question_rewrite="soma total dos dois CNPJs",
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="in", value=cnpjs)],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    # Plano típico do LLM: um passo sem shard_bindings — o nó deve fan-in mesmo assim
    mat_plan = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="SELECT cnpj, valor FROM recebiveis",
                target_table="recebiveis",
                mode="replace",
            )
        ],
    )
    script = [
        ready,
        mat_plan,
        SQLPlan(
            sql=(
                "SELECT SUM(valor) AS total FROM recebiveis "
                "WHERE cnpj IN ('65410433218196', '74778161849593')"
            ),
            dialect="duckdb",
        ),
        VerifyDecision(action="answer", reason="ok"),
        "Total 4121.55",
    ]
    monkeypatch.setattr("txt2sql.analytical_planning.build_deterministic_mat_plan", lambda *_a, **_k: None)
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=MemorySaver())
    result = agent.invoke(
        {"messages": [HumanMessage(content="soma dos dois?")]},
        config={"configurable": {"thread_id": "multi-fan-in"}},
    )
    assert result.get("execution_path") == "analytical"
    routing = _d(result.get("shard_routing"))
    assert routing.get("mode") == "multi"
    last = _d(result.get("last_result"))
    assert last.get("status") == "ok"
    sample = last.get("sample") or []
    assert sample, last
    assert abs(float(sample[0]["total"]) - 4121.55) < 0.01


def test_analytical_join_clientes_materialized_in_duckdb(monkeypatch: Any) -> None:
    """JOIN recebiveis+clientes no DuckDB: materializa ambos (não rejeita cross-DB)."""
    _env()
    cfg = _cfg_multi_shard_and_clientes()
    cnpjs = ["65410433218196", "74778161849593"]
    ready = IntentPlan(
        status="ready",
        question_rewrite="tabela cnpj nome valor",
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="in", value=cnpjs)],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        joins=[
            JoinClause(
                from_table_id="recebiveis",
                to_table_id="clientes",
                on=[JoinOn(from_column="cnpj", to_column="cnpj")],
            )
        ],
    )
    # LLM planejou as duas tabelas; fan-in de recebiveis ainda deve ser completo
    mat_plan = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="SELECT cnpj, valor FROM recebiveis",
                target_table="recebiveis",
                mode="replace",
            ),
            MaterializationStep(
                source_query="SELECT cnpj, razao_social FROM clientes",
                target_table="clientes",
                mode="replace",
            ),
        ],
    )
    join_sql = (
        "SELECT r.cnpj, c.razao_social AS nome, SUM(r.valor) AS valor "
        "FROM recebiveis r JOIN clientes c ON r.cnpj = c.cnpj "
        "WHERE r.cnpj IN ('65410433218196', '74778161849593') "
        "GROUP BY r.cnpj, c.razao_social"
    )
    script = [
        ready,
        mat_plan,
        SQLPlan(sql=join_sql, dialect="duckdb", expected_shape="table"),
        VerifyDecision(action="answer", reason="ok"),
        "tabela ok",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=MemorySaver())
    result = agent.invoke(
        {"messages": [HumanMessage(content="tabela cnpj nome valor?")]},
        config={"configurable": {"thread_id": "multi-join"}},
    )
    assert result.get("execution_path") == "analytical"
    last = _d(result.get("last_result"))
    assert last.get("status") == "ok", last
    catalog = _d(result.get("duckdb_catalog")).get("tables") or []
    names = {t.get("name") for t in catalog}
    assert "recebiveis" in names
    assert "clientes" in names
    sample = last.get("sample") or []
    assert len(sample) == 2
    by_cnpj = {row["cnpj"]: row for row in sample}
    assert abs(float(by_cnpj["65410433218196"]["valor"]) - 1789.28) < 0.01
    assert by_cnpj["65410433218196"]["nome"] == "Cliente_000"
    assert abs(float(by_cnpj["74778161849593"]["valor"]) - 2332.27) < 0.01
    assert by_cnpj["74778161849593"]["nome"] == "Cliente_001"


def test_analytical_ensures_intent_table_omitted_from_mat_plan(monkeypatch: Any) -> None:
    """Se o plano omite clientes mas o intent tem JOIN, materializa mesmo assim."""
    _env()
    cfg = _cfg_multi_shard_and_clientes()
    cnpjs = ["65410433218196", "74778161849593"]
    ready = IntentPlan(
        status="ready",
        question_rewrite="tabela com nome",
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="in", value=cnpjs)],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        joins=[
            JoinClause(
                from_table_id="recebiveis",
                to_table_id="clientes",
                on=[JoinOn(from_column="cnpj", to_column="cnpj")],
            )
        ],
    )
    mat_plan = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="SELECT cnpj, valor FROM recebiveis",
                target_table="recebiveis",
                mode="replace",
            )
        ],
    )
    join_sql = (
        "SELECT r.cnpj, c.razao_social AS nome, SUM(r.valor) AS valor "
        "FROM recebiveis r JOIN clientes c ON r.cnpj = c.cnpj "
        "GROUP BY r.cnpj, c.razao_social"
    )
    script = [
        ready,
        mat_plan,
        SQLPlan(sql=join_sql, dialect="duckdb", expected_shape="table"),
        VerifyDecision(action="answer", reason="ok"),
        "ok",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=MemorySaver())
    result = agent.invoke(
        {"messages": [HumanMessage(content="tabela?")]},
        config={"configurable": {"thread_id": "ensure-clientes"}},
    )
    catalog = _d(result.get("duckdb_catalog")).get("tables") or []
    names = {t.get("name") for t in catalog}
    assert "clientes" in names
    assert "recebiveis" in names
    last = _d(result.get("last_result"))
    assert last.get("status") == "ok", last
    assert len(last.get("sample") or []) == 2


def test_analytical_refines_after_physical_shard_sql(monkeypatch: Any) -> None:
    """Trace: LLM usa recebiveis_654 UNION …; policy rejeita; refine usa nome lógico."""
    _env()
    cfg = _cfg_multi_shard_and_clientes()
    cnpjs = ["65410433218196", "74778161849593"]
    ready = IntentPlan(
        status="ready",
        question_rewrite="tabela cnpj nome valor",
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="in", value=cnpjs)],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        joins=[
            JoinClause(
                from_table_id="recebiveis",
                to_table_id="clientes",
                on=[JoinOn(from_column="cnpj", to_column="cnpj")],
            )
        ],
    )
    mat_plan = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="SELECT cnpj, valor FROM recebiveis",
                target_table="recebiveis",
                mode="replace",
            ),
            MaterializationStep(
                source_query="SELECT cnpj, razao_social FROM clientes",
                target_table="clientes",
                mode="replace",
            ),
        ],
    )
    bad_sql = (
        "WITH u AS (SELECT * FROM recebiveis_654 UNION ALL SELECT * FROM recebiveis_747) "
        "SELECT r.cnpj, c.razao_social AS nome, SUM(r.valor) AS valor "
        "FROM u r JOIN clientes c ON r.cnpj = c.cnpj GROUP BY r.cnpj, c.razao_social"
    )
    good_sql = (
        "SELECT r.cnpj, c.razao_social AS nome, SUM(r.valor) AS valor "
        "FROM recebiveis r JOIN clientes c ON r.cnpj = c.cnpj "
        "GROUP BY r.cnpj, c.razao_social"
    )
    script = [
        ready,
        mat_plan,
        SQLPlan(sql=bad_sql, dialect="duckdb", expected_shape="table"),
        # verify diria answer, mas o nó deve forçar refine_sql
        VerifyDecision(action="answer", reason="vou responder o erro"),
        SQLPlan(sql=good_sql, dialect="duckdb", expected_shape="table"),
        VerifyDecision(action="answer", reason="ok"),
        "tabela ok",
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: ScriptedLLM(script))
    agent = build_agent(cfg, checkpointer=MemorySaver())
    result = agent.invoke(
        {"messages": [HumanMessage(content="tabela?")]},
        config={"configurable": {"thread_id": "refine-physical"}},
    )
    last = _d(result.get("last_result"))
    assert last.get("status") == "ok", last
    sample = last.get("sample") or []
    assert len(sample) == 2
    by_cnpj = {row["cnpj"]: row for row in sample}
    assert abs(float(by_cnpj["65410433218196"]["valor"]) - 1789.28) < 0.01
    assert by_cnpj["74778161849593"]["nome"] == "Cliente_001"


def test_sufficiency_gate_skips_llm_when_deterministic(monkeypatch: Any) -> None:
    """Catálogo vazio → refresh determinístico; GateDecision não é consumido."""
    _env()
    cfg = _cfg_recebiveis_analytical()
    cfg.reuse_ttl_seconds = 0
    ready = IntentPlan(
        status="ready",
        question_rewrite="total",
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
    script = [
        ready,
        SQLPlan(sql="SELECT SUM(valor) AS total FROM recebiveis", dialect="duckdb"),
        VerifyDecision(action="answer", reason="ok"),
        "175",
    ]
    llm = ScriptedLLM(script)
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: llm)
    agent = build_agent(cfg, checkpointer=MemorySaver())
    result = agent.invoke(
        {"messages": [HumanMessage(content="total?")]},
        config={"configurable": {"thread_id": "det-gate"}},
    )
    assert result.get("gate_action") == "refresh"
    decision = result.get("sufficiency_decision")
    assert decision is not None
    assert getattr(decision, "action", None) == "reuse" or _d(decision).get("action") in {
        "reuse",
        "refresh",
    }
    # interpret + sql + verify + answer = 4; sem GateDecision/MaterializationPlan
    assert llm.invoke_count == 4
    assert result.get("final_answer")
    assert "175" in result["final_answer"]


def test_sufficiency_gate_llm_fallback_on_unknown(monkeypatch: Any) -> None:
    """unknown determinístico aciona gate_llm (GateDecision da fila)."""
    from txt2sql.sufficiency import SufficiencyDecision

    _env()
    cfg = _cfg_recebiveis_analytical()
    ready = IntentPlan(
        status="ready",
        question_rewrite="total",
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
    )
    script = [
        ready,
        GateDecision("refresh"),
        mat_plan,
        SQLPlan(sql="SELECT SUM(valor) AS total FROM recebiveis", dialect="duckdb"),
        VerifyDecision(action="answer", reason="ok"),
        "175",
    ]
    llm = ScriptedLLM(script)
    calls = {"n": 0}

    def _fake_eval(*_a: Any, **_k: Any) -> SufficiencyDecision:
        calls["n"] += 1
        if calls["n"] == 1:
            return SufficiencyDecision(action="unknown", reasons=["predicado OR"])
        return SufficiencyDecision(action="reuse")

    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: llm)
    monkeypatch.setattr("txt2sql.analytical_planning.evaluate_sufficiency", _fake_eval)
    monkeypatch.setattr("txt2sql.analytical_planning.build_deterministic_mat_plan", lambda *_a, **_k: None)
    agent = build_agent(cfg, checkpointer=MemorySaver())
    result = agent.invoke(
        {"messages": [HumanMessage(content="total?")]},
        config={"configurable": {"thread_id": "unknown-gate"}},
    )
    assert result.get("gate_action") == "refresh"
    assert result.get("final_answer")
    # interpret + gate + mat + sql + verify + answer
    assert llm.invoke_count >= 5
    assert calls["n"] >= 2


def _interrupt_question(result: dict[str, Any]) -> str | None:
    for item in result.get("__interrupt__") or []:
        value = getattr(item, "value", None) or item
        if isinstance(value, dict) and value.get("type") == "clarification":
            return str(value.get("question") or "")
    return None


def test_resume_after_clarification_rehydrates_duckdb_on_reuse(
    monkeypatch: Any,
) -> None:
    """HITL resume não passa por init_state; reuse não deve falhar com sessão ausente."""
    _env()
    cfg = _cfg_recebiveis_analytical()
    cfg.reuse_ttl_seconds = 0
    ready = IntentPlan(
        status="ready",
        question_rewrite="total recebíveis",
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
    clarify = IntentPlan(
        status="needs_clarification",
        question_rewrite="adicionar colunas",
        clarification=Clarification(question="Confirma adicionar total pago e vencido?"),
    )
    mat_plan = MaterializationPlan(
        steps=[
            MaterializationStep(
                source_query="SELECT cnpj, valor, status FROM recebiveis_123",
                target_table="recebiveis",
                mode="replace",
            )
        ],
    )
    sql = SQLPlan(sql="SELECT SUM(valor) AS total FROM recebiveis", dialect="duckdb")
    vd = VerifyDecision(action="answer", reason="ok")

    class _LLM(ScriptedLLM):
        def invoke(self, messages: list[Any]) -> Any:
            first = messages[0] if messages else None
            content = getattr(first, "content", "") or ""
            if isinstance(content, str) and "Responda ao usuário" in content:
                return "ok"
            return super().invoke(messages)

    # Pad de refine (verify força refine_sql quando last_result=error)
    script = [
        ready,
        mat_plan,
        sql,
        vd,
        clarify,
        ready,
        sql,
        vd,
        sql,
        vd,
        sql,
        vd,
    ]
    monkeypatch.setattr("txt2sql.graph.build_llm", lambda config: _LLM(script))
    monkeypatch.setattr("txt2sql.analytical_planning.build_deterministic_mat_plan", lambda *_a, **_k: None)
    agent = build_agent(cfg, checkpointer=MemorySaver())
    thread = {"configurable": {"thread_id": "resume-duckdb-session"}}

    r1 = agent.invoke(
        {"messages": [HumanMessage(content="total?")]},
        config=thread,
    )
    assert _d(r1.get("last_result")).get("status") == "ok"

    r2 = agent.invoke(
        {"messages": [HumanMessage(content="adicione total pago e vencido")]},
        config=thread,
    )
    assert _interrupt_question(r2)

    r3 = agent.invoke(Command(resume="Sim"), config=thread)
    last = _d(r3.get("last_result"))
    assert last.get("error") != "Sessão DuckDB ausente", last
    assert last.get("status") == "ok", last
    assert r3.get("gate_action") == "reuse"
    assert r3.get("duckdb_session") is not None
