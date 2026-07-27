# Design: playground Streamlit + docker-compose de testes

**Data:** 2026-07-27  
**Escopo:** pasta `playground/` (compose, seed, YAML, Streamlit, prompts),
extra opcional `[playground]` no `pyproject.toml`, menção em docs de primeiros passos  
**Status:** aprovado (design)

## Problema

Não há forma prática de exercitar o agente ponta a ponta contra Postgres real
com sharding, DuckDB e guardrail, nem de inspecionar tool calls / SQL / shards
durante a interação. Os smokes usam SQLite + LLM falso e não substituem um
playground de verificação manual.

## Objetivo

Entregar um harness de teste manual:

1. **docker-compose** com 1 Postgres principal + 2 shards, seed determinístico.
2. **UI Streamlit** local: chat + painel de debug (tools, SQL, shards, guardrail).
3. **Perguntas prontas** com valores esperados para checar corretude.
4. **Script de seed** para gerar/aplicar/resetar os dados sem recriar volumes.

A biblioteca `txt2sql` **não muda**.

## Não-objetivos

- Empacotar Streamlit no docker-compose (roda só no host).
- Auth, multi-usuário, deploy de produção.
- Langfuse obrigatório.
- Espelhar 3 shards do `examples/recebiveis.yaml` (playground usa 2).
- Alterar API pública ou nós do grafo.

## Decisões

| Tema | Escolha |
|------|---------|
| Organização | Pasta `playground/` autocontida |
| Topologia | 1 main + 2 shards Postgres 16 |
| UI | Streamlit local, 3 colunas (sidebar / chat / debug) |
| Debug | Parser local das mensagens do estado; sem mudança na lib |
| Seed | Script Python (`--apply` / `--dump-sql`) + SQL no init do compose |
| Credenciais LLM | Env vars `AZURE_OPENAI_*` (como o restante do projeto) |
| Checkpointer | `MemorySaver` no app; `thread_id` por conversa |
| Deps | Extra `[playground]`: streamlit, psycopg |

## Estrutura

```text
playground/
  docker-compose.yml
  .env.example
  config.yaml              # dialect postgres; db_main + db_shard_1/2
  shard_resolver.py        # 000–499 → shard_1; 500–999 → shard_2
  seed_data.py             # gera/aplica dados + gabarito
  seed/                    # SQL gerado (init do compose)
    01_main.sql
    02_shard1.sql
    03_shard2.sql
  app.py                   # Streamlit
  debug_view.py            # extrai tool calls do turno
  prompts.yaml             # perguntas prontas + expected
  README.md
```

## Bancos e seed

### Containers

| Serviço | Porta host | Papel |
|---------|------------|-------|
| `db_main` | 15432 | `clientes` (não shardada) |
| `db_shard_1` | 15433 | `recebiveis_NNN` para prefixo CNPJ 000–499 |
| `db_shard_2` | 15434 | `recebiveis_NNN` para prefixo CNPJ 500–999 |

Credenciais locais fixas (ex.: user/pass/db `txt2sql` / `txt2sql` / `txt2sql`).
Env vars documentadas em `.env.example`:

- `MAIN_DB_URL=postgresql+psycopg://…@localhost:15432/txt2sql`
- `SHARD_1_DB_URL=…@localhost:15433/txt2sql`
- `SHARD_2_DB_URL=…@localhost:15434/txt2sql`

### Dados determinísticos

| CNPJ | Cliente | Shard / tabela | Linhas (valor, status) | Soma |
|------|---------|----------------|------------------------|------|
| `12345678000190` | ACME | shard_1 / `recebiveis_123` | 100 pago, 50 pendente, 25 pago | 175 |
| `55667788000111` | Beta | shard_2 / `recebiveis_556` | 200 pago, 80 pendente | 280 |
| `99988877000155` | Gama | shard_2 / `recebiveis_999` | 40 vencido | 40 |

### `seed_data.py`

- `--dump-sql`: escreve `seed/*.sql` (DDL + DML) sem conectar.
- `--apply`: conecta nos 3 bancos, `CREATE IF NOT EXISTS`, `TRUNCATE`, inserts.
- Sempre imprime o gabarito no stdout.
- Compose monta `seed/*.sql` em `/docker-entrypoint-initdb.d` (first boot).
- Os `seed/*.sql` **ficam versionados** no repo (gerados via `--dump-sql`) para
  `docker compose up` funcionar sem passo prévio.
- Re-seed sem recriar volumes: `python playground/seed_data.py --apply`.

### Resolver

Mesma ideia do exemplo de produto, com 2 faixas:

- prefixo `000–499` → `db_shard_1`, tabela `recebiveis_<prefix>`
- prefixo `500–999` → `db_shard_2`, tabela `recebiveis_<prefix>`

Referência no YAML: `playground.shard_resolver:resolve_cnpj_shard`
(ou caminho dotted equivalente com `PYTHONPATH` na raiz do repo).

## UI Streamlit

### Layout

1. **Sidebar:** status dos DBs (ping), path do YAML, `thread_id`, botão
   “Nova conversa”, lista de perguntas prontas.
2. **Centro:** histórico Human/AI + input.
3. **Direita:** debug do último turno — tool calls, SQL, shards resolvidos,
   mensagens de guardrail, `expected` da pergunta pronta (se houver).

### Wiring

1. `@st.cache_resource`: `load_config(config.yaml)` +
   `build_agent(config, checkpointer=MemorySaver())`.
2. Invoke: `agent.invoke({"messages": […]}, config={"configurable": {"thread_id": …}})`.
3. Histórico de chat: filtra Human/AI; tools ficam no painel.
4. Pergunta pronta: injeta o texto no fluxo e exibe `expected` no painel;
   comparação visual (sem assert automático).
5. Nova conversa: novo UUID em `thread_id`.

### `debug_view.py`

Percorre mensagens do turno e classifica:

- `resolve_shard`
- `materialize_sharded_table`
- `sql_db_query` / `sql_db_schema`
- falhas de guardrail (conteúdo da `ToolMessage`)

Sem hooks na lib.

## Perguntas prontas (`prompts.yaml`)

| Id | Intenção | Expected |
|----|----------|----------|
| `single_sum` | Soma recebíveis CNPJ ACME | `175` + uso de `resolve_shard` |
| `multi_sum` | Soma ACME + Beta | `455` + `materialize_sharded_table` |
| `join_vencido` | Razão social com recebível vencido | Gama |
| `guardrail_delete` | Pedido de DELETE | rejeição / não executar DML |

## Fluxo de uso

```text
cd playground && docker compose up -d
# (opcional) python seed_data.py --apply   # se volumes já existiam
export $(grep -v '^#' .env.example | xargs)   # + AZURE_OPENAI_*
cd .. && uv sync --extra playground
.venv/bin/streamlit run playground/app.py
```

## Erros e bordas

- DB inacessível: sidebar mostra falha; chat desabilitado ou mensagem clara.
- LLM/env faltando: erro explícito na UI ao construir o agente.
- Guardrail: ToolMessage de rejeição aparece no painel debug.
- Comparação expected vs resposta: manual (humano); sem auto-pass/fail.

## Testes

Harness manual — sem pytest obrigatório para a UI. Smoke opcional futuro:
script que sobe compose e chama `seed_data.py --apply` + ping.

## Docs

- `playground/README.md` com setup e gabarito.
- Linha em `docs/primeiros-passos.md` apontando para o playground.
- `.gitignore`: manter `.superpowers/` ignorado se ainda não estiver.
