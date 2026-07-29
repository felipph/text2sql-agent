# Design: Rearquitetura do grafo Text2SQL

**Data:** 2026-07-28  
**Status:** aprovado no grilling; PRD canônico em `docs/prd-refatoracao-grafo-rearquitetura.md`  
**Não implementar código neste documento** — spec de desenho.

## Contexto

O grafo atual (`txt2sql/agent.py`) é um loop ReAct com tools (`sql_db_query`, `resolve_shard`, `materialize_sharded_table`) após `interpret_intent`. O alvo é dual-path tipado (LLM → artefato → nó determinístico + middleware), preservando IntentPlan grounded, sharding determinístico e protegendo OLTP-hot.

## Decisões (grilling)

1. `resolve_routing` determinístico + `ShardRouting` — sem tools de shard no LLM.
2. Path ortogonal ao número de shards; single + analytical é o caso feliz OLTP-hot.
3. YAML `tables[].duckdb.force_analytical` (alias `trigger: always`).
4. Topologia do PRD (Gate, mat loop, verify, middleware).
5. DuckDB: catálogo reutilizável por `thread_id`; Gate reuse vs refresh; revisa ADR-0003; preferir file-backed.
6. Evoluir `IntentPlan`; path derivado por regras; `LogicalPlan` = projeção para provenance.
7. Verify: `answer` | `refine_sql` | `data_gap` (dois níveis de refine).
8. Timeout: origem + DuckDB; client deadline; config `query_timeout`; `status=timeout`.

## Componentes

| Unidade | Responsabilidade | Depende de |
|---|---|---|
| `intent.py` | IntentPlan + validate_intent (já existe) | schema index |
| `routing` (novo ou `query_routing` estendido) | `resolve_routing`, `route_execution`, ShardRouting | config, IntentPlan, resolver dotted |
| Policy Gate | Evolução de `guardrail.py` + routing reject + volume + force_analytical | sqlglot, config |
| Nós determinísticos | `exec_source`, `materialize`, `exec_duckdb` | Registry, DuckDB session |
| DuckDB session store | Sessão por thread_id + catálogo | filesystem / process registry |
| Middleware | pre/post hooks | Gate, timeout, compactor, budget |
| Nós LLM | intent, generate_*, gate, plan_mat, verify, answer | artefatos tipados |

## Relação com specs existentes

| Spec | Relação |
|---|---|
| 2026-07-27 intent-interpretation | Mantém; vira entrada do dual-path |
| 2026-07-28 query-timeout | Amplia escopo para materialize extract + DuckDB; tipa `ExecutionResult` |
| multi-shard / duckdb streaming | Reutiliza fan-in e BATCH_SIZE; deixa de ser tool LLM |

## Testes (aceite de desenho)

- S5 offline: DML reject, unresolved shard, force_analytical + SUM na origem, LIMIT inject.
- Unit: `route_execution` matriz (force / multi / agg / simple).
- Unit: `resolve_routing` missing discriminator → clarify signal.
- Gate: reuse vs refresh; sessão ausente → refresh.
- Verify: data_gap vs refine_sql roteia certo.

## Próximo passo

Plano de implementação em `docs/superpowers/plans/` (writing-plans) após review deste spec + PRD.
