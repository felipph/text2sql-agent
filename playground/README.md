# Playground txt2sql

Harness manual: Postgres (1 main + 2 shards) + UI Streamlit com painel de debug.

## Pré-requisitos

- Docker + Docker Compose
- Python ≥ 3.12 e deps do projeto (`uv sync --extra playground` ou `pip install -e ".[playground]"`)
- Credenciais Azure OpenAI (`AZURE_OPENAI_*`)
- (Opcional) Langfuse — `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` (o extra `playground` já inclui o pacote `langfuse`)

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
# opcional: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
```

O `app.py` e o `seed_data.py` carregam `playground/.env` automaticamente
(vars já exportadas no shell têm prioridade).

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

### Langfuse

Com `LANGFUSE_PUBLIC_KEY` e `LANGFUSE_SECRET_KEY` no `.env`, cada
`agent.invoke` envia um trace (tag `playground`, `session_id` = thread da
conversa). A sidebar mostra se o tracing está on/off.

```bash
# no .env
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
# LANGFUSE_HOST=https://cloud.langfuse.com   # ou self-hosted
```

Reinicie o Streamlit após editar o `.env`.

## Layout

- Sidebar: status dos DBs, Langfuse, thread, perguntas prontas
- Centro: chat
- Direita: path, SQL, provenance, expected
