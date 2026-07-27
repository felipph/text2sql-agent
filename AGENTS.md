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
  agent.py         # build_agent + nós do grafo
  config.py        # load_config + dataclasses
  guardrail.py     # validate_sql fail-closed
  db/              # registry, schema, shard, duckdb_layer
examples/          # YAMLs e resolver de exemplo
tests/             # pytest
docs/              # Documentação (PT-BR)
docs/superpowers/  # Specs e plans de features (não é doc de produto)
```

## Conventions

- API pública: `build_agent`, `load_config`, `AgentConfig`, `ShardResult` (`txt2sql/__init__.py`).
- Resolver de shard: caminho dotted `modulo.sub:funcao` retornando `ShardResult`.
- IDs de tabela no YAML são lógicos; nomes físicos podem diferir após shard.
- `materialize` no DuckDB usa lotes (`BATCH_SIZE`); não usar `fetchall` na origem.
- Guardrail é fail-closed: qualquer dúvida → rejeitar.

## Gotchas

- Checkpointer **não** é criado pela lib — passe via `build_agent(..., checkpointer=...)`.
- Tabelas shardadas exigem `resolve_shard` antes de `sql_db_query`; fan-out é proibido.
- Sem `columns` no YAML → discovery no `database` de referência; com `columns` → declarativo.
- Env vars de banco vêm de `connection_env` no YAML (ex.: `MAIN_DB_URL`), não de nomes fixos.
- `AZURE_OPENAI_*` são obrigatórias se o bloco `llm` do YAML estiver incompleto.
- Workspace pode não ter `.git`; commits só quando o repo existir.

## Key docs

- [Arquitetura](docs/arquitetura.md)
- [Primeiros passos](docs/primeiros-passos.md)
- [API](docs/referencia/api.md)
- [Variáveis de ambiente](docs/referencia/variaveis-de-ambiente.md)
- [ADRs](docs/adr/)
