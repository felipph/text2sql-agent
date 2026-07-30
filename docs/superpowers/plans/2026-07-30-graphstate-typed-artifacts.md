# GraphState typed artifacts — Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tipar `GraphState` com modelos de `artifacts` e gravar/ler instâncias Pydantic de forma consistente.

**Architecture:** Anotações corretas no state; nós retornam modelos; helpers `_coerce_*` normalizam dict (checkpoint) → modelo.

**Tech Stack:** Python, LangGraph `MessagesState`, Pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-07-30-graphstate-typed-artifacts-design.md`

---

## Arquivos

- `txt2sql/graph.py` — `GraphState`, helpers, nós
- `tests/test_graph_dual_path.py` — assert de instâncias no state
- `playground/debug_view.py` — leitura de artefatos tipados

---

### Task 1: Teste que exige instâncias no state

- [x] Adicionar teste: após invoke, artefatos são instâncias Pydantic.
- [x] Rodar e confirmar falha (hoje eram dicts via `model_dump`).

### Task 2: Tipar GraphState + helpers

- [x] Trocar anotações `dict[str, Any]` pelos tipos de `artifacts`.
- [x] Completar `_coerce_*` / helpers para todos os artefatos tipados.
- [x] Ajustar leitores que usavam `.get` em dict.

### Task 3: Nós retornam instâncias

- [x] Remover `.model_dump()` dos returns de artefatos tipados.
- [x] Trocar acessos `last.get("status")` → atributo após coerce.

### Task 4: Verificação

- [x] Ajustar testes / playground que assumiam dict.
- [x] Suite relacionada passando (falhas de `test_guardrail_break` são pré-existentes).
