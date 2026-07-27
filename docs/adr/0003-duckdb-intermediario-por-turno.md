---
status: accepted
date: 2026-07-27
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

## Consequences

**Positive:**
- OLTP só entrega `SELECT *` limitado; agregação roda local
- Sessão descartada ao fim do turno — sem dados stale entre turnos

**Negative:**
- Resultado limitado a `fetch_limit` linhas da origem
- Inferência de tipos pelo primeiro lote

**Neutral:**
- Tabela DuckDB usa o id lógico da config para a query reescrita
