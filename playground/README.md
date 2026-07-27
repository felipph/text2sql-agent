# Playground txt2sql

Harness manual: Postgres (1 main + 2 shards) + UI Streamlit com painel de debug.

## Pré-requisitos

- Docker + Docker Compose
- Python ≥ 3.12 e deps do projeto (`uv sync --extra playground` ou `pip install -e ".[playground]"`)
- Credenciais Azure OpenAI (`AZURE_OPENAI_*`)

## Subir os bancos

```bash
cd playground
docker compose up -d
```

Portas no host: `15432` (main), `15433` (shard 1), `15434` (shard 2)
(evitam conflito com Postgres local na 5432).

Copie o env de exemplo e preencha a chave Azure:

```bash
cp .env.example .env
# edite AZURE_OPENAI_*
set -a && source .env && set +a
```

## Seed (gerador paramétrico)

Parâmetros em `seed_params.yaml` (flags CLI sobrescrevem):

```yaml
cnpjs: 3
por_cnpj: 3
seed: 42          # ignorado com --random
```

```bash
# na raiz do repo — regenera SQL + prompts.yaml e aplica nos Postgres
.venv/bin/python playground/seed_data.py --apply --dump-sql

# mais volume
.venv/bin/python playground/seed_data.py --cnpjs 20 --por-cnpj 10 --apply --dump-sql

# aleatório (sem seed fixo)
.venv/bin/python playground/seed_data.py --random --apply --dump-sql
```

O script sempre reescreve `prompts.yaml` com perguntas e `expected` derivados
dos dados gerados. O compose usa `seed/*.sql` só no first boot; depois use
`--apply` para resetar sem `docker compose down -v`.

O gabarito é impresso no stdout a cada execução.

## Rodar a UI

Na raiz do repositório (com as env vars carregadas):

```bash
uv sync --extra playground
.venv/bin/streamlit run playground/app.py
```

Use as **perguntas prontas** na sidebar e compare a resposta / tools com o
`expected` no painel de debug.

Cada turno também grava o mesmo payload de debug:

- no terminal (loguru `playground turn debug`)
- em `playground/logs/turns.jsonl` (uma linha JSON por turno — útil para
  revisar falhas e aprimorar prompts/agente)

## Layout

- Sidebar: status dos DBs, thread, perguntas prontas
- Centro: chat
- Direita: tool calls, SQL, shards, guardrail, expected
