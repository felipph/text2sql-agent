---
status: accepted
date: 2026-07-27
amended: 2026-07-28
---

# ADR-0003: Camada DuckDB intermediária por turno

## Context and Problem Statement

Agregações/ordens/joins em tabelas volumétricas no OLTP degradam o banco produtivo. Precisávamos de um caminho analítico sem warehouse separado.

## Considered Options

- Sempre executar no banco de origem
- Materializar em DuckDB in-memory efêmero por turno, com gatilhos (`aggregation` / `order` / `join` / `always`)
- ETL contínuo para um OLAP externo

## Decision Outcome

Chosen option: **DuckDB efêmero por turno com gatilhos e `fetch_limit`**, because isola custo analítico sem infra nova. Materialização usa lotes (`fetchmany`) para não carregar tudo em memória Python.

**Emenda 2026-07-28 (dual-path):** o grafo dual-path é o **padrão** em `build_agent(...)`. Catálogo DuckDB e reuse entre turnos usam **sessão por `thread_id`**, file-backed via `DuckDBSessionStore` (sufficiency gate decide refresh/reuse; `check_materialization` valida cobertura antes do SQL analítico). O caminho legado ReAct (`dual_path=False`, `generate_query` + tools) mantém DuckDB **efêmero por turno** — descartado em `init_turn`.

## Consequences

**Positive:**
- OLTP só entrega `SELECT *` limitado; agregação roda local
- ReAct: sessão descartada ao fim do turno — sem dados stale entre turnos
- Dual-path: reuse de materializações no mesmo `thread_id` reduz extract repetido

**Negative:**
- Resultado limitado a `fetch_limit` linhas da origem
- Inferência de tipos pelo primeiro lote

**Neutral:**
- Tabela DuckDB usa o id lógico da config para a query reescrita
