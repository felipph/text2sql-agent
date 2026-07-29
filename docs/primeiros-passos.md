# Primeiros passos

Do zero até rodar os smokes locais.

## Pré-requisitos

| Tool | Versão | Instalação |
|------|--------|------------|
| Python | ≥ 3.12 | [python.org](https://www.python.org/) ou `uv` |
| uv (opcional) | recente | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

Azure OpenAI e bancos reais só são necessários para invocar o agente de ponta a ponta. Os smokes usam SQLite in-memory e LLM falso.

## Setup

1. Entre no projeto:

   ```bash
   cd txt2sql
   ```

2. Crie o ambiente e instale com extras de desenvolvimento:

   ```bash
   uv sync
   uv pip install -e ".[dev]" --python .venv/bin/python
   # alternativa sem uv:
   # python -m venv .venv && .venv/bin/pip install -e ".[dev]"
   ```

3. (Opcional) Configure LLM e bancos para uso real — veja [variáveis de ambiente](referencia/variaveis-de-ambiente.md). Para o exemplo `recebiveis.yaml`:

   ```bash
   export AZURE_OPENAI_API_KEY=...
   export AZURE_OPENAI_ENDPOINT=...
   export AZURE_OPENAI_DEPLOYMENT=gpt-4o
   export AZURE_OPENAI_API_VERSION=2024-06-01
   export MAIN_DB_URL=postgresql+psycopg://...
   export SHARD_1_DB_URL=...
   export SHARD_2_DB_URL=...
   export SHARD_3_DB_URL=...
   ```

## Verificar que funciona

```bash
.venv/bin/pytest tests/ -v
.venv/bin/python smoke_test.py
.venv/bin/python smoke_test_graph.py
```

Esperado:

* `pytest`: todos os testes em `tests/` passam.
* `smoke_test.py`: termina com `=== RESULTADO: TODOS OS TESTES PASSARAM ===`.
* `smoke_test_graph.py`: fluxo do grafo completa sem traceback.

Uso mínimo do agente (requer LLM + bancos):

```python
from txt2sql import build_agent, load_config
from langgraph.checkpoint.memory import MemorySaver

config = load_config("examples/recebiveis.yaml")
# dual_path=True por padrão; checkpointer necessário para HITL (clarificação + resume)
agent = build_agent(config, checkpointer=MemorySaver())
```

Sem checkpointer, perguntas ambíguas recebem a clarificação e o grafo encerra
(sem `Command(resume=...)`). Contrato: [API → HITL](referencia/api.md#hitl-clarificação).

## Playground (Postgres + Streamlit)

Para exercitar o agente contra Postgres real com sharding, clarificação HITL e painel de debug,
veja [playground/README.md](../playground/README.md).

## Problemas comuns

**`ModuleNotFoundError: pytest` / `ruff`**  
Instale o extra de desenvolvimento: `pip install -e ".[dev]"`.

**`env var 'MAIN_DB_URL' não definida`**  
O YAML referencia `connection_env`. Defina a env var ou passe `override_connections` em `load_config`.

**`AZURE_OPENAI_*` faltando ao chamar `build_agent`**  
Complete o bloco `llm` no YAML ou exporte as env vars listadas em [variáveis de ambiente](referencia/variaveis-de-ambiente.md).
