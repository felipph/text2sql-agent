# Design: Lookup-then-route de discriminador + `max_shards`

**Data:** 2026-07-30  
**Status:** aprovado (brainstorming)  
**Motivo:** trace `d7633ffb…` — “análise de todos os clientes” entra em loop de clarificação porque `recebiveis` é shardada e o IntentPlan chega `ready` sem `filters` no CNPJ; o usuário pede para consultar `clientes`, mas o grafo não tem caminho para descobrir discriminadores.

## Problema

No dual-path, `resolve_routing` exige valores do discriminador em `IntentPlan.filters` (ou `value_extractor` textual) **antes** de qualquer SQL. Pedidos do tipo “todos os clientes” / join `clientes`↔`recebiveis` sem CNPJ explícito:

1. `missing_discriminator_filter_errors` → retry ao LLM
2. LLM → `needs_clarification` pedindo CNPJs
3. Usuário: “consulte clientes” → nova clarificação → loop

O prompt já menciona descobrir discriminador via outra tabela, mas **não existe nó** que execute esse lookup. Fan-out cego continua proibido (ADR-0002); falta o meio-termo: **lista descoberta via tabela lookup não-shardada**.

Além disso, `max_shard_discriminators` limita pela **quantidade de valores** do discriminador. No domínio CNPJ→shard, muitos CNPJs caem no mesmo físico; o custo real é o número de shards físicos abertos, não o de CNPJs.

## Decisão

1. **Lookup-then-route** determinístico quando faltar discriminador e existir fonte segura via `RelationshipConfig`.
2. Renomear **`max_shard_discriminators` → `max_shards`** e aplicar o cap sobre shards físicos distintos `(database_id, physical_table)` após o resolve — em `resolve_routing` e no caminho de lookup.
3. Emendar ADR-0002: lista via lookup ≠ fan-out cego.
4. Sem HITL no caminho feliz; truncamento por `max_shards` → `partial=True` + assumption explícita.

## Gatilho

Em `resolve_and_route`, quando `resolve_routing` retornaria `ClarifyNeeded` **e** `find_lookup_source(intent, config)` ≠ `None`:

| # | Condição |
|---|----------|
| 1 | Intent toca tabela shardada sem `FilterClause` no discriminador |
| 2 | Existe `RelationshipConfig` ligando a coluna do discriminador a coluna de tabela **não-shardada** |
| 3 | Preferência se várias relações: tabela lookup já referida em `entities` / `joins` do intent |

Se não houver lookup seguro → `ClarifyNeeded` (comportamento atual).

## API

Módulo novo `txt2sql/discriminator_lookup.py` (puro / injetável):

```python
@dataclass(frozen=True)
class LookupSource:
    lookup_table_id: str
    lookup_column: str
    sharded_table_id: str
    discriminator_column: str

@dataclass(frozen=True)
class LookupResult:
    values: list[str]
    truncated_by_fetch: bool   # safety no DISTINCT (ver Limites)
    source_sql: str
    from_cache: bool

def find_lookup_source(intent: IntentPlan, config: AgentConfig) -> LookupSource | None: ...

def run_discriminator_lookup(
    source: LookupSource,
    *,
    config: AgentConfig,
    registry: Any,
    duckdb_session: Any | None,
    catalog: DuckDBCatalog | None,
) -> LookupResult: ...
```

`run_discriminator_lookup`:
- Preferir DuckDB se a lookup table já estiver no catálogo com cobertura adequada; senão `SELECT DISTINCT <col>` na origem (`database` da tabela).
- Não inventar valores; falha SQL ou 0 rows → sinal para clarificar (sem fallback inventado).

## Integração no grafo

Fluxo em `resolve_and_route`:

```
interpret_intent (ready)
    → resolve_routing
        → ShardRouting → (ensure filters) → route_execution  [caminho atual]
        → ClarifyNeeded
            → find_lookup_source?
                → None → ask_clarification
                → LookupSource → run_discriminator_lookup
                    → vazio/erro → ask_clarification
                    → valores → inject FilterClause(op=in)
                              → resolve_routing (de novo)
                              → aplicar max_shards
                              → ensure_discriminator_filters
                              → route_execution (quase sempre analytical / multi)
```

Uma tentativa de lookup por turno (não loop lookup↔clarify).

**Prompts:** regra 5 de sharding — lookup é do sistema; LLM não deve pedir CNPJ quando a pergunta cobre o escopo via tabela lookup relacionada.

## `max_shards` (rename + semântica)

### Config

- Campo: `AgentConfig.max_shards` (default **20**, mesmo default numérico de hoje).
- YAML: `agent.max_shards`.
- Aceitar alias de leitura `max_shard_discriminators` → `max_shards` **uma release** (deprecation warning) **ou** breaking direto no playground/examples — preferência: **breaking no repo** (playground + examples + docs + testes), sem alias, por ser lib ainda em evolução.
- Remover `max_shard_discriminators` de `AgentConfig`, validação, docs de produto e testes.

### Aplicação (pós-resolve)

Após resolver cada discriminador → `ShardBinding`:

1. Agrupar bindings por chave física `(database_id, physical_table)`.
2. Se `#grupos > max_shards`:
   - Manter os primeiros `max_shards` grupos (ordem estável: ordem de primeira aparição dos discriminadores).
   - Descartar bindings dos grupos excedentes.
   - Restringir `filters` / lista de valores aos discriminadores retidos.
   - `partial=True` + assumption: `"Cobertura parcial: N de M shards físicos (max_shards=K)"`.
3. Muitos discriminadores no **mesmo** físico contam como **1** shard.

Aplicar em:
- `resolve_routing` (substituir o slice `values[:max_shard_discriminators]` **antes** do resolve).
- Caminho lookup-then-route (mesma função utilitária, ex. `cap_bindings_by_shards(bindings, max_shards) -> CapResult`).

### Safety no DISTINCT (separado do cap de shards)

Lookup pode retornar milhares de CNPJs. Limite de fetch do DISTINCT: usar `fetch_limit` da tabela lookup (ou `Budget.max_rows_per_extract`), **não** `max_shards`. Se o DISTINCT truncar por fetch → `truncated_by_fetch=True` + partial/assumption distinta (“lista de discriminadores truncada no lookup”).

## Erros

| Caso | Ação |
|------|------|
| Sem relationship utilizável | `ClarifyNeeded` atual |
| Lookup table shardada | Ignorar como fonte |
| SQL lookup falha | Clarificar (mensagem: falha ao obter discriminadores de `<table>`) |
| 0 rows | Clarificar |
| Cap `max_shards` | Continuar parcial |
| Cap fetch DISTINCT | Continuar parcial com assumption |

## Testes

- Unit `find_lookup_source`: hit / miss / preferência por entity / lookup shardada rejeitada.
- Unit `run_discriminator_lookup`: valores, empty, cache DuckDB, erro.
- Unit `cap_bindings_by_shards`: 50 discs → 2 shards (ok); 50 discs → 25 shards com `max_shards=20` (parcial).
- Config: `max_shards` load + default; ausência de `max_shard_discriminators`.
- Graph: “todos os clientes” sem filter → inject `in` + analytical, sem HITL.
- Graph: sem relationship → clarify.
- Regression: CNPJ já em `filters` → lookup **não** roda.
- Atualizar `tests/test_fan_in.py`, `test_shard_routing.py`, docs/examples/playground.

## Docs

- Emenda ADR-0002: lookup-then-route com lista descoberta; cap por shards físicos (`max_shards`).
- `docs/arquitetura.md`: passo lookup entre intent e route.
- `docs/referencia/api.md` + guias: rename do campo.
- `playground/config.yaml` + `examples/*.yaml`.
- Prompt intent/SQL: alinhamento com lookup sistêmico.

## Fora de escopo

- Fan-out sem lista / enumerar todos os shards físicos sem discriminadores.
- HITL de confirmação antes do lookup (caminho feliz automático).
- Mudança de `IntentPlan` com campo `shard_scope` (abordagem B descartada).
- Unificar `_touched_table_ids` / outros cleanups de graph.

## Critério de sucesso

Replay do cenário do trace: pergunta de análise de **todos** os clientes (com `clientes` no intent e `recebiveis` shardada, sem CNPJ em filters) → materializa via fan-in dos CNPJs obtidos de `clientes` → responde sem loop de clarificação; se `#shards > max_shards`, responde parcial com aviso explícito.
