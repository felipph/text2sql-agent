# API pública

Superfície Python exportada por `txt2sql`. Sem servidor OpenAPI — a “API” é o pacote.

## Imports estáveis

```python
from txt2sql import build_agent, load_config, AgentConfig, ShardResult, QueryTimeoutError
```

| Símbolo | Descrição |
|---------|-----------|
| `load_config(path, override_connections=None) -> AgentConfig` | Carrega e valida o YAML. |
| `build_agent(config, checkpointer=None, *, dual_path=True) -> CompiledStateGraph` | Compila o grafo LangGraph. |
| `AgentConfig` | Dataclass raiz da configuração. |
| `ShardResult(database_id, table_name)` | Retorno de resolvers de shard. |
| `QueryTimeoutError` | SELECT OLTP excedeu `query_timeout`; vira resultado/erro no grafo. |

Versão: `txt2sql.__version__`.

## `load_config`

* `path`: arquivo YAML.
* `override_connections`: `dict[str, str]` opcional `{database_id: url}` com precedência sobre `connection_string` / `connection_env`.

## `build_agent`

* `config`: `AgentConfig`.
* `checkpointer`: objeto LangGraph opcional (`MemorySaver`, saver Postgres, etc.). A lib **não** cria checkpointer.
* `dual_path`: `True` (padrão) → grafo em `txt2sql.graph` (simple \| analytical). `False` → loop ReAct legado em `txt2sql.agent` com tools.

Retorno: grafo com `invoke` / `stream`.

### Dual-path (padrão)

Estado tipado inclui, entre outros: `intent_plan`, `shard_routing`, `execution_path`, `sql_plan`, `budget`, catálogo DuckDB da sessão. Sessão DuckDB é **por `thread_id`** (file-backed via `DuckDBSessionStore`), não descartada a cada turno.

Fluxo resumido: `interpret_intent` → (clarificação HITL se preciso) → `resolve_and_route` → *simple* (`generate_sql` → `exec_source` → `verify` → `answer`) ou *analytical* (`sufficiency_gate` → materialização → SQL DuckDB → `verify` → `answer`).

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

### ReAct legado (`dual_path=False`)

Estado estende `MessagesState` com `page_count`, `schema_loaded`, `duckdb_session` (efêmera, **não** checkpointada), `resolved_shards`, `multi_materialized`, `pending_queries` (fila de queries validadas; várias `sql_db_query` no mesmo passo executam em sequência).

## Tools expostas ao LLM (somente ReAct)

| Tool | Args | Papel |
|------|------|-------|
| `resolve_shard` | `table_id`, `discriminator_value` | Resolve `{database_id, table_name}` (caminho single) |
| `materialize_sharded_table` | `table_id`, `discriminator_values[]` | Fan-in de 2+ discriminadores no DuckDB (nome lógico) |
| `sql_db_schema` | `table_names` (IDs separados por vírgula) | Schema + amostras |
| `sql_db_query` | `query` (um SELECT) | Executa via guardrail + rota DB/DuckDB |

No dual-path o LLM **não** chama essas tools: sharding via `resolve_routing` / `resolve_and_route`, execução via nós determinísticos + Policy Gate.

`materialize_sharded_table` só aparece se existir tabela shardada com DuckDB. Após o fan-in, consulte pelo **nome lógico**. Limite: `agent.max_shard_discriminators` (default `20`); valores excedentes são omitidos com aviso (`truncated`).

Implementação real está nos nós do grafo; as `StructuredTool` usam placeholder `_noop`.

## `AgentConfig` (agent YAML)

| Campo | Default | Papel |
|-------|---------|--------|
| `top_k` | 20 | Truncamento de linhas no resultado |
| `max_pages` | 10 | Máx. de queries de dados por turno |
| `max_shard_discriminators` | 20 | Máx. de discriminadores por `materialize_sharded_table` |
| `query_timeout` | 30 | Timeout de execução SELECT OLTP (segundos); `0` desliga |

Cada entrada em `databases[]` pode definir `query_timeout` opcional para sobrescrever o global. Use `AgentConfig.effective_query_timeout(database_id)` para obter o valor efetivo.

Em `tables[].duckdb`, `force_analytical: true` (ou `trigger: always`) obriga o path analítico — ver [configuração](../guias/configuracao.md).

## Módulos internos úteis (sem garantia de estabilidade)

* `txt2sql.guardrail.validate_sql` / `ReadOnlyViolationError`
* `txt2sql.policy.check_sql_plan` / `PolicyDecision`
* `txt2sql.intent.IntentPlan` / `validate_intent`
* `txt2sql.db.duckdb_layer.DuckDBSession`, `needs_duckdb`
* `txt2sql.db.session_store.DuckDBSessionStore`
* `txt2sql.db.registry.DatabaseRegistry`

Prefira a API de `__init__.py` em código de produto. Detalhes de YAML: [configuração](../guias/configuracao.md).
