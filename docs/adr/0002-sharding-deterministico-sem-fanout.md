---
status: accepted
date: 2026-07-27
---

# ADR-0002: Sharding determinístico sem fan-out

## Context and Problem Statement

Tabelas volumétricas estão particionadas em vários bancos físicos. Um agente SQL ingênuo poderia tentar `UNION` entre shards ou varrer todos — caro e perigoso.

## Considered Options

- Fan-out automático (consultar todos os shards e agregar)
- Resolução determinística `(discriminador) → (database_id, table_name)` obrigatória antes do SELECT
- Roteamento só por convenção de nome no prompt, sem tool

## Decision Outcome

Chosen option: **resolver determinístico + tool `resolve_shard`, fan-out proibido**, because o domínio (ex.: CNPJ) já define o shard e evita varredura multi-banco.

## Consequences

**Positive:**
- Custo e blast radius previsíveis por pergunta
- Contrato explícito via `ShardResult`

**Negative:**
- Perguntas cross-shard exigem orquestração fora da lib
- LLM precisa ser instruído a resolver antes de consultar

**Neutral:**
- Resolver é código do usuário (`modulo:funcao`), não hardcoded
