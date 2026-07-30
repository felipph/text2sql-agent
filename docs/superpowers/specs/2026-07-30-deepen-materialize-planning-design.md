# Design: deepen materialize + analytical planning + limpar ReAct

**Data:** 2026-07-30  
**Status:** approved (grilling)

## Objetivo

Aprofundar três fricções do dual-path: (1) materialize espalhado no `graph.py`, (2) planning analítico com tipos LLM duplicados no graph, (3) leftovers ReAct (`ShardResolver`, `needs_duckdb`, docs).

## Decisões

### C3 — Remover leftovers ReAct
- Apagar `txt2sql/db/shard.py` (`ShardResolver` + tool).
- Apagar `needs_duckdb`.
- `resolve_routing(..., registry=None)` valida `database_id` quando registry é passado (`has_database`).
- Schema prompt: discriminador em `filters` / routing — sem `resolve_shard`.
- Atualizar `db/__init__.py`, `smoke_test.py`, `docs/arquitetura.md`, `docs/referencia/api.md`.
- Plans/specs históricos em `docs/superpowers/` ficam.

### C1 — `db/materialize.py`
- Interface: `materialize_tables(...) → MaterializeOutcome(catalog, rows, error?)`.
- Inclui steps do plano + completar tabelas do intent + provenance/`DuckDBCatalog`.
- Graph: aplica state + incrementa budget + compacta `last_result`.
- `fan_in` permanece helper multi-binding; single-binding também passa por ele (`len >= 1`).
- `DuckDBSession.materialize(..., source_sql=)` para extract custom em lotes.
- `DuckDBSession.load_rows(...)` residual; graph não toca `_conn`.

### C2 — `analytical_planning.py`
- Manter nós `sufficiency_gate`, `plan_materialization`, `check_materialization`.
- Módulo novo usa `sufficiency` (puro) + adapters LLM injetáveis.
- `GateDecision` / `MaterializationCheck` saem de `graph.py` para o módulo (ou ficam só como schemas LLM internos).

## Ordem

C3 → C1 → C2 (C3 pode paralelizar no arranque de C1 se PRs separados).

## Fora de escopo

- Unificar `_touched_table_ids` (candidato 4).
- Apagar `LogicalPlan` / `covered_filters` / `policy.path` (candidato 5).
- Split de `config.py` (candidato 6).
