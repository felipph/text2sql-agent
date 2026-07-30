# Design: Sufficiency Gate determinístico via AST

**Data:** 2026-07-30  
**Status:** revisado — Fase 2 (plano sem LLM) incluída; TTL default 30min; gaps de rematerialização/predicados fechados

## Problema

A decisão de "buscar mais dados na origem para o DuckDB" está espalhada em três pontos com critérios divergentes:

1. `sufficiency_gate` (`txt2sql/graph.py`) — 100% LLM: dumpa `IntentPlan` + `DuckDBCatalog` em JSON e pede `GateDecision(reuse|refresh)`. Não recebe `ShardRouting`, não vê predicados nem colunas do cache, é não determinístico e custa uma chamada LLM por turno mesmo em casos triviais (catálogo vazio, pergunta repetida).
2. `check_materialization` — híbrido: `_catalog_covers_tables` (determinístico, só nomes de tabela) com fallback em `mat_check_llm`. Critério diferente do gate para a mesma pergunta ("o catálogo cobre o intent?"), o que permite loop gate↔mat_check discordando.
3. `verify` → `data_gap` — reentra no gate LLM.

O insumo determinístico já existe antes do gate: `IntentPlan` (entidades, filtros, métricas), `ShardRouting.bindings` (resolvido por `resolve_routing`), proveniência no catálogo (`DuckDBTableInfo.shard_bindings`, `source_queries`, `materialized_at`) e infra AST (sqlglot em `guardrail.py`, `policy.py`, `query_routing.py`).

## Decisão

Criar `txt2sql/sufficiency.py` com uma função pura que decide reuse/refresh deterministicamente; a LLM vira **fallback** apenas para o caso "não sei". Nenhum nó novo no grafo; `sufficiency_gate` e `check_materialization` passam a usar a mesma função.

Convenção fail-closed do repo aplicada: em dúvida → `refresh` (seguro; custa apenas I/O, nunca resposta errada).

## API

```python
# txt2sql/sufficiency.py

class TableGap(BaseModel):
    table_id: str
    reason: Literal[
        "missing_table",        # tabela do intent ausente do catálogo
        "missing_shard",        # binding exigido não coberto
        "missing_columns",      # projeção do extract não cobre colunas do intent
        "predicate_mismatch",   # WHERE do extract não subsume filtros do intent
        "stale",                # TTL excedido
    ]
    missing_bindings: list[ShardBinding] = []
    missing_columns: list[str] = []
    detail: str = ""

class SufficiencyDecision(BaseModel):
    action: Literal["reuse", "refresh", "unknown"]
    gaps: list[TableGap] = []       # ação por tabela quando action == "refresh"
    reasons: list[str] = []         # trilha legível para logs/proveniência

def evaluate_sufficiency(
    intent: IntentPlan,
    shard_routing: ShardRouting,
    catalog: DuckDBCatalog,
    config: AgentConfig,
    *,
    dialect: str | None,
    now: datetime | None = None,    # injetável para testes
) -> SufficiencyDecision: ...
```

- `action="refresh"` com `gaps` precisos → materialização direcionada (só o que falta).
- `action="unknown"` → único caso em que o grafo chama o `gate_llm`, com `reasons` injetadas no prompt como diagnóstico.
- Função pura, sem I/O de banco e sem LLM — testável isoladamente.

## Cascata de verificações (barato → caro; curto-circuito em gap)

Para cada `table_id` em `_intent_table_ids(intent)`:

1. **Cobertura de tabela** — mesma semântica de `_catalog_covers_tables` (match por `id`/`name`, case-insensitive). Ausente → gap `missing_table`; se a tabela é shardada, preencher `missing_bindings` com todos os bindings de `ShardRouting` para aquele `table_id` (insumo do plano determinístico). Catálogo vazio → `refresh` imediato sem avaliar o resto. Intent sem `table_id` algum: espelhar `_catalog_covers_tables` — catálogo vazio → `refresh`; senão → `reuse`.
2. **Cobertura de shards** — se a tabela é shardada: bindings exigidos por `ShardRouting.bindings` (filtrados por `table_id`) devem ser subconjunto de `DuckDBTableInfo.shard_bindings` (chave: `database_id` + `discriminator_value`). Faltando → gap `missing_shard` com **apenas** os bindings ausentes em `missing_bindings`. Exemplo: cache tem filial 654, pergunta agora inclui 747.
3. **Cobertura de colunas via AST** — parsear cada `source_queries` da entrada do catálogo com `sqlglot.parse_one(dialect=dialect)` (dialeto da **origem**, tipicamente `config.dialect`):
   - Entradas sintéticas (`fan-in:N bindings`, `SELECT * FROM …` gerado pelo path de binding único) e `SELECT *` cobrem todas as colunas. Colunas cobertas = união das projeções de todas as `source_queries`.
   - Projeção explícita: precisa ser superconjunto das colunas que o intent usa nessa tabela (`filters`, `metrics` com `column_id`, `group_by`, `joins.on`, `order_by`). Crítico: `_load_rows_into_duckdb` cria a tabela só com as colunas do extract anterior.
   - Parse falhou ou projeção com expressões não mapeáveis → `unknown` (não gap).
4. **Subsunção de predicados** — o cache contém linhas satisfazendo `F` (WHERE do extract); o intent precisa de `G`. Reuse seguro sse `G ⇒ F`. Regras conservadoras:
   - Query sintética / extract sem WHERE → cobre todos os predicados de domínio (cobertura de shard já tratou o discriminador).
   - Mais de uma `source_query` não-sintética → `unknown` (união de predicados não modelada nesta entrega).
   - Igualdade/`IN` na mesma coluna: valores do intent ⊆ valores do extract → cobre.
   - Ranges simples (`>`, `>=`, `<`, `<=`, `BETWEEN`) na mesma coluna: intervalo do intent contido no do extract → cobre.
   - `OR`, funções sobre colunas, subqueries, `LIKE`, `ne`, `is_null`, qualquer padrão fora dos acima → `unknown`.
5. **Frescor** — TTL com **default de 30 minutos** (`analytics.reuse_ttl_seconds: 1800` no YAML → `AgentConfig.reuse_ttl_seconds`): `now - materialized_at > ttl` → gap `stale`. Configurável; `0` ou negativo desabilita a verificação. Entrada sem `materialized_at` → tratar como stale (fail-closed). Comparar com `datetime` timezone-aware (`UTC`); `now` injetável.

Agregação: qualquer gap → `refresh` (com `gaps`, mesmo que também haja `unknown` em outra tabela); nenhum gap e nenhum `unknown` → `reuse`; sem gap mas com `unknown` em alguma verificação → `unknown`.

## Mudanças no grafo (topologia preservada)

### `sufficiency_gate`
```
decision = evaluate_sufficiency(...)
if decision.action != "unknown": usa direto (sem LLM)
else: gate_llm com decision.reasons no prompt (comportamento atual como fallback)
```
- Guarda `SufficiencyDecision` no estado (`sufficiency_decision: SufficiencyDecision | None`, seguindo a spec de artefatos tipados).
- O curto-circuito por `budget.exhausted("gate_visits")` → `refresh` permanece.
- `gate_visits` incrementa em **todo** caminho (determinístico ou LLM), para o loop `verify → data_gap → gate` continuar bounded.

### `plan_materialization`
- Consome `sufficiency_decision.gaps` quando presente: o prompt instrui a materializar **apenas** as tabelas com gap (hoje o refresh re-materializa tudo).
- **Plano sem LLM (aprovado — entra nesta entrega)**: se todos os gaps são `missing_shard`/`missing_table` de tabelas shardadas, construir o `MaterializationPlan` deterministicamente — um `MaterializationStep` por tabela com `target_table = table.id`, `source_query = ""`.
  - **`shard_bindings` do step = união** dos bindings já no catálogo para aquela tabela **mais** `gap.missing_bindings` (dedupe por `database_id`+`discriminator_value`). Motivo: `materialize` usa `replace=True` / fan-in completo — passar só o binding faltante apagaria shards já cached.
  - `materialize` já resolve via fan-in/binding único sem `source_query`.
  - Qualquer gap fora desse padrão (`missing_columns`, `predicate_mismatch`, `stale`, tabela não shardada) → cai no `mat_llm` (comportamento atual), com os gaps no prompt.

### `check_materialization`
- Substituir `_catalog_covers_tables` + `mat_check_llm` por `evaluate_sufficiency`: `reuse` → `mat_ready=True`; `refresh` → `mat_ready=False`; `unknown` → fallback `mat_check_llm` (atual). Um único critério nos dois nós elimina a divergência gate↔mat_check.

### `verify` → `data_gap`
- Sem mudança de aresta; o gate reavaliará com a mesma função.

## Fora de escopo

- Nó novo de extração de entidades — `interpret_intent` + `resolve_and_route` já entregam entidades e shards resolvidos.
- Invalidação por escrita na origem (CDC/notify) — apenas TTL.
- Subsunção de predicados sofisticada (normalização booleana, `OR` distribuído) — começar conservador; sofisticar só se a taxa de refresh desnecessário incomodar.
- Mudanças na API pública (`build_agent`, `load_config`) além do campo `analytics.reuse_ttl_seconds` no YAML (default 1800; `0`/negativo desabilita).
- Remoção de `GateDecision`/`MaterializationCheck` — continuam como fallback.

## Testes

`tests/test_sufficiency.py` (função pura, sem LLM/banco):
- Catálogo vazio → refresh.
- Tabela do intent ausente → gap `missing_table`.
- Shard: cache {654}, intent {654} → reuse; intent {654,747} → gap `missing_shard` só com 747.
- Colunas: extract `SELECT a, b` + intent usando `c` → gap `missing_columns`; `SELECT *` → cobre.
- Predicados: extract `WHERE uf = 'SP'` + intent `uf = 'SP'` → reuse; intent `uf = 'RJ'` → gap; extract `WHERE valor > 100` + intent `valor > 500` → reuse (range contido); `OR`/`LIKE` → unknown.
- SQL não parseável → unknown (nunca reuse).
- TTL: default 1800s aplica sem config; excedido → gap `stale`; `materialized_at` ausente → gap `stale`; `reuse_ttl_seconds: 0` → ignora `materialized_at`.
- Plano determinístico: gaps só `missing_shard`/`missing_table` de tabelas shardadas → `MaterializationPlan` construído sem LLM (um step por tabela, `source_query` vazio, `shard_bindings` = catálogo ∪ missing); gap `missing_columns` no meio → fallback `mat_llm`.
- Rematerialização parcial: cache com binding 654 + gap missing 747 → step com bindings [654, 747] (não só 747).

`tests/test_graph_dual_path.py` (ajustes):
- Gate não chama LLM quando decisão é determinística (contador de invocações no LLM falso).
- Fallback LLM acionado apenas em `unknown`.
- `check_materialization` coerente com o gate (mesmo critério).

Smoke: `smoke_test_graph.py` deve continuar passando sem alteração de contrato.
