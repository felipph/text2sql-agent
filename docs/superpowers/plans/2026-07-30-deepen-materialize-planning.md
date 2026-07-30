# Deepen materialize + planning + limpar ReAct — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extrair materialize e analytical planning para módulos deep; remover leftovers ReAct do core.

**Architecture:** C3 limpa o pacote (`ShardResolver`/`needs_duckdb` fora; validação de DB em `resolve_routing`). C1 cria `db/materialize.py` + estende `DuckDBSession`. C2 cria `analytical_planning.py`; graph vira topologia fina nos nós analíticos.

**Tech Stack:** Python 3.12+, Pydantic v2, LangGraph, DuckDB, SQLAlchemy, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-07-30-deepen-materialize-planning-design.md`  
**Glossário:** `CONTEXT.md`

**Status:** implementado 2026-07-30 (sem commit — regra do usuário).

---

## Tasks

- [x] Task 1: `resolve_routing` valida `database_id` via `registry=`
- [x] Task 2: Apagar `ShardResolver` / `needs_duckdb`; docs + smoke + schema prompt
- [x] Task 3: `DuckDBSession.materialize(source_sql=)` + `load_rows`
- [x] Task 4: `fan_in` aceita um binding; rejeita lista vazia
- [x] Task 5: `db/materialize.py` + nó graph thin; remove `_load_rows_into_duckdb`
- [x] Task 6: `analytical_planning.py` + nós gate/plan/check thin
- [x] Task 7: Verificação — suite relacionada verde; leftovers ausentes no core

## Verificação

- Relacionados: 84 passed (s7, dual_path, materialize, analytical_planning, fan_in, shard_routing, duckdb, sufficiency)
- Pré-existente fora de escopo: `tests/test_guardrail_break.py` adversarial
- `graph.py` ~1005 LOC (antes ~1273)
