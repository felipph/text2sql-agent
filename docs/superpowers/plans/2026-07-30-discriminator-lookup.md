# Discriminator lookup + max_shards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lookup-then-route de discriminadores via tabela não-shardada + rename `max_shard_discriminators` → `max_shards` (cap por shard físico).

**Architecture:** Módulo puro `discriminator_lookup.py`; `cap_bindings_by_shards` em `shard_routing.py`; integração em `resolve_and_route`; breaking rename no config.

**Tech Stack:** Python, pytest, LangGraph, DuckDB, SQLAlchemy

**Spec:** `docs/superpowers/specs/2026-07-30-discriminator-lookup-design.md`

---

### Task 1: Rename config `max_shards` + `cap_bindings_by_shards`

**Files:**
- Modify: `txt2sql/config.py`, `txt2sql/shard_routing.py`
- Modify: `tests/test_fan_in.py`, `tests/test_shard_routing.py`, playground/examples/docs
- Test: mesmos arquivos

- [x] Testes: load `max_shards`, default 20; `cap_bindings_by_shards` (2 shards ok / 25→20 parcial)
- [x] Implementar rename + cap pós-resolve em `resolve_routing`
- [x] Atualizar YAML/docs/testes que citam o nome antigo

### Task 2: `find_lookup_source` + `run_discriminator_lookup`

**Files:**
- Create: `txt2sql/discriminator_lookup.py`, `tests/test_discriminator_lookup.py`

- [x] Testes unitários find/run (hit/miss/preferência/cache/empty)
- [x] Implementar módulo

### Task 3: Integração `resolve_and_route` + prompts + graph tests

**Files:**
- Modify: `txt2sql/graph.py`, `txt2sql/prompts.py`
- Modify: `tests/test_graph_dual_path.py`, `tests/test_shard_routing.py`
- Docs: ADR-0002, arquitetura, api

- [x] Test graph: lookup injeta filters / sem relationship clarify / filter explícito não lookup
- [x] Wire lookup em resolve_and_route; partial/assumptions
- [x] Prompts + docs
- [x] Suite pytest verde
