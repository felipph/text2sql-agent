# Design: expor knobs chumbados (budget, limites, messages, prompts)

**Data:** 2026-08-03  
**Status:** aprovado (brainstorming)  
**Motivo:** vários limites e textos de produto estão hardcoded; parte dos campos YAML (`top_k`, `max_pages`, `sample_rows_in_table_info`) não afeta o runtime.

## Decisões fechadas

| Tema | Escolha |
|------|---------|
| Escopo | Pacote C: operacional + limpeza de domínio + messages/prompts |
| Knobs mortos | **Remover** do YAML (`ValueError` se presentes) — breaking |
| Estrutura | Blocos aninhados: `agent.budget`, `agent.messages`, `agent.prompts`, `analytics.*` |
| Messages/prompts | Inline no YAML; defaults na lib se omitidos |
| Copy parcial | Inferir `{discriminator}` de `tables[].sharding.discriminator_column` |
| `max_rows_materialized` | Wire real (incrementar + `exhausted`) |
| Guardrail denylist | Fora de escopo (permanece hardcoded) |
| Session dir DuckDB | Fora de escopo (caller / `session_store`) |
| Deprecação silenciosa | Não — fail-closed nos nomes removidos |

## Problema

Operadores não conseguem ajustar orçamentos do grafo, LIMIT do Policy Gate, batch de materialização nem copy de UX sem fork da lib. Campos documentados (`top_k`, `max_pages`) induzem configuração inócua.

## Configuração alvo

```yaml
agent:
  # removidos (erro se presentes): top_k, max_pages, sample_rows_in_table_info
  read_only: true
  max_shards: 20
  query_timeout: 30
  max_string_length: 5000

  sample_rows: 20              # linhas no sample de ExecutionResult (compact_result)
  query_max_rows: 500000       # LIMIT injetado pelo Policy Gate se SQL sem LIMIT
  max_intent_retries: 2

  budget:
    max_clarifications: 2
    max_refine: 3
    max_mat_loops: 3
    max_gate_visits: 2
    max_rows_per_extract: 500000
    max_rows_materialized: 2000000

  export_detect_keywords:
    - exportar
    - baixar
    - csv
    - planilha
    - lista completa

  messages:
    clarification_exhausted: "..."
    export_disabled: "..."
    export_no_data: "..."
    export_failed: "..."
    export_download_hint: "Você pode baixar a lista completa aqui: {url}"
    export_truncated: "..."   # pode usar {discriminator}
    partial_coverage: "..."    # deve usar {discriminator} quando fizer sentido
    answer_fallback_header: "Resultado da consulta:"

  prompts:
    intent_extra: ""           # anexado ao system prompt de interpret_intent
    answer_rules: ""           # se não vazio, substitui o bloco de regras do nó answer

  export:
    # (já existente — inalterado nesta spec além de messages)

analytics:
  reuse_ttl_seconds: 1800
  batch_size: 5000
  materialize_sample_rows: 5

# custom_section: permanece no Txt2SqlPromptBuilder (SQL persona); não no intent
```

### Validação

- Inteiros de budget / `query_max_rows` / `sample_rows` / `batch_size` / `max_intent_retries` / `materialize_sample_rows`: `>= 1` (exceto onde `0` já significa “desliga”, nenhum destes usa 0).
- Se `agent` contiver `top_k`, `max_pages` ou `sample_rows_in_table_info` → `ValueError` listando substitutos:
  - `top_k` → `agent.sample_rows` + `agent.query_max_rows`
  - `max_pages` → removido (grafo dual-path não pagina; use budgets)
  - `sample_rows_in_table_info` → `tables[].sample_rows`
- `export_detect_keywords`: lista de strings; omitido → defaults da lib; `[]` → só `IntentPlan.wants_export`.
- Messages/prompts: string; omitido/null/"" → default da lib (exceto `intent_extra` / `answer_rules` vazios = sem extra / manter rules default).

## Mapeamento runtime

| Config | Destino |
|--------|---------|
| `agent.budget.*` | `Budget` em `init_state` / construção do grafo |
| `agent.sample_rows` | `Budget.sample_rows` |
| `agent.query_max_rows` | `check_sql_plan(..., max_rows=)` em `exec_source` e `exec_duckdb` |
| `agent.budget.max_rows_per_extract` | materialize Policy Gate |
| `agent.budget.max_rows_materialized` | incrementar `total_rows_materialized` após materialize; se exhausted → erro controlado |
| `agent.max_intent_retries` | substitui `MAX_INTENT_RETRIES` |
| `analytics.batch_size` | substitui `BATCH_SIZE` em `duckdb_layer` |
| `analytics.materialize_sample_rows` | `LIMIT N` do sample pós-materialize |
| `agent.messages.*` | constantes / strings em `graph`, `answer_grounding`, export node |
| `agent.export_detect_keywords` | `detect_wants_export` |
| `agent.prompts.intent_extra` | append em `build_intent_prompt` |
| `agent.prompts.answer_rules` | bloco de regras do nó `answer` |

### Placeholder `{discriminator}`

Resolver a partir do `IntentPlan` / tabelas tocadas: primeira tabela com `sharding.discriminator_column`; senão literal `"discriminador"`. Aplicar em `partial_coverage`, `export_truncated` e quaisquer defaults que hoje digam “CNPJs”.

### Placeholder `{url}`

Só em `export_download_hint`.

## Correção inclusa

Em `prompts.py` `_section_general_rules`: remover afirmação de que DML/DDL são permitidos; alinhar ao guardrail fail-closed / `read_only`.

## Não-objetivos

- Denylist do guardrail configurável
- Path do DuckDB session store no YAML
- Arquivos externos de messages
- Alias/deprecação silenciosa de `top_k` / `max_pages` / `sample_rows_in_table_info`
- Reescrever o prompt SQL completo como template livre

## Testes mínimos

1. `load_config` rejeita `top_k` / `max_pages` / `sample_rows_in_table_info`
2. YAML com `budget` / `query_max_rows` / `sample_rows` popula `AgentConfig` e `Budget` no grafo
3. Policy Gate usa `query_max_rows` (não 500k hardcoded quando config ≠ default)
4. Override de `messages.clarification_exhausted` aparece no fluxo de budget esgotado
5. `partial_coverage` renderiza nome da coluna discriminadora
6. `batch_size` chega ao materialize (`fetchmany`)
7. `intent_extra` presente no prompt de intent
8. Prompt SQL não afirma DML/DDL liberados

## Docs a atualizar na implementação

- `examples/agente-completo.yaml`
- `docs/guias/configuracao.md`
- `docs/referencia/api.md`
- Playground `config.yaml` se ainda usar knobs removidos
