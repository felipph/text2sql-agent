# API pública

Superfície Python exportada por `txt2sql`. Sem servidor OpenAPI — a “API” é o pacote.

## Imports estáveis

```python
from txt2sql import build_agent, load_config, AgentConfig, ShardResult
```

| Símbolo | Descrição |
|---------|-----------|
| `load_config(path, override_connections=None) -> AgentConfig` | Carrega e valida o YAML. |
| `build_agent(config, checkpointer=None) -> CompiledStateGraph` | Compila o grafo LangGraph. |
| `AgentConfig` | Dataclass raiz da configuração. |
| `ShardResult(database_id, table_name)` | Retorno de resolvers de shard. |

Versão: `txt2sql.__version__`.

## `load_config`

* `path`: arquivo YAML.
* `override_connections`: `dict[str, str]` opcional `{database_id: url}` com precedência sobre `connection_string` / `connection_env`.

## `build_agent`

* `config`: `AgentConfig`.
* `checkpointer`: objeto LangGraph opcional (`MemorySaver`, saver Postgres, etc.). A lib **não** cria checkpointer.

Retorno: grafo com `invoke` / `stream`. Estado estende `MessagesState` com `page_count`, `schema_loaded`, `duckdb_session`, `resolved_shards`, `pending_query`.

## Tools expostas ao LLM

| Tool | Args | Papel |
|------|------|-------|
| `resolve_shard` | `table_id`, `discriminator_value` | Resolve `{database_id, table_name}` |
| `sql_db_schema` | `table_names` (IDs separados por vírgula) | Schema + amostras |
| `sql_db_query` | `query` (um SELECT) | Executa via guardrail + rota DB/DuckDB |

Implementação real está nos nós do grafo; as `StructuredTool` usam placeholder `_noop`.

## Módulos internos úteis (sem garantia de estabilidade)

* `txt2sql.guardrail.validate_sql` / `ReadOnlyViolationError`
* `txt2sql.db.duckdb_layer.DuckDBSession`, `needs_duckdb`
* `txt2sql.db.registry.DatabaseRegistry`

Prefira a API de `__init__.py` em código de produto. Detalhes de YAML: [configuração](../guias/configuracao.md).
