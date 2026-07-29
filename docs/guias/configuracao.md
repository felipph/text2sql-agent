# Configuração

Como configurar um agente `txt2sql` para diferentes ambientes.

## Mecanismos

1. **Arquivo YAML** — fonte principal (`load_config(path)`). Exemplos em `examples/`.
2. **`override_connections`** — mapa `{database_id: connection_string}` passado a `load_config` (prioridade sobre YAML/env).
3. **Env vars** — `connection_env` por banco + `AZURE_OPENAI_*` + `LANGFUSE_*`. Referência completa: [variáveis de ambiente](../referencia/variaveis-de-ambiente.md).
4. **`build_agent(..., dual_path=True)`** — parâmetro de código (não YAML). `True` (padrão) = grafo dual-path; `False` = ReAct legado. Ver [API](../referencia/api.md).

## Blocos do YAML

| Bloco | Função |
|-------|--------|
| `dialect` | Dialeto SQL (prompt + guardrail) |
| `databases[]` | Engines (`connection_string` ou `connection_env`, `read_only`, `query_timeout` opcional) |
| `tables[]` | Tabelas lógicas; `columns` → declarativo; ausente → discovery |
| `tables[].description` | Texto negocial da tabela (prompt + schema); opcional |
| `tables[].sharding` | `discriminator_column` + `resolver` dotted |
| `tables[].duckdb` | `enabled`, `trigger`, `fetch_limit`, `force_analytical` |
| `relationships[]` / `glossary[]` | Contexto semântico no prompt |
| `agent` | `top_k`, `max_pages`, `max_string_length`, `read_only`, `query_timeout` (default 30; `0` desliga) |
| `llm` | Azure OpenAI (opcional se env vars completas) |
| `custom_section` | Texto livre no system prompt |

### DuckDB: `force_analytical` e `trigger`

* `force_analytical: true` — obriga extract → DuckDB → análise (path *analytical* no dual-path; Policy Gate rejeita agg pesada na origem).
* `trigger: always` — alias que força `force_analytical=True` no parse do YAML.
* Demais triggers (`aggregation`, `order`, `join`) — no dual-path, agregação/`group_by` em tabela com `duckdb.enabled` também roteia para *analytical*; no ReAct, o trigger casa com a SQL da tool.

## Por ambiente

**Desenvolvimento** — SQLite/`override_connections` locais; LLM de teste ou `smoke_test_graph.py` sem Azure.

**Homologação / produção** — connection strings via env vars nomeadas no YAML; `read_only: true`; `fetch_limit` alinhado ao volume aceitável; Langfuse se quiser tracing.

## Exemplos

* Multi-banco + shard + DuckDB: `examples/recebiveis.yaml`
* Discovery MSSQL: `examples/diario.yaml`
* Resolver: `examples/shard_resolver_example.py`

```python
from txt2sql import load_config

config = load_config(
    "examples/recebiveis.yaml",
    override_connections={"db_main": "sqlite:////tmp/main.db"},
)
```
