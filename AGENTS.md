# txt2sql

Biblioteca standalone para agentes Text-to-SQL com LangGraph, multi-banco, sharding determinístico e DuckDB intermediário.

## Language

All docs and comments in PT-BR. Termos técnicos de indústria (API, commit, deploy, branch, middleware) permanecem em inglês.

## Commands

```bash
uv sync                              # Instalar deps (ou: pip install -e ".[dev]")
.venv/bin/pytest tests/ -v           # Testes unitários
.venv/bin/python smoke_test.py       # Smoke camadas de dados
.venv/bin/python smoke_test_graph.py # Smoke do grafo (LLM falso)
.venv/bin/ruff check .               # Lint
.venv/bin/ruff format .              # Format
```

## Structure

```
txt2sql/           # Pacote
  agent.py         # build_agent (wrapper fino para graph.build_graph)
  graph.py         # Grafo dual-path (padrão)
  intent.py        # IntentPlan + validate_intent
  artifacts.py     # Planos tipados, Budget, DuckDBCatalog
  policy.py        # Policy Gate pré-execução
  config.py        # load_config + dataclasses
  guardrail.py     # validate_sql fail-closed
  db/              # registry, schema, shard, duckdb_layer, session_store
examples/          # YAMLs e resolver de exemplo
playground/        # Postgres + Streamlit
tests/             # pytest
docs/              # Documentação (PT-BR)
docs/superpowers/  # Specs e plans de features (não é doc de produto)
```

## Conventions

- API pública: `build_agent`, `load_config`, `AgentConfig`, `ShardResult` (`txt2sql/__init__.py`).
- Resolver de shard: caminho dotted `modulo.sub:funcao` retornando `ShardResult`.
- IDs de tabela no YAML são lógicos; nomes físicos podem diferir após shard.
- `materialize` no DuckDB usa lotes (`BATCH_SIZE`); não usar `fetchall` na origem.
- Guardrail / Policy Gate são fail-closed: qualquer dúvida → rejeitar.
- Antes do SQL: `interpret_intent` valida um `IntentPlan`; ambiguidade → HITL (`interrupt` com checkpointer).
- `build_agent(...)` usa o grafo dual-path (simple|analytical); stack ReAct removido.

## Gotchas

- Checkpointer **não** é criado pela lib — passe via `build_agent(..., checkpointer=...)`. HITL/resume exige checkpointer + `Command(resume=...)`.
- Clarificação: `Budget.max_clarifications` (default 2); esgotado → `finish` com mensagem, sem SQL.
- Sharding via `resolve_routing` determinístico; fan-in multi-shard via `db/fan_in.fan_in(bindings)`. Fan-out cego é proibido.
- Discriminador em `filters` do IntentPlan; fallback textual opcional via `sharding.value_extractor` (callable do app, não da lib).
- Sem `columns` no YAML → discovery no `database` de referência; com `columns` → declarativo.
- Env vars de banco vêm de `connection_env` no YAML (ex.: `MAIN_DB_URL`), não de nomes fixos.
- `AZURE_OPENAI_*` são obrigatórias se o bloco `llm` do YAML estiver incompleto.
- `force_analytical` / `trigger: always` obrigam path analítico; agg na origem é rejeitada pelo Policy Gate.
- Workspace pode não ter `.git`; commits só quando o repo existir.

## Key docs

- [Arquitetura](docs/arquitetura.md)
- [Primeiros passos](docs/primeiros-passos.md)
- [API](docs/referencia/api.md)
- [Variáveis de ambiente](docs/referencia/variaveis-de-ambiente.md)
- [ADRs](docs/adr/)
