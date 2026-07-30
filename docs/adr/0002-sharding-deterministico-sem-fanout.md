---
status: accepted
date: 2026-07-27
amended: 2026-07-30
---

# ADR-0002: Sharding determinístico sem fan-out

## Context and Problem Statement

Tabelas volumétricas estão particionadas em vários bancos físicos. Um agente SQL ingênuo poderia tentar `UNION` entre shards ou varrer todos — caro e perigoso.

## Considered Options

- Fan-out automático (consultar todos os shards e agregar)
- Resolução determinística `(discriminador) → (database_id, table_name)` obrigatória antes do SELECT
- Roteamento só por convenção de nome no prompt, sem tool

## Decision Outcome

Chosen option: **resolver determinístico `(discriminador) → ShardResult`, fan-out proibido**, because o domínio (ex.: CNPJ) já define o shard e evita varredura multi-banco.

**Emenda 2026-07-29 (dual-path):** no grafo padrão, a resolução ocorre no nó `resolve_and_route` via `resolve_routing` (callable dotted do YAML) — **sem tool LLM**. Ausência de discriminador em tabela shardada → clarificação HITL. Multi-discriminador → fan-in no DuckDB e path *analytical*. O caminho ReAct (`dual_path=False`) mantém as tools `resolve_shard` / `materialize_sharded_table`.

## Consequences

**Positive:**
- Custo e blast radius previsíveis por pergunta
- Contrato explícito via `ShardResult` / `ShardRouting`
- Cross-shard com lista conhecida: fan-in via DuckDB; fan-out cego continua proibido
- Lista descoberta via tabela lookup não-shardada (`RelationshipConfig`) — lookup-then-route — sem HITL no caminho feliz

**Negative:**
- Dual-path: IntentPlan precisa carregar o discriminador nos filtros (senão clarify **ou** lookup)
- ReAct: LLM precisa ser instruído a resolver (single) ou materializar (multi) antes de consultar
- Análise multi limitada por `max_shards` (shards físicos distintos) e `fetch_limit` por grupo/lookup

**Neutral:**
- Resolver é código do usuário (`modulo:funcao`), não hardcoded
- Extração textual de discriminadores (quando o IntentPlan omite `filters`) também é adapter opcional via `sharding.value_extractor` — a lib não embute formatos de domínio (CNPJ/CPF/etc.)
- Cap `max_shards` aplica-se após o resolve, por `(database_id, physical_table)`

## Emenda 2026-07-30 — lookup-then-route + `max_shards`

Quando `resolve_routing` não encontra discriminador mas existe `RelationshipConfig` ligando a coluna do discriminador a uma tabela **não-shardada**, o nó `resolve_and_route` executa `SELECT DISTINCT` nessa lookup (origem ou DuckDB se já materializada), injeta `FilterClause(op=in)` e re-resolve. Isso **não** é fan-out cego: a lista de discriminadores é materializada antes do fan-in.

`agent.max_shard_discriminators` foi renomeado para `agent.max_shards`: o teto conta shards físicos distintos, não a quantidade de valores do discriminador.