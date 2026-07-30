# txt2sql

Biblioteca standalone para agentes Text-to-SQL com LangGraph, multi-banco e DuckDB.

## Visão Geral

`txt2sql` reimplementa um agente Text-to-SQL como pacote Python independente — sem FastAPI nem aplicação hospedeira. Serve quem precisa consultar vários bancos físicos, tabelas particionadas e cargas analíticas sem sobrecarregar o OLTP.

Três diferenciais frente a um agente SQL tradicional:

1. **Multi-banco + sharding determinístico** — `(discriminador) → (banco, tabela_física)`; fan-out é proibido.
2. **Schema declarativo ou discovery** — YAML com descrições de negócio, ou reflection via SQLAlchemy.
3. **DuckDB intermediário** — agregações/ordens/joins em tabelas volumétricas materializam em DuckDB (sessão por `thread_id`, reuse via sufficiency gate).

## Funcionalidades

* **Grafo dual-path** — `IntentPlan` → `resolve_and_route` → caminho *simple* ou *analytical*.
* **Interpretação de intenção + HITL** — `interpret_intent` valida o plano; ambiguidade → clarificação (`interrupt` com checkpointer).
* **Policy Gate + guardrail read-only** — validação fail-closed (sqlglot + regras de volume/`force_analytical`) antes de executar.
* **Checkpointer externo** — a lib não gerencia sessão; o caller injeta `MemorySaver` ou equivalente.
* **Tracing opcional** — Langfuse quando as env vars estão definidas.
* **Config YAML** — bancos, tabelas, relacionamentos, glossário e LLM.

## Estrutura do Projeto

```bash
├── pyproject.toml              # Metadados, deps, ruff
├── README.md
├── AGENTS.md                   # Contexto para agentes de IA
├── CONTRIBUTING.md
├── smoke_test.py               # Smoke das camadas de dados
├── smoke_test_graph.py         # Smoke do grafo (LLM falso)
├── examples/                   # YAMLs e resolver de shard de exemplo
├── playground/                 # Postgres + Streamlit (harness manual)
├── tests/                      # Testes unitários (pytest)
├── docs/                       # Documentação (ver seção abaixo)
└── txt2sql/                    # Pacote principal
    ├── agent.py                # build_agent (wrapper para graph.build_graph)
    ├── graph.py                # Grafo dual-path (padrão)
    ├── intent.py               # IntentPlan + validate_intent
    ├── artifacts.py            # Planos tipados, Budget, catálogo DuckDB
    ├── policy.py               # Policy Gate pré-execução
    ├── config.py               # Dataclasses + load_config
    ├── guardrail.py            # Validação SQL read-only
    ├── llm.py / prompts.py     # Azure OpenAI + prompts
    ├── tracing.py              # Langfuse opcional
    └── db/                     # Registry, schema, shard, DuckDB, session_store
```

## Pré-requisitos

- Python `>=3.12`
- Acesso a Azure OpenAI (ou env vars `AZURE_OPENAI_*`) para uso real do agente
- Connection strings dos bancos declarados no YAML (ou `override_connections`)

## Stack principal

* **LangGraph / LangChain** — orquestração do agente e tools
* **SQLAlchemy 2** — engines e discovery de schema
* **sqlglot** — parsing AST do guardrail / Policy Gate
* **DuckDB** — materialização analítica in-process
* **PyYAML / loguru** — config e logs

## Instalação

```bash
pip install -e .
# ou, com uv:
uv sync

# tracing opcional:
pip install -e ".[langfuse]"

# desenvolvimento (pytest, ruff):
pip install -e ".[dev]"

# playground (Streamlit + Postgres):
pip install -e ".[playground]"
```

### Como usar

```python
from txt2sql import build_agent, load_config
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage

config = load_config("examples/recebiveis.yaml")
agent = build_agent(config, checkpointer=MemorySaver())

resultado = agent.invoke(
    {"messages": [HumanMessage(content="Total de recebíveis do CNPJ 12.345.678/0001-90?")]},
    config={"configurable": {"thread_id": "sessao-1"}},
)
print(resultado["messages"][-1].content)
```

Overrides de conexão em runtime:

```python
config = load_config(
    "examples/recebiveis.yaml",
    override_connections={
        "db_shard_1": "postgresql+psycopg://user:pass@host-1/db",
    },
)
```

## Observações e Restrições

* Fan-out entre shards é proibido — sharding entra via `resolve_and_route`; fan-in multi-shard via `db/fan_in`.
* Queries de escrita são rejeitadas pelo guardrail / Policy Gate (fail-closed).
* Sessão DuckDB por `thread_id` (reuse via sufficiency gate).
* Clarificação HITL exige checkpointer; sem ele o grafo emite a pergunta e encerra.
* O checkpointer é responsabilidade do caller.

## Documentação

- [Primeiros passos](docs/primeiros-passos.md) — setup local e verificação
- [Arquitetura](docs/arquitetura.md) — componentes e fluxo
- [Contribuindo](CONTRIBUTING.md) — workflow de desenvolvimento
- [ADRs](docs/adr/) — decisões de arquitetura
- [Runbook](docs/guias/runbook.md) — operação e incidentes
- [API](docs/referencia/api.md) — superfície pública Python
- [Variáveis de ambiente](docs/referencia/variaveis-de-ambiente.md)

Specs/planos internos de features ficam em [`docs/superpowers/`](docs/superpowers/).

## Responsáveis

- **Felipph Calado** — <!-- TODO: e-mail / canal de contato preferido -->

## Licença

MIT. Ver `license` em [`pyproject.toml`](pyproject.toml).
