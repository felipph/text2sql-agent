# Design: nó de interpretação de intenção (desambiguação)

**Data:** 2026-07-27  
**Escopo:** `txt2sql/intent.py`, nós no grafo (`interpret_intent`, `ask_clarification`), estado do agente, prompt do interpretador, playground HITL  
**Status:** implementado

## Problema

Hoje o fluxo vai de `load_schema` direto para `generate_query`. O LLM gera SQL (e tool calls) a partir da pergunta em linguagem natural + schema textual, sem um contrato intermediário casado com o schema.

Isso favorece:

- alucinação de tabelas/colunas;
- ambiguidades resolvidas em silêncio (período, entidade, métrica);
- dificuldade de inspecionar “o que o agente entendeu” antes do SQL.

## Objetivo

Inserir um nó de interpretação **antes** de gerar/executar SQL que:

1. transforma a intenção do usuário num plano semântico leve (`IntentPlan`) com grounding de entidades (`table_id` / `column_id`);
2. valida o plan programaticamente contra o schema conhecido (fail-closed);
3. em ambiguidade, **para e pergunta ao usuário** (HITL);
4. só então segue para `generate_query`, que traduz o plan validado em SQL/tools (fluxo atual de shard/guardrail/DuckDB intacto).

## Não-objetivos (v1)

- Compilar SQL de forma determinística a partir do plan (sem LLM).
- Pular `interpret_intent` em follow-ups “óbvios”.
- UI especial além do chat (playground continua por mensagens).
- Alterar guardrail, sharding, DuckDB ou API pública além do estado/nós internos do grafo.
- Assumir defaults silenciosos quando houver ambiguidade (`assumptions` fica vazio em `ready`).

## Decisões fechadas

| Tema | Escolha |
|------|---------|
| Ambiguidade | HITL — perguntar ao usuário (não chutar) |
| Forma do plan | Plano semântico leve **+** grounding de entidades |
| Posição no grafo | Após `load_schema`: `… → interpret_intent → generate_query` |
| Consumo do plan | Validação programática; LLM gera SQL a partir do plan validado |
| HITL mecânico | `interrupt()` se houver checkpointer; senão `AIMessage` + `END` |
| Abordagem | Nó único `interpret_intent` + validação + rota (não tool dentro de `generate_query`) |

## Arquitetura e fluxo

```
START → init_turn → route_discovery
          ├─[schema não carregado]→ load_schema → interpret_intent
          └─[schema carregado]───────────────────→ interpret_intent
interpret_intent
    ├─[needs_clarification] → ask_clarification
    │     ├─[com checkpointer] interrupt → (resume) → interpret_intent
    │     └─[sem checkpointer] AIMessage → END
    │           próximo invoke (mesma thread) → init_turn → … → interpret_intent
    ├─[intent inválido vs schema] → interpret_intent (retry, default máx 2)
    └─[intent válido] → generate_query → … (fluxo atual intacto)
```

Detecção de checkpointer: flag fechada em `build_agent` (`has_checkpointer = checkpointer is not None`); os nós não inspecionam o runtime.

### Estado novo

- `intent_plan: dict | None` — plan serializado (JSON-compatível) após interpretação/validação.
- `intent_retries: int` — contador de retries de validação no turno (zerado em `init_turn`).

`duckdb_session` e demais campos permanecem como hoje.

## Modelo `IntentPlan`

Structured output Pydantic (persistido como dict no estado). Representação canônica é JSON; YAML só para debug/playground se útil.

Campos:

- `status`: `ready` | `needs_clarification`
- `question_rewrite`: pergunta desambiguada em PT-BR
- `entities[]`: grounding
  - `mention`, `table_id`, `column_id` (opcional), `role`: `table` | `column` | `value`
- `filters[]`: `table_id`, `column_id`, `op` (`eq` | `ne` | `gt` | `gte` | `lt` | `lte` | `in` | `like` | `between` | `is_null`), `value`
- `metrics[]`: `table_id`, `column_id` (null se `COUNT(*)`), `agg` (`count` | `sum` | `avg` | `min` | `max` | `none`)
- `group_by[]`: `{table_id, column_id}`
- `joins[]`: `{from_table_id, to_table_id, on: [{from_column, to_column}]}`
- `order_by[]`: `{table_id, column_id, direction: asc|desc}`
- `limit`: `int | null`
- `clarification`: só se `needs_clarification` — `question`, `options` (opcional)
- `assumptions`: lista; na v1 permanece vazia quando `status=ready` (HITL estrito)

## Validação programática

Função pura `validate_intent(plan, schema_index) -> ValidationResult` em `txt2sql/intent.py`.

`schema_index` é `dict[table_id, set[column_name]]`, construído de forma **estruturada** (não parse de texto DDL):

- tabelas declarativas: colunas a partir de `TableConfig.columns`;
- tabelas discovery: reflexão via `SchemaLoader` / SQLAlchemy `inspect` (mesmo caminho do discovery atual), exposta por um helper tipo `SchemaLoader.get_column_index()` — uma vez por turno, reutilizável pelo validador.

Se a reflexão de uma tabela discovery falhar, a validação trata as colunas dessa tabela como desconhecidas (fail-closed para refs de coluna nela).

Regras fail-closed:

1. Todo `table_id` / `column_id` referenciado existe no índice.
2. Joins só entre tabelas presentes no plan; colunas do `on` existem nas tabelas respectivas.
3. Se `status=needs_clarification`, não segue para SQL (rota para `ask_clarification`).
4. IDs inventados ou inconsistências → inválido → retry com feedback no contexto.
5. Após esgotar `intent_retries` (default **2**), cai em clarificação genérica ao usuário (“não consegui mapear a pergunta ao schema; reformule ou esclareça X”), **nunca** segue para SQL com plan inválido.

## Componentes

### `txt2sql/intent.py` (novo)

- Modelos Pydantic do plan.
- `validate_intent`.
- Helpers de serialização (`model_dump` / parse).

### `txt2sql/agent.py`

- Nós:
  - `interpret_intent`: LLM **sem tools**, `with_structured_output(IntentPlan)`; lê mensagens do turno + schema já injetado; grava `intent_plan`.
  - `ask_clarification`: se `has_checkpointer`, chama `interrupt({type, question, options})`; no resume, anexa a resposta como `HumanMessage` e a aresta volta para `interpret_intent`. Sem checkpointer: emite `AIMessage` e aresta para `END`.
- Rotas após `interpret_intent` / validação.
- `init_turn` zera `intent_plan` e `intent_retries` a cada invoke completo (fluxo sem interrupt). Resume via interrupt **não** passa de novo por `init_turn`.
- Arestas: `load_schema` → `interpret_intent` (em vez de `generate_query`); `route_discovery` aponta para `interpret_intent` quando schema já carregado.
- `generate_query`: injeta `SystemMessage` com o plan validado, instruindo a traduzir **esse** intent e não inventar tabelas/colunas fora dele. Tools/shards/guardrail inalterados.

### Prompt do interpretador

Método `Txt2SqlPromptBuilder.build_intent_prompt()` (mesmo builder; prompt separado do system prompt de SQL), cobrindo:

- persona: mapear pergunta → `IntentPlan` casado ao schema;
- glossário / relacionamentos / semântica de tabelas quando disponíveis;
- regras: não inventar IDs; se ambíguo → `needs_clarification`; sem assumptions silenciosas na v1.

### Playground

- Detectar clarificação (interrupt payload ou `AIMessage` de esclarecimento) e exibir no chat.
- Próxima mensagem do usuário reinvoca o grafo na mesma `thread_id` (já é o padrão atual).

## HITL detalhado

| Contexto | Comportamento |
|----------|----------------|
| `build_agent(..., checkpointer=...)` | `ask_clarification` usa `langgraph.types.interrupt` com payload `{"type": "clarification", "question": ..., "options": ...}`. Caller retoma (`Command(resume=...)` ou API equivalente) com a resposta em texto; o nó anexa `HumanMessage` e o grafo reentra em `interpret_intent`. |
| Sem checkpointer | Emite `AIMessage` com a pergunta e vai a `END`. Caller faz novo `invoke` incluindo a resposta como `HumanMessage` (passa por `init_turn` de novo). |

A lib **não** cria checkpointer (ADR-0001 / convenção atual).

## Erros e retries

| Falha | Ação |
|-------|------|
| Structured output inválido / parse | 1 retry interno; depois clarificação “não entendi, reformule” |
| Validação de schema falhou | Feedback estruturado no contexto + `interpret_intent` de novo; incrementa `intent_retries` |
| `intent_retries` esgotado | Clarificação ao usuário; não chama `generate_query` |
| Usuário responde clarificação | Novo ciclo em `interpret_intent` com histórico da thread |

## Testes

- **Unit** (`tests/test_intent.py`): `validate_intent` — IDs válidos/inválidos, joins inconsistentes, `needs_clarification` não é “ready”.
- **Grafo com LLM fake**:
  - ambíguo → chega em clarificação / interrupt e **não** executa SQL;
  - plan válido → `generate_query` recebe plan no estado;
  - plan com ID inventado → retry; após N → clarificação.

## Impacto em docs (pós-implementação)

- Atualizar diagrama/fluxo em `docs/arquitetura.md`.
- Mencionar o nó e o HITL em `docs/referencia/api.md` / primeiros passos se o contrato de invoke mudar para callers com interrupt.
- Spec permanece em `docs/superpowers/specs/` (não é doc de produto).
