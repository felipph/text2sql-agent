# P0: answer grounded + sync filters pós-cap + skip retry com lookup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Corrigir o trace `b137b86c…`: answer não pode negar `last_result` ok; filters alinhados ao cap; sem retry ruidoso quando lookup existe.

**Architecture:** Ajustes em `ensure_discriminator_filters`, `interpret_intent` (skip retry se `find_lookup_source`), e nó `answer` (contexto limpo + grounding fail-closed).

**Tech Stack:** Python, pytest, LangGraph

**Spec informal (aprovado):** P0.1–P0.4 da análise do trace.

---

### Task 1: Sync filters com bindings (pós-cap)

**Files:** `txt2sql/shard_routing.py`, `tests/test_shard_routing.py`

- [x] Teste: filters com 5 CNPJs + routing capped 2 → filters ficam com 2
- [x] `ensure_discriminator_filters` **substitui** FilterClause do discriminador pelos valores dos bindings

### Task 2: Skip intent retry quando lookup disponível

**Files:** `txt2sql/graph.py`, `tests/test_graph_dual_path.py`

- [x] Teste: intent sem filter + relationship → sem SystemMessage de retry; vai a analytical via lookup
- [x] Em `interpret_intent`, se `disc_errors` e `find_lookup_source` → `resolve_and_route` direto

### Task 3: Answer grounded + histórico limpo

**Files:** `txt2sql/graph.py` (+ helper testável), `tests/test_answer_grounding.py`

- [x] Helpers: filtrar SystemMessages de retry de discriminador; detectar contradição ok-vs-falha
- [x] Prompt: se `last_result.status=ok`, responder com dados; nunca narrar falha de shard
- [x] Fallback determinístico: se LLM contradiz, montar resposta a partir do `sample`
- [x] Suite verde
