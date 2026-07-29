# Intent Interpretation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inserir nó `interpret_intent` (plano semântico + grounding + validação + HITL) antes de `generate_query`.

**Architecture:** `load_schema` → `interpret_intent` (structured output + `validate_intent`) → `ask_clarification` ou `generate_query`. Índice estruturado de colunas via `SchemaLoader.get_column_index()`. Com checkpointer: `interrupt`; sem: `AIMessage` + `END`.

**Tech Stack:** Python 3.12+, LangGraph, LangChain structured output, Pydantic, pytest.

**Note:** Commits só se o usuário pedir. Atualizar testes existentes com `ScriptedLLM` (primeiro retorno = `IntentPlan`).

---

## File map

| File | Responsibility |
| --- | --- |
| `txt2sql/intent.py` | Modelos `IntentPlan` + `validate_intent` + `ValidationResult` |
| `txt2sql/db/schema.py` | `get_column_index()` estruturado |
| `txt2sql/prompts.py` | `build_intent_prompt()` |
| `txt2sql/agent.py` | Estado, nós, rotas, injeção do plan em `generate_query` |
| `tests/test_intent.py` | Unit de validação |
| `tests/test_intent_graph.py` | Grafo com LLM fake (clarificação / ready / retry) |
| `tests/test_multi_query.py` etc. | Adaptar `ScriptedLLM` + plan ready no script |
| `docs/arquitetura.md` | Diagrama/fluxo |
| `playground/app.py` | Exibir clarificação (interrupt / AIMessage) |

---

### Task 1: Modelos + `validate_intent` (TDD)

**Files:**
- Create: `txt2sql/intent.py`
- Create: `tests/test_intent.py`

- [ ] **Step 1: Testes falhando**

```python
"""Validação programática do IntentPlan."""

from __future__ import annotations

from txt2sql.intent import (
    Clarification,
    EntityRef,
    FilterClause,
    IntentPlan,
    JoinClause,
    JoinOn,
    MetricClause,
    validate_intent,
)


INDEX = {
    "clientes": {"cnpj", "razao_social"},
    "recebiveis": {"cnpj", "valor", "status"},
}


def test_ready_valid_plan() -> None:
    plan = IntentPlan(
        status="ready",
        question_rewrite="Soma dos recebíveis do CNPJ X",
        entities=[EntityRef(mention="recebíveis", table_id="recebiveis", role="table")],
        filters=[
            FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="X"),
        ],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    result = validate_intent(plan, INDEX)
    assert result.ok
    assert result.errors == []


def test_unknown_table_fails() -> None:
    plan = IntentPlan(
        status="ready",
        question_rewrite="x",
        metrics=[MetricClause(table_id="fantasma", column_id=None, agg="count")],
    )
    result = validate_intent(plan, INDEX)
    assert not result.ok
    assert any("fantasma" in e for e in result.errors)


def test_unknown_column_fails() -> None:
    plan = IntentPlan(
        status="ready",
        question_rewrite="x",
        filters=[FilterClause(table_id="clientes", column_id="foo", op="eq", value="1")],
    )
    result = validate_intent(plan, INDEX)
    assert not result.ok


def test_bad_join_fails() -> None:
    plan = IntentPlan(
        status="ready",
        question_rewrite="x",
        joins=[
            JoinClause(
                from_table_id="clientes",
                to_table_id="recebiveis",
                on=[JoinOn(from_column="nope", to_column="cnpj")],
            )
        ],
    )
    result = validate_intent(plan, INDEX)
    assert not result.ok


def test_needs_clarification_skips_schema_checks() -> None:
    plan = IntentPlan(
        status="needs_clarification",
        question_rewrite="x",
        clarification=Clarification(question="Qual período?"),
        metrics=[MetricClause(table_id="fantasma", agg="count")],
    )
    result = validate_intent(plan, INDEX)
    assert result.ok
    assert result.needs_clarification
```

- [ ] **Step 2: Implementar `txt2sql/intent.py`** com enums/literal status, todos os submodelos do spec, `ValidationResult(ok, errors, needs_clarification)`, `validate_intent` fail-closed para `ready` e bypass de IDs quando `needs_clarification`.

- [ ] **Step 3: Rodar testes**

```bash
.venv/bin/pytest tests/test_intent.py -v
```

Expected: PASS

---

### Task 2: `SchemaLoader.get_column_index()`

**Files:**
- Modify: `txt2sql/db/schema.py`
- Modify: `tests/test_table_description.py` (ou novo teste curto em `tests/test_intent.py`)

- [ ] **Step 1: Método**

```python
def get_column_index(self) -> dict[str, set[str]]:
    """Índice {table_id: {colunas}} para validação de intent.

    Declarativo: ``TableConfig.columns``.
    Discovery: ``inspect.get_columns``; falha → set vazio (fail-closed em cols).
    """
```

- [ ] **Step 2: Teste declarativo + discovery (sqlite in-memory)** — discovery com colunas reais; tabela inacessível → `set()`.

---

### Task 3: `build_intent_prompt()`

**Files:**
- Modify: `txt2sql/prompts.py`
- Test: assert contém “IntentPlan”, “needs_clarification”, glossário se houver

---

### Task 4: Grafo — nós e rotas

**Files:**
- Modify: `txt2sql/agent.py`

Estado: `intent_plan: dict | None`, `intent_retries: int` (default 0).

Constantes: `MAX_INTENT_RETRIES = 2`.

`has_checkpointer = checkpointer is not None`.

Nós:

1. **`interpret_intent`**
   - Monta mensagens: intent system prompt + state messages (+ feedback de validação se houver).
   - `llm.with_structured_output(IntentPlan).invoke(...)`.
   - Parse fail → 1 retry; depois força `needs_clarification`.
   - `validate_intent(plan, schema_loader.get_column_index())`.
   - Se `needs_clarification` / clarificação forçada → grava plan, rota `ask_clarification`.
   - Se inválido e `intent_retries < MAX` → incrementa, SystemMessage com erros, rota self.
   - Se inválido e retries esgotados → plan com clarification genérica → `ask_clarification`.
   - Se ok → grava `intent_plan` dict → `generate_query`.

2. **`ask_clarification`**
   - Com checkpointer: `answer = interrupt({...})`; return `HumanMessage(str(answer))`; edge → `interpret_intent`.
   - Sem: return `AIMessage(question)`; edge → `END`.

3. **`generate_query`**: se `intent_plan`, prepend `SystemMessage` com JSON do plan (“traduza este intent…”).

4. **`init_turn`**: zera `intent_plan=None`, `intent_retries=0`.

5. Edges: `load_schema` → `interpret_intent`; `route_discovery` → `interpret_intent` (não mais `generate_query`).

Rotas condicionais de `interpret_intent`: `ask_clarification` | `interpret_intent` | `generate_query`.

---

### Task 5: Testes de grafo + adaptar ScriptedLLM

**Files:**
- Create: `tests/test_intent_graph.py`
- Modify: `tests/test_multi_query.py`, `tests/test_checkpointer_duckdb.py`

`ScriptedLLM` mínimo:

```python
class ScriptedLLM:
    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self._i = 0

    def bind_tools(self, tools: list[Any]) -> "ScriptedLLM":
        return self

    def with_structured_output(self, schema: Any) -> "ScriptedLLM":
        return self

    def invoke(self, messages: list[Any]) -> Any:
        msg = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return msg
```

Scripts existentes: primeiro item = `IntentPlan(status="ready", ...)` com tabelas do teste; depois os `AIMessage` de tools.

Novos testes:
- ambíguo → clarificação, sem `sql_db_query` ToolMessage;
- ready → chega em generate e responde;
- ID inválido → retry then clarification genérica.

---

### Task 6: Playground + docs

**Files:**
- Modify: `playground/app.py` — se invoke levantar/`__interrupt__` / resultado com clarificação, mostrar pergunta no chat.
- Modify: `docs/arquitetura.md` — incluir `interpret_intent` no diagrama e no fluxo.

Para interrupt no Streamlit com MemorySaver:

```python
from langgraph.types import Command

result = agent.invoke(...)
# se state próximo tem interrupt, exibir e no próximo turn:
agent.invoke(Command(resume=user_text), config=...)
```

Manter fallback: se a última mensagem for AIMessage de clarificação (sem checkpointer path), chat normal.

---

### Task 7: Verificação final

```bash
.venv/bin/pytest tests/ -v
.venv/bin/ruff check txt2sql tests playground
```

Expected: all green.

---

## Spec coverage

| Spec | Task |
|------|------|
| IntentPlan + validate | 1 |
| schema_index estruturado | 2 |
| prompt interpretador | 3 |
| nós/rotas/HITL/estado | 4 |
| testes grafo | 5 |
| playground + arquitetura | 6 |
