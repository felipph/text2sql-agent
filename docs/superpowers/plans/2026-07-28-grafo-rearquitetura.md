# Rearquitetura do grafo Text2SQL — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Status (2026-07-28):** Tasks 1–13 executadas via subagent-driven. Dual-path disponível com `build_agent(..., dual_path=True)` (default ReAct preservado). Regressão: 107 passed, 1 skipped. Commits pendentes (só a pedido do usuário).

**Goal:** Substituir o loop ReAct+tools por grafo dual-path tipado (IntentPlan → resolve/route → simple|analytical) com Policy Gate, budgets, catálogo DuckDB reutilizável e sharding determinístico sem tools LLM.

**Architecture:** Artefatos Pydantic em módulo dedicado; `resolve_routing` + `route_execution` determinísticos; nós LLM só emitem planos; middleware envolve exec_source/materialize/exec_duckdb. Evolui `IntentPlan` existente; `force_analytical` no YAML; DuckDB por `thread_id`.

**Tech Stack:** Python 3.12+, LangGraph, Pydantic, sqlglot, SQLAlchemy, DuckDB, pytest.

**Spec / PRD:**
- `docs/prd-refatoracao-grafo-rearquitetura.md`
- `docs/superpowers/specs/2026-07-28-grafo-rearquitetura-design.md`

**Note:** Commits só se o usuário pedir. Preferir TDD. Não expandir `agent.py` indefinidamente — extrair módulos listados abaixo.

---

## File map

| File | Responsibility |
| --- | --- |
| `txt2sql/artifacts.py` | `LogicalPlan`, `SQLPlan`, `MaterializationPlan/Step`, `ExecutionResult`, `DuckDBCatalog*`, `Budget`, `VerifyDecision`, `ShardBinding`, `ShardRouting` |
| `txt2sql/config.py` | `DuckDBConfig.force_analytical` + parse + property `requires_analytical` |
| `txt2sql/shard_routing.py` | `resolve_routing(intent, config) -> ShardRouting \| ClarifySignal` |
| `txt2sql/path_routing.py` | `route_execution(intent, shard_routing, config) -> Literal["simple","analytical"]` |
| `txt2sql/policy.py` | Policy Gate (evolui/chama `guardrail.validate_sql` + volume + force_analytical + unresolved shard) |
| `txt2sql/middleware.py` | pre/post hooks: policy → timeout → execute → compact → asserts → budget |
| `txt2sql/db/session_store.py` | DuckDB session por `thread_id` (file-backed) + open/close |
| `txt2sql/intent.py` | `LogicalPlan.from_intent` (ou em artifacts importando IntentPlan) |
| `txt2sql/agent.py` | Wiring do grafo novo (ou `txt2sql/graph.py` + thin `build_agent`) |
| `txt2sql/guardrail.py` | Mantém AST SELECT; Policy Gate compõe |
| `txt2sql/query_routing.py` | Reusado por Policy Gate / resolve |
| `playground/config.yaml` | `force_analytical: true` em `recebiveis` |
| `tests/test_artifacts.py` | Serialização / Budget.exhausted / LogicalPlan.from_intent |
| `tests/test_force_analytical_config.py` | YAML force_analytical + alias always |
| `tests/test_path_routing.py` | Matriz de roteamento |
| `tests/test_shard_routing.py` | none/single/multi/missing discriminator |
| `tests/test_policy_gate.py` | S5 offline |
| `tests/test_graph_dual_path.py` | Smoke grafo com LLM falso |
| `docs/adr/0003-*.md` | Revisar lifetime DuckDB (fase final) |

---

## Fase 0 — Fundação (artefatos + config + routing puro)

### Task 1: Artefatos Pydantic

**Files:**
- Create: `txt2sql/artifacts.py`
- Create: `tests/test_artifacts.py`

- [ ] **Step 1: Teste falhando — Budget.exhausted + LogicalPlan.from_intent**

```python
"""Artefatos tipados do grafo dual-path."""

from txt2sql.artifacts import Budget, LogicalPlan
from txt2sql.intent import FilterClause, IntentPlan, MetricClause


def test_budget_exhausted_refine() -> None:
    b = Budget(refine_count=3, max_refine=3)
    assert b.exhausted("refine_count") is True
    assert b.exhausted("mat_loop_count") is False


def test_logical_plan_from_intent() -> None:
    plan = IntentPlan(
        status="ready",
        filters=[FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="1")],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
        assumptions=["ano corrente"],
        limit=100,
    )
    lp = LogicalPlan.from_intent(plan)
    assert "recebiveis" in lp.tables
    assert any("sum" in a.lower() or "valor" in a for a in lp.aggregations)
    assert lp.assumptions == ["ano corrente"]
    assert lp.limit == 100
```

- [ ] **Step 2: Rodar teste — esperar FAIL (módulo ausente)**

```bash
.venv/bin/pytest tests/test_artifacts.py -v
```

- [ ] **Step 3: Implementar `txt2sql/artifacts.py`**

Incluir no mínimo (alinhar ao PRD §3):

```python
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from txt2sql.intent import IntentPlan


class LogicalPlan(BaseModel):
    tables: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    aggregations: list[str] = Field(default_factory=list)
    limit: int | None = None
    assumptions: list[str] = Field(default_factory=list)

    @classmethod
    def from_intent(cls, intent_plan: IntentPlan) -> LogicalPlan:
        tables: set[str] = set()
        filters: list[str] = []
        aggregations: list[str] = []
        joins: list[str] = []
        for f in intent_plan.filters:
            tables.add(f.table_id)
            filters.append(f"{f.table_id}.{f.column_id} {f.op} {f.value!r}")
        for m in intent_plan.metrics:
            tables.add(m.table_id)
            if m.agg and m.agg != "none":
                aggregations.append(f"{m.agg}({m.table_id}.{m.column_id})")
            elif m.column_id:
                aggregations.append(f"{m.table_id}.{m.column_id}")
        for g in intent_plan.group_by:
            tables.add(g.table_id)
        for j in intent_plan.joins:
            tables.add(j.from_table_id)
            tables.add(j.to_table_id)
            joins.append(f"{j.from_table_id}->{j.to_table_id}")
        for e in intent_plan.entities:
            if e.table_id:
                tables.add(e.table_id)
        return cls(
            tables=sorted(tables),
            joins=joins,
            filters=filters,
            aggregations=aggregations,
            limit=intent_plan.limit,
            assumptions=list(intent_plan.assumptions),
        )


class ShardBinding(BaseModel):
    table_id: str
    discriminator_value: str
    database_id: str
    physical_table: str


class ShardRouting(BaseModel):
    mode: Literal["none", "single", "multi"] = "none"
    bindings: list[ShardBinding] = Field(default_factory=list)
    logical_table: str | None = None


class SQLPlan(BaseModel):
    sql: str
    dialect: Literal["postgres", "duckdb"]
    params: dict = Field(default_factory=dict)
    expected_shape: Literal["scalar", "row", "table"] = "table"


class MaterializationStep(BaseModel):
    source_query: str
    target_table: str
    mode: Literal["create", "append", "replace"] = "replace"
    estimated_rows: int | None = None
    shard_binding: ShardBinding | None = None
    shard_bindings: list[ShardBinding] = Field(default_factory=list)


class MaterializationPlan(BaseModel):
    steps: list[MaterializationStep]
    rationale: str = ""


class ExecutionResult(BaseModel):
    status: Literal["ok", "error", "rejected", "timeout"]
    row_count: int = 0
    schema_: list[dict] = Field(default_factory=list, alias="schema")
    sample: list[dict] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    truncated: bool = False
    full_result_ref: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class DuckDBTableInfo(BaseModel):
    name: str
    schema_: list[dict] = Field(default_factory=list, alias="schema")
    row_count: int = 0
    source_queries: list[str] = Field(default_factory=list)
    covered_filters: list[str] = Field(default_factory=list)
    shard_bindings: list[ShardBinding] = Field(default_factory=list)
    materialized_at: datetime | None = None

    model_config = {"populate_by_name": True}


class DuckDBCatalog(BaseModel):
    tables: list[DuckDBTableInfo] = Field(default_factory=list)


class Budget(BaseModel):
    refine_count: int = 0
    max_refine: int = 3
    mat_loop_count: int = 0
    max_mat_loops: int = 3
    gate_visits: int = 0
    max_gate_visits: int = 2
    total_rows_materialized: int = 0
    max_rows_materialized: int = 2_000_000
    max_rows_per_extract: int = 500_000
    sample_rows: int = 20

    def exhausted(self, counter: str) -> bool:
        mapping = {
            "refine_count": ("refine_count", "max_refine"),
            "mat_loop_count": ("mat_loop_count", "max_mat_loops"),
            "gate_visits": ("gate_visits", "max_gate_visits"),
            "total_rows_materialized": ("total_rows_materialized", "max_rows_materialized"),
        }
        cur_name, max_name = mapping[counter]
        return getattr(self, cur_name) >= getattr(self, max_name)


class VerifyDecision(BaseModel):
    action: Literal["answer", "refine_sql", "data_gap"]
    reason: str = ""
```

- [ ] **Step 4: Rodar testes — PASS**

```bash
.venv/bin/pytest tests/test_artifacts.py -v
```

---

### Task 2: `force_analytical` no YAML

**Files:**
- Modify: `txt2sql/config.py` (`DuckDBConfig`, `_parse_duckdb`, opcional property em `TableConfig`)
- Create: `tests/test_force_analytical_config.py`
- Modify: `playground/config.yaml` (recebiveis)

- [ ] **Step 1: Testes falhando**

```python
from pathlib import Path

import pytest

from txt2sql.config import DuckDBConfig, load_config


def test_force_analytical_explicit(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db\n"
        "    connection_string: sqlite:///:memory:\n"
        "tables:\n"
        "  - id: t1\n"
        "    database: db\n"
        "    name: t1\n"
        "    duckdb:\n"
        "      enabled: true\n"
        "      force_analytical: true\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.tables[0].duckdb.force_analytical is True
    assert cfg.tables[0].requires_analytical is True


def test_trigger_always_aliases_force_analytical(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db\n"
        "    connection_string: sqlite:///:memory:\n"
        "tables:\n"
        "  - id: t1\n"
        "    database: db\n"
        "    name: t1\n"
        "    duckdb:\n"
        "      enabled: true\n"
        "      trigger: always\n",
        encoding="utf-8",
    )
    d = load_config(p).tables[0].duckdb
    assert d.force_analytical is True


def test_duckdb_config_force_analytical_default_false() -> None:
    assert DuckDBConfig(enabled=True).force_analytical is False
```

- [ ] **Step 2: Rodar — FAIL**

```bash
.venv/bin/pytest tests/test_force_analytical_config.py -v
```

- [ ] **Step 3: Implementar**

Em `DuckDBConfig`:

```python
force_analytical: bool = False

def __post_init__(self) -> None:
    if self.trigger not in self._VALID_TRIGGERS:
        raise ValueError(...)
    if self.trigger == "always":
        object.__setattr__(self, "force_analytical", True)  # se frozen; senão self.force_analytical = True
```

Em `_parse_duckdb`:

```python
force = bool(raw.get("force_analytical", False))
trigger = raw.get("trigger", "aggregation")
if trigger == "always":
    force = True
return DuckDBConfig(enabled=..., trigger=trigger, fetch_limit=..., force_analytical=force)
```

Em `TableConfig`:

```python
@property
def requires_analytical(self) -> bool:
    return bool(self.duckdb and self.duckdb.enabled and self.duckdb.force_analytical)
```

Playground `recebiveis.duckdb`:

```yaml
force_analytical: true
# manter fetch_limit; trigger pode ficar aggregation ou always
```

- [ ] **Step 4: PASS + docs config (`docs/guias/configuracao.md` — uma linha na tabela duckdb)**

---

### Task 3: `route_execution` determinístico

**Files:**
- Create: `txt2sql/path_routing.py`
- Create: `tests/test_path_routing.py`

- [ ] **Step 1: Testes da matriz**

```python
from txt2sql.artifacts import ShardRouting
from txt2sql.config import AgentConfig, DatabaseConfig, DuckDBConfig, TableConfig
from txt2sql.intent import GroupByClause, IntentPlan, MetricClause
from txt2sql.path_routing import route_execution


def _cfg(*tables: TableConfig) -> AgentConfig:
    return AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite:///:memory:")],
        tables=list(tables),
    )


def test_force_analytical_forces_path() -> None:
    t = TableConfig(
        id="recebiveis", database="db", name="recebiveis",
        duckdb=DuckDBConfig(enabled=True, force_analytical=True),
    )
    plan = IntentPlan(metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="none")])
    assert route_execution(plan, ShardRouting(mode="none"), _cfg(t)) == "analytical"


def test_multi_shard_forces_analytical() -> None:
    t = TableConfig(id="recebiveis", database="db", name="recebiveis")
    plan = IntentPlan()
    assert route_execution(plan, ShardRouting(mode="multi"), _cfg(t)) == "analytical"


def test_agg_on_duckdb_enabled_analytical() -> None:
    t = TableConfig(
        id="recebiveis", database="db", name="recebiveis",
        duckdb=DuckDBConfig(enabled=True, trigger="aggregation"),
    )
    plan = IntentPlan(metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")])
    assert route_execution(plan, ShardRouting(mode="none"), _cfg(t)) == "analytical"


def test_plain_lookup_simple() -> None:
    t = TableConfig(id="clientes", database="db", name="clientes")
    plan = IntentPlan(metrics=[MetricClause(table_id="clientes", column_id="cnpj", agg="none")])
    assert route_execution(plan, ShardRouting(mode="none"), _cfg(t)) == "simple"
```

- [ ] **Step 2: FAIL → implementar**

```python
# txt2sql/path_routing.py
from __future__ import annotations

from typing import Literal

from txt2sql.artifacts import ShardRouting
from txt2sql.config import AgentConfig
from txt2sql.intent import IntentPlan

ExecutionPath = Literal["simple", "analytical"]


def _touched_table_ids(plan: IntentPlan) -> set[str]:
    ids: set[str] = set()
    for xs in (plan.filters, plan.metrics, plan.group_by, plan.order_by):
        for x in xs:
            ids.add(x.table_id)
    for j in plan.joins:
        ids.add(j.from_table_id)
        ids.add(j.to_table_id)
    for e in plan.entities:
        if e.table_id:
            ids.add(e.table_id)
    return ids


def route_execution(
    intent_plan: IntentPlan,
    shard_routing: ShardRouting,
    config: AgentConfig,
) -> ExecutionPath:
    if shard_routing.mode == "multi":
        return "analytical"
    touched = _touched_table_ids(intent_plan)
    for tid in touched:
        table = config.get_table(tid) if hasattr(config, "get_table") else None
        if table is None:
            continue
        if table.requires_analytical:
            return "analytical"
        if table.uses_duckdb:
            has_agg = any(
                m.table_id == tid and m.agg and m.agg != "none"
                for m in intent_plan.metrics
            )
            has_gb = any(g.table_id == tid for g in intent_plan.group_by)
            if has_agg or has_gb:
                return "analytical"
    return "simple"
```

- [ ] **Step 3: PASS**

```bash
.venv/bin/pytest tests/test_path_routing.py -v
```

---

### Task 4: `resolve_routing` determinístico

**Files:**
- Create: `txt2sql/shard_routing.py`
- Create: `tests/test_shard_routing.py`
- Reuse: resolvers / `ShardResult` de `config.py`

- [ ] **Step 1: Testes**

```python
from dataclasses import dataclass

import pytest

from txt2sql.artifacts import ShardRouting
from txt2sql.config import (
    AgentConfig,
    DatabaseConfig,
    ShardResult,
    ShardingConfig,
    TableConfig,
)
from txt2sql.intent import FilterClause, IntentPlan
from txt2sql.shard_routing import ClarifyNeeded, resolve_routing


def _resolver_factory(mapping: dict[str, ShardResult]):
    def _resolve(value: str) -> ShardResult:
        return mapping[value]
    return _resolve


def test_non_sharded_none() -> None:
    cfg = AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite:///:memory:")],
        tables=[TableConfig(id="clientes", database="db", name="clientes")],
    )
    out = resolve_routing(IntentPlan(), cfg)
    assert isinstance(out, ShardRouting)
    assert out.mode == "none"


def test_sharded_missing_discriminator_clarify() -> None:
    # table sharded, intent sem filter no discriminator → ClarifyNeeded
    ...


def test_sharded_one_value_single() -> None:
    ...


def test_sharded_two_values_multi() -> None:
    ...
```

Completar os três casos com `ShardingConfig` + monkeypatch `load_resolver` ou injetar callable via parâmetro opcional `resolvers: dict[str, Callable]` no `resolve_routing` para testabilidade:

```python
def resolve_routing(
    intent_plan: IntentPlan,
    config: AgentConfig,
    resolvers: dict[str, Callable[[str], ShardResult]] | None = None,
) -> ShardRouting | ClarifyNeeded:
    ...
```

`ClarifyNeeded` dataclass: `table_id: str`, `discriminator_column: str`, `question: str`.

Regras (PRD): extrair valores do discriminador a partir de `filters` com `column_id == discriminator_column` e `op in (eq, in)`; 0 → ClarifyNeeded; 1 → single; 2+ → multi (cap `max_shard_discriminators`).

- [ ] **Step 2: FAIL → implement → PASS**

```bash
.venv/bin/pytest tests/test_shard_routing.py -v
```

---

## Fase 1 — Policy Gate + ExecutionResult helpers

### Task 5: Policy Gate (S5 offline)

**Files:**
- Create: `txt2sql/policy.py`
- Create: `tests/test_policy_gate.py`
- Keep: `txt2sql/guardrail.py` (chamado internamente)

- [ ] **Step 1: Testes S5**

```python
from txt2sql.artifacts import ShardRouting, SQLPlan
from txt2sql.policy import PolicyDecision, check_sql_plan


def test_rejects_dml() -> None:
    plan = SQLPlan(sql="DELETE FROM clientes", dialect="postgres")
    d = check_sql_plan(plan, config=..., shard_routing=ShardRouting(), path="simple")
    assert d.status == "rejected"


def test_rejects_unresolved_sharded_logical_name() -> None:
    # SQL usa nome lógico shardado sem bindings
    ...


def test_rejects_aggregation_on_source_when_force_analytical() -> None:
    plan = SQLPlan(
        sql="SELECT cnpj, SUM(valor) FROM recebiveis_001 GROUP BY cnpj",
        dialect="postgres",
    )
    # table force_analytical / path extract context
    d = check_sql_plan(..., context="source_extract")
    assert d.status == "rejected"
    assert "force_analytical" in (d.error or "").lower() or "pushdown" in (d.error or "").lower()


def test_injects_limit_when_missing() -> None:
    ...
```

`PolicyDecision`: `status: ok|rejected`, `sql: str` (possivelmente reescrito), `error: str | None`.

- [ ] **Step 2: Implementar `check_sql_plan` compondo `validate_sql` + `routing_rejection_reason` + heurística sqlglot de agg em `context=="source_extract"` quando tabelas `requires_analytical`.**

- [ ] **Step 3: PASS**

```bash
.venv/bin/pytest tests/test_policy_gate.py -v
```

---

### Task 6: Helper de timeout → ExecutionResult

**Files:**
- Modify: `txt2sql/db/registry.py` (já tem `QueryTimeoutError`)
- Create: helper em `txt2sql/middleware.py` ou `artifacts` factory
- Test: estender `tests/test_query_timeout.py` ou novo `tests/test_execution_result_timeout.py`

- [ ] **Step 1: Teste**

```python
from txt2sql.artifacts import ExecutionResult
from txt2sql.middleware import result_from_timeout


def test_result_from_timeout() -> None:
    r = result_from_timeout("query exceeded 30s")
    assert r.status == "timeout"
    assert r.error
```

- [ ] **Step 2: Implementar stub de middleware com factories `result_from_timeout`, `result_from_rejection`, `compact_result(rows, budget) -> ExecutionResult` (compact pode ser stub que só corta sample na Fase 1; full ref na Fase 3).**

---

## Fase 2 — Grafo dual-path + sessão DuckDB

### Task 7: Session store DuckDB por thread_id

**Files:**
- Create: `txt2sql/db/session_store.py`
- Create: `tests/test_session_store.py`

- [ ] API:

```python
class DuckDBSessionStore:
    def __init__(self, root_dir: Path): ...
    def get(self, thread_id: str) -> DuckDBSession: ...
    def close(self, thread_id: str) -> None: ...
```

Arquivo: `{root_dir}/{safe_thread_id}.duckdb`. Teste: get duas vezes mesma id → mesmo conteúdo após CREATE TABLE.

Revisar `init_turn` para **não** descartar sessão a cada turno quando `thread_id` presente; catalog no state.

- [ ] Atualizar ADR-0003 em task separada no fim (Task 12).

---

### Task 8: Extrair / rewire `build_agent` para topologia PRD

**Files:**
- Prefer: Create `txt2sql/graph.py` com nodes + `build_graph`; `build_agent` em `agent.py` delega
- Modify: prompts para generate_sql / analytical / gate / verify / answer (structured where needed)
- Test: `tests/test_graph_dual_path.py` com LLM falso (padrão de `smoke_test_graph.py` / `test_intent_graph.py`)

- [ ] **Step 1: Smoke — path simple**

Pergunta sobre `clientes` (sem duckdb) → passa por generate_sql → exec_source (mock) → verify(answer) → final_answer com provenance.

- [ ] **Step 2: Smoke — force_analytical**

Intent tocando `recebiveis` → path analytical → gate refresh → plan_mat → materialize (mock) → analytical sql → duckexec → answer.

- [ ] **Step 3: Smoke — missing discriminator → interrupt/clarify**

- [ ] **Step 4: Verify data_gap vs refine_sql**

Fake verify decisions e assert arestas.

Implementação: portar `interpret_intent` / `ask_clarification` existentes; remover dependência de tools `resolve_shard` / `materialize_sharded_table` / `sql_db_query` no path novo (manter código antigo atrás de flag só se necessário para migração — YAGNI: trocar de uma vez se smoke cobrir).

Reusar `materialize_sharded_values` e `DuckDBSession.materialize` dentro do nó `materialize`.

---

### Task 9: Wire Policy Gate + timeout nos nós determinísticos

**Files:**
- `txt2sql/middleware.py` + nodes em `graph.py`

Cada nó determinístico:

```python
def exec_source(state):
    decision = check_sql_plan(...)
    if decision.status == "rejected":
        return {"last_result": result_from_rejection(decision.error)}
    try:
        rows = registry.execute(...)
    except QueryTimeoutError as e:
        return {"last_result": result_from_timeout(str(e))}
    return {"last_result": compact_result(rows, state["budget"]), "executed_sql_history": [...]}
```

Mesmo padrão em `materialize` (timeout no extract) e `exec_duckdb`.

Testes: rejected volta ao gerador (aresta); timeout não derruba o grafo.

---

## Fase 3 — Compactor + asserts

### Task 10: Result Compactor + S6

**Files:**
- `txt2sql/middleware.py` — `compact_result`
- `tests/test_compactor.py`

- sample ≤ `budget.sample_rows`
- overflow → tabela no DuckDB session + `full_result_ref`
- `EMPTY_RESULT` warning se IntentPlan tinha metrics e row_count==0

---

## Fase 4 — Harness + docs + ADR

### Task 11: Harness S7 (esqueleto)

**Files:**
- Create: `tests/harness/` ou `scripts/harness_s7.py`
- 10–15 perguntas (playground); métricas sucesso/latência/rejeição — **sem** exact-match SQL
- Pode começar com 3 perguntas e LLM real opcional (skip se sem credenciais)

### Task 12: Docs + ADR-0003

- Revisar `docs/adr/0003-duckdb-intermediario-por-turno.md` → sessão por thread_id + Gate reuse
- Atualizar `docs/arquitetura.md` com topologia nova
- `docs/guias/configuracao.md` — `force_analytical`
- `AGENTS.md` — bullet: path dual + resolve_routing (não tools shard)

### Task 13: Regressão

```bash
.venv/bin/pytest tests/ -v
.venv/bin/ruff check .
.venv/bin/python smoke_test_graph.py
```

Corrigir quebras do playground (`playground/app.py` — HITL / provenance) se a API de invoke mudar.

---

## Ordem de execução sugerida

1 → 2 → 3 → 4 (Fase 0, tudo unitário) → 5 → 6 (Fase 1) → 7 → 8 → 9 (Fase 2) → 10 → 11 → 12 → 13

Cada task = PR/commit lógico se o usuário pedir commits.

---

## Spec coverage checklist

| Requisito PRD | Task |
|---|---|
| Artefatos / Budget / LogicalPlan | 1 |
| force_analytical YAML | 2 |
| route_execution | 3 |
| resolve_routing / ClarifyNeeded | 4 |
| Policy Gate S5 | 5 |
| Timeout tipado | 6, 9 |
| DuckDB thread_id + catalog reuse | 7, 8 |
| Dual path graph + verify 2 níveis | 8 |
| Middleware nos 3 nós | 9 |
| Compactor S6 | 10 |
| Harness S7 | 11 |
| ADR-0003 / arquitetura | 12 |
| US-15 / US-16 | 2, 4, 5, 8 |
