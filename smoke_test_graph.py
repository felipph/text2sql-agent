"""Smoke test do fluxo completo do grafo com um LLM falso (scriptado).

Verifica a orquestração dos nós sem depender de um provider real: um modelo
falso emite tool calls scriptadas (resolve_shard → sql_db_query com agregação,
roteando pela camada DuckDB) e o grafo executa de ponta a ponta.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

import txt2sql.agent as agent_mod
from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    DatabaseConfig,
    DuckDBConfig,
    ShardingConfig,
    TableConfig,
)
from txt2sql.intent import FilterClause, IntentPlan, MetricClause

FAILS = 0


def check(name: str, cond: bool) -> None:
    global FAILS
    if not cond:
        FAILS += 1
    print(f"[{'OK ' if cond else 'FALHOU'}] {name}")


class ScriptedLLM:
    """LLM falso que devolve IntentPlan + AIMessages scriptados."""

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


# --------------------------------------------------------------------------- #
# Dados: um shard com recebíveis volumétricos
# --------------------------------------------------------------------------- #
tmp = tempfile.mkdtemp()
shard1 = Path(tmp) / "shard1.db"
c = sqlite3.connect(shard1)
c.executescript(
    "CREATE TABLE recebiveis_123 (cnpj TEXT, valor REAL, status TEXT);"
    "INSERT INTO recebiveis_123 VALUES "
    "('12345678000190', 100.0, 'pago'),"
    "('12345678000190', 50.0, 'pendente'),"
    "('12345678000190', 25.0, 'pago');"
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
                resolver="examples.shard_resolver_example:resolve_cnpj_shard",
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

# Script: 0) IntentPlan  1) resolve_shard  2) sql_db_query  3) resposta final
ready = IntentPlan(
    status="ready",
    question_rewrite="total de recebíveis do CNPJ",
    filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="12345678000190")],
    metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
)
script = [
    ready,
    AIMessage(
        content="",
        tool_calls=[
            {
                "name": "resolve_shard",
                "args": {"table_id": "recebiveis", "discriminator_value": "12345678000190"},
                "id": "call_1",
            }
        ],
    ),
    AIMessage(
        content="",
        tool_calls=[
            {
                "name": "sql_db_query",
                "args": {"query": "SELECT SUM(valor) AS total FROM recebiveis_123"},
                "id": "call_2",
            }
        ],
    ),
    AIMessage(content="O total de recebíveis do CNPJ é R$ 175,00."),
]

# injeta o LLM falso
agent_mod.build_llm = lambda config: ScriptedLLM(script)  # type: ignore

agent = agent_mod.build_agent(cfg)

from langchain_core.messages import HumanMessage

final = agent.invoke(
    {"messages": [HumanMessage(content="Qual o total de recebíveis do CNPJ 12345678000190?")]}
)

msgs = final["messages"]
texts = [m.content for m in msgs]
print("\n--- Mensagens do turno ---")
for m in msgs:
    kind = type(m).__name__
    preview = (m.content or "")[:80]
    tcs = getattr(m, "tool_calls", None)
    print(f"  {kind}: {preview!r}" + (f"  tool_calls={[t['name'] for t in tcs]}" if tcs else ""))

check("resposta final presente", "175" in (msgs[-1].content or ""))
# verifica que o resultado da agregação DuckDB (175.0) apareceu num ToolMessage
tool_contents = " ".join(
    str(m.content) for m in msgs if type(m).__name__ == "ToolMessage"
)
check("agregação via DuckDB retornou 175.0", "175" in tool_contents)
check("shard resolvido (recebiveis_123 no toolmessage)", "recebiveis_123" in tool_contents)

print(f"\n=== RESULTADO: {'TODOS OS TESTES PASSARAM' if FAILS == 0 else str(FAILS)+' FALHA(S)'} ===")
raise SystemExit(1 if FAILS else 0)
