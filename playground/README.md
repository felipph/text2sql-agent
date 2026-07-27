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

## Seed

O compose já aplica `seed/*.sql` no first boot. Para regenerar o SQL ou resetar
dados sem recriar volumes:

```bash
# na raiz do repo
.venv/bin/python playground/seed_data.py --dump-sql playground/seed
.venv/bin/python playground/seed_data.py --apply
```

### Gabarito

| CNPJ | Cliente | Soma |
|------|---------|------|
| `12345678000190` | ACME | 175 |
| `55667788000111` | Beta | 280 |
| `99988877000155` | Gama | 40 |
| ACME + Beta | — | 455 |

Cliente com recebível `vencido` → **Gama**.

## Rodar a UI

Na raiz do repositório (com as env vars carregadas):

```bash
uv sync --extra playground
.venv/bin/streamlit run playground/app.py
```

Use as **perguntas prontas** na sidebar e compare a resposta / tools com o
`expected` no painel de debug.

## Layout

- Sidebar: status dos DBs, thread, perguntas prontas
- Centro: chat
- Direita: tool calls, SQL, shards, guardrail, expected
