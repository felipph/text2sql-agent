# Design: GraphState com artefatos tipados

**Data:** 2026-07-30  
**Status:** aprovado

## Problema

`GraphState` tipa `intent_plan` como `IntentPlan`, mas demais artefatos como `dict[str, Any]`, embora existam modelos em `txt2sql/artifacts.py`. Os nós gravam `.model_dump()` e leem com `.model_validate()`, desalinhando anotação e runtime.

## Decisão

Abordagem A: anotar com tipos reais e gravar/ler instâncias Pydantic. Helpers `_coerce_*` aceitam modelo ou `dict` (resume/checkpoint).

## GraphState

| Campo | Tipo |
|-------|------|
| `intent_plan` | `IntentPlan \| None` (já tipado) |
| `shard_routing` | `ShardRouting \| None` |
| `sql_plan` | `SQLPlan \| None` |
| `materialization_plan` | `MaterializationPlan \| None` |
| `last_result` | `ExecutionResult \| None` |
| `verify_decision` | `VerifyDecision \| None` |
| `duckdb_catalog` | `DuckDBCatalog` |
| `budget` | `Budget` |

Literals opcionais (`execution_path`, `intent_route`, `gate_action`) ficam fora deste escopo se não forem necessários para o objetivo.

## Escrita / leitura

- Nós retornam instâncias (sem `.model_dump()` nos artefatos tipados).
- Acesso via atributos (`last.status`) após coerce.
- `_dump_json` continua aceitando modelo (já usa `model_dump` quando disponível).

## Fora de escopo

- Mudanças em `artifacts.py`, API pública, playground.
- Migrar `GraphState` para schema Pydantic completo do LangGraph.
- Serialização custom do checkpointer.

## Verificação

`pytest` nos testes de grafo / clarificação; suite completa se houver regressão.
