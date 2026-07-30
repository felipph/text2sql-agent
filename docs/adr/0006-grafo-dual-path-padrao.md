---
status: accepted
date: 2026-07-28
amended: 2026-07-29
decision-makers: maintainers txt2sql
---

# ADR-0006: Grafo dual-path como padrão (simple | analytical)

## Context and Problem Statement

O loop ReAct com tools (`sql_db_query`, `resolve_shard`, …) após `interpret_intent` deixava sharding e roteamento analítico nas mãos do LLM: risco de fan-out, agg pesada no OLTP e retries opacos. Precisávamos de um grafo tipado com decisões determinísticas e proteção explícita ao OLTP-hot.

## Considered Options

- Manter só ReAct + tools, reforçando o system prompt
- Grafo dual-path (IntentPlan → resolve/route → simple | analytical) com Policy Gate e budgets
- Substituir IntentPlan grounded por plano SQL livre gerado de uma vez

## Decision Outcome

Chosen option: **grafo dual-path como padrão** em `build_agent(..., dual_path=True)`, because separa “o que perguntar” (IntentPlan) de “como executar” (nós determinísticos + middleware), preserva grounding e permite `dual_path=False` para o ReAct legado.

### Pros and Cons of the Options

#### Só ReAct + prompt
- Good, because implementação já existente e flexível
- Bad, because o LLM controla shard/materialize; difícil garantir `force_analytical` e anti-fan-out

#### Dual-path tipado
- Good, because `resolve_routing` / `route_execution` são determinísticos; Policy Gate fail-closed; budgets (`max_clarifications`, mat loops, gate visits)
- Bad, because mais nós e artefatos para manter; ReAct permanece como caminho secundário

#### Plano SQL livre único
- Good, because menos round-trips
- Bad, because perde grounding e validação estruturada de intent

## Consequences

**Positive:**
- Sharding sem tools no LLM; multi-shard força path analytical
- `force_analytical` / `trigger: always` protegem OLTP-hot
- HITL de clarificação com orçamento; DuckDB por `thread_id` com reuse via sufficiency gate

**Negative:**
- Dois grafos para documentar e testar (`graph.py` vs ReAct em `agent.py`)
- Callers precisam de checkpointer para resume de clarificação

**Neutral:**
- API pública de `__init__.py` inalterada além do kwarg `dual_path`

## Confirmation

- Testes em `tests/test_graph_dual_path.py`, `test_path_routing.py`, `test_shard_routing.py`, `test_policy_gate.py`, `test_clarification_loop.py`
- Smoke `smoke_test_graph.py` cobre o caminho compilado

## Emenda 2026-07-29 — Remoção do stack ReAct e unificação do fan-in

**Decisão:** o stack ReAct (`dual_path=False`) foi **removido**. `build_agent` não aceita mais o kwarg `dual_path`; só o grafo dual-path é mantido.

**Razão:** ReAct e dual-path divergiam em sharding, segurança (policy gate) e DuckDB lifetime. Manter os dois impedia localidade de bugs e exigia documentação bifurcada. Todos os callers internos usavam `dual_path=True`.

**Consequências:**
- `agent.py` é agora um wrapper fino que delega para `graph.build_graph`
- `db/multi_shard.py` foi removido; `db/fan_in.py` substitui com interface `fan_in(session, table, registry, bindings) → FanInResult`
- `_fan_in_sharded_bindings` em `graph.py` foi removido; o grafo chama `fan_in()` diretamente
- Fan-in agora verifica existência de tabela física (gap que `_fan_in_sharded_bindings` não cobria)
- Testes anteriores de ReAct migrados para o dual-path; `test_multi_shard.py` substituído por `test_fan_in.py`
