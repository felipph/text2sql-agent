# Design: fan-in multi-shard via DuckDB

**Data:** 2026-07-27  
**Escopo:** `DuckDBSession`, `ShardResolver` / novo tool, `agent.py` (estado + roteamento),
`config` (`max_shard_discriminators`), `prompts.py`, ADR-0002, testes  
**Status:** aprovado (pending implementation)

## Problema

O fluxo atual é 1 discriminador → 1 shard → (opcional) DuckDB. Perguntas que
comparam ou agregam vários discriminadores conhecidos (ex.: liquidez de vários
CNPJs; contratos de um financiador que tocam vários titulares) não funcionam:

- fan-out cego é proibido por design (ADR-0002);
- `_resolve_target` escolhe um único banco;
- `materialize` é idempotente por nome lógico — segundo shard é ignorado.

O DuckDB hoje é offload analítico de **um** shard, não mesa de junção cross-shard.

## Objetivo

Permitir análise cross-shard no DuckDB quando há lista conhecida de
discriminadores (explícita na pergunta ou descoberta no banco não-shardado),
sem fan-out cego no OLTP.

## Não-objetivos

- Fan-out sem lista de discriminadores (varrer todos os shards).
- Coluna técnica `_shard_key` (usar o discriminador de negócio, ex.: `cnpj`).
- Orquestração exclusiva no caller (fora da lib).
- Mudar o caminho single-discriminador existente.

## Decisões

| Tema | Escolha |
|------|---------|
| Estratégia | Materialização multi-fonte no DuckDB (fan-in analítico) |
| Origem da lista | Explícita **ou** descoberta via `sql_db_query` no banco não-shardado |
| Limite excedido | Usar os N primeiros + aviso; análise segue |
| Forma no DuckDB | Uma tabela lógica (`recebiveis`) com append/`UNION` das fontes |
| Orquestração | Híbrido: descoberta com tools atuais; tool dedicado só se `len(values) > 1` |
| Coluna técnica | Não |

## Fluxo

### Single (`len == 1`)

Inalterado: `resolve_shard` → `sql_db_query` → DuckDB se o gatilho bater.

### Multi (`len(values) > 1`)

1. LLM obtém a lista (pergunta ou SELECT no `db_main` / tabela não-shardada).
2. Chama `materialize_sharded_table(table_id, discriminator_values)`.
3. Tool: resolve cada valor → corta em `max_shard_discriminators` → agrupa por
   `(database_id, table_name)` → materializa com `WHERE <discriminator> IN (...)`
   → marca estado do turno.
4. LLM emite `sql_db_query` usando o **nome lógico** da tabela.
5. Roteamento força DuckDB; resultado inclui eco do aviso de truncamento se houver.

## Componentes

### Tool `materialize_sharded_table`

- Args: `table_id: str`, `discriminator_values: list[str]`
- Pré-condições: tabela shardada **e** com DuckDB habilitado
- Recusa `len == 0` (pedir valores) e `len == 1` (usar caminho single)
- Retorno JSON:
  `{table_id, materialized_values, truncated, omitted_count, message}`

### `DuckDBSession`

- Extender `materialize` (flag `append` / API irmã) para permitir várias fontes
  na mesma tabela lógica; hoje o segundo call é no-op.
- Cada call: `(engine, physical_name, filter_sql)` — schema no primeiro lote.
- Re-chamar o tool multi para a mesma `table_id` no turno: recria a tabela
  DuckDB (evita misturar materializações stale).

### Estado (`AgentState`)

```text
multi_materialized: dict[table_id, {values, truncated, omitted_count}]
```

- Guardrail: inclui o id lógico em `allowed_tables` quando presente.
- Roteamento: SQL que referencia esse id lógico → caminho DuckDB.

### Config

```yaml
agent:
  max_shard_discriminators: 20  # default
```

Escopo global do agente (blast radius / custo).

### Prompt

- Single: protocolo atual.
- Multi: listar valores → `materialize_sharded_table` antes da query analítica →
  consultar pelo nome lógico; ecoar aviso de truncamento.
- Continua proibido materializar sem lista de discriminadores.

## Erros e bordas

- Resolver falha em qualquer valor → falha a rodada do tool (fail-closed).
- Mesmo físico para vários valores → um `SELECT` com `IN (...)`.
- `fetch_limit` por chamada de origem (por grupo físico); teto aproximado
  `grupos × fetch_limit`.
- Truncamento por N: análise segue com aviso.

## Testes

- Append multi-fonte + `filter_sql`; agrupamento por shard; corte N + `truncated`.
- Roteamento: nome lógico pós-multi → DuckDB; single path intacto.
- Tool: recusa `len == 0` e `len == 1`.
- Smoke/graph (LLM falso): descoberta → materialize multi → agregação no lógico.

## Docs

- Atualizar ADR-0002: cross-shard via fan-in DuckDB com lista conhecida.
- Documentar tool e `max_shard_discriminators` na API / guias.
