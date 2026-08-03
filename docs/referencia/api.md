# API pública

Superfície Python exportada por `txt2sql`. Sem servidor OpenAPI — a “API” é o pacote.

## Imports estáveis

```python
from txt2sql import (
    build_agent,
    load_config,
    AgentConfig,
    ExportConfig,
    ShardResult,
    QueryTimeoutError,
    cleanup_expired_exports,
)
```

| Símbolo | Descrição |
|---------|-----------|
| `load_config(path, override_connections=None) -> AgentConfig` | Carrega e valida o YAML. |
| `build_agent(config, checkpointer=None) -> CompiledStateGraph` | Compila o grafo LangGraph dual-path. |
| `AgentConfig` | Dataclass raiz da configuração. |
| `ExportConfig` | Bloco `agent.export` (CSV sob demanda). |
| `cleanup_expired_exports(dir, ttl_seconds) -> int` | Remove CSVs expirados; a app agenda (sem daemon na lib). |
| `ShardResult(database_id, table_name)` | Retorno de resolvers de shard. |
| `QueryTimeoutError` | SELECT OLTP excedeu `query_timeout`; vira resultado/erro no grafo. |

Versão: `txt2sql.__version__`.

## `load_config`

* `path`: arquivo YAML.
* `override_connections`: `dict[str, str]` opcional `{database_id: url}` com precedência sobre `connection_string` / `connection_env`.

## `build_agent`

* `config`: `AgentConfig`.
* `checkpointer`: objeto LangGraph opcional (`MemorySaver`, saver Postgres, etc.). A lib **não** cria checkpointer.

Retorno: grafo com `invoke` / `stream`.

### Dual-path

Estado tipado inclui, entre outros: `intent_plan`, `shard_routing`, `execution_path`, `sql_plan`, `budget`, catálogo DuckDB da sessão. Sessão DuckDB é **por `thread_id`** (file-backed via `DuckDBSessionStore`), não descartada a cada turno.

Fluxo resumido: `interpret_intent` → (clarificação HITL se preciso) → `resolve_and_route` → *simple* (`generate_sql` → `exec_source` → `verify` → `answer`) ou *analytical* (`sufficiency_gate` → materialização → SQL DuckDB → `verify` → `answer`). Com `wants_export`, após materialização o nó `export_csv` gera o arquivo (COPY streaming) e o `answer` inclui `export_url`.

O LLM **não** chama tools de shard/SQL: sharding via `resolve_routing` / `resolve_and_route`, execução via nós determinísticos + Policy Gate. Limite de shards físicos: `agent.max_shards` (default `20`). Sem discriminador, o grafo pode fazer lookup via `RelationshipConfig` (tabela não-shardada).

### HITL (clarificação)

Quando o plano precisa de esclarecimento, o nó `ask_clarification`:

* **Com checkpointer:** chama `interrupt({"type": "clarification", "question": ..., "options": ...})`. O caller retoma com `Command(resume=resposta)` (mesmo `thread_id`).
* **Sem checkpointer:** emite `AIMessage` com a pergunta e encerra.
* Orçamento: `Budget.max_clarifications` (default `2`); esgotado → mensagem final e `finish`.

Exemplo de resume (playground / app hospedeira):

```python
from langgraph.types import Command

# após detectar result["__interrupt__"] com type=clarification:
agent.invoke(Command(resume="resposta do usuário"), config=cfg)
```

## `AgentConfig` (agent YAML)

| Campo | Default | Papel |
|-------|---------|--------|
| `sample_rows` | 20 | Linhas no sample de `ExecutionResult` |
| `query_max_rows` | 500000 | LIMIT injetado pelo Policy Gate se a SQL não tiver LIMIT |
| `max_intent_retries` | 2 | Retries de IntentPlan antes de clarificar |
| `max_shards` | 20 | Máx. de shards físicos distintos no fan-in / resolve_routing |
| `query_timeout` | 30 | Timeout de execução SELECT OLTP (segundos); `0` desliga |
| `budget` | `BudgetConfig()` | Orçamentos do grafo (clarificação, refine, mat, extracts) |
| `messages` | `MessagesConfig()` | Copy de UX (clarificação, export, partial) |
| `prompts` | `PromptsConfig()` | `intent_extra` / `answer_rules` |
| `export` | `ExportConfig()` | CSV denormalizado sob demanda (off por padrão) |

**Removidos (erro se presentes):** `top_k`, `max_pages`, `sample_rows_in_table_info`.

### `analytics`

| Campo | Default | Papel |
|-------|---------|--------|
| `reuse_ttl_seconds` | 1800 | TTL de reuse do catálogo DuckDB |
| `batch_size` | 5000 | `fetchmany` na materialização |
| `materialize_sample_rows` | 5 | LIMIT do sample pós-materialize |

### `agent.export` (`ExportConfig`)

```yaml
agent:
  export:
    enabled: false
    dir: /var/txt2sql/exports
    base_url: https://app.example/exports
    ttl_seconds: 86400
    delimiter: ","
    max_rows: 500000
```

| Campo | Default | Papel |
|-------|---------|--------|
| `enabled` | `false` | Se `true`, pedidos de exportar/CSV disparam o nó `export_csv` |
| `dir` | `""` | Diretório local dos arquivos (obrigatório se `enabled`) |
| `base_url` | `""` | Prefixo HTTP servido pela app (obrigatório se `enabled`) |
| `ttl_seconds` | `86400` | Idade máxima para `cleanup_expired_exports` |
| `delimiter` | `","` | Separador CSV (ex.: `";"` para Excel PT-BR) |
| `max_rows` | `500000` | Limite de linhas no SELECT exportado |

A lib grava o arquivo e monta a URL; a app hospedeira serve `dir` e agenda o cleanup:

```python
from txt2sql import cleanup_expired_exports

removed = cleanup_expired_exports("/var/txt2sql/exports", ttl_seconds=86400)
```

Estado relevante: `wants_export`, `export_url`, `export_result`.

Cada entrada em `databases[]` pode definir `query_timeout` opcional para sobrescrever o global. Use `AgentConfig.effective_query_timeout(database_id)` para obter o valor efetivo.

Em `tables[].duckdb`, `force_analytical: true` (ou `trigger: always`) obriga o path analítico — ver [configuração](../guias/configuracao.md).

## Módulos internos úteis (sem garantia de estabilidade)

* `txt2sql.guardrail.validate_sql` / `ReadOnlyViolationError`
* `txt2sql.policy.check_sql_plan` / `PolicyDecision`
* `txt2sql.intent.IntentPlan` / `validate_intent`
* `txt2sql.db.duckdb_layer.DuckDBSession`
* `txt2sql.db.fan_in.fan_in`
* `txt2sql.db.session_store.DuckDBSessionStore`
* `txt2sql.db.registry.DatabaseRegistry`
* `txt2sql.shard_routing.resolve_routing`

Prefira a API de `__init__.py` em código de produto. Detalhes de YAML: [configuração](../guias/configuracao.md).
