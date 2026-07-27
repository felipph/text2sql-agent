# Variáveis de ambiente

## LLM (Azure OpenAI)

Usadas por `txt2sql.llm.build_llm` quando o bloco `llm` do YAML omite o campo.

| Variável | Obrigatória | Default | Descrição |
|----------|-------------|---------|-----------|
| `AZURE_OPENAI_API_KEY` | sim* | — | Chave da API |
| `AZURE_OPENAI_ENDPOINT` | sim* | — | Endpoint do recurso Azure |
| `AZURE_OPENAI_DEPLOYMENT` | sim* | — | Nome do deployment |
| `AZURE_OPENAI_MODEL` | não | = deployment | Nome do modelo |
| `AZURE_OPENAI_API_VERSION` | não | — | Versão da API (ou YAML `llm.api_version`) |

\* Obrigatória na prática se não estiver no YAML `llm`.

## Bancos de dados

Não há nomes fixos na biblioteca. Cada `databases[].connection_env` no YAML aponta para uma env var. No exemplo `examples/recebiveis.yaml`:

| Variável (exemplo) | Obrigatória | Default | Descrição |
|--------------------|-------------|---------|-----------|
| `MAIN_DB_URL` | se usada no YAML | — | Banco principal / cadastro |
| `SHARD_1_DB_URL` | se usada no YAML | — | Shard 1 |
| `SHARD_2_DB_URL` | se usada no YAML | — | Shard 2 |
| `SHARD_3_DB_URL` | se usada no YAML | — | Shard 3 |

Precedência ao resolver connection string: `override_connections` → `connection_string` no YAML → env var `connection_env`.

## Tracing (Langfuse)

| Variável | Obrigatória | Default | Descrição |
|----------|-------------|---------|-----------|
| `LANGFUSE_PUBLIC_KEY` | para habilitar | — | Chave pública |
| `LANGFUSE_SECRET_KEY` | para habilitar | — | Chave secreta |
| `LANGFUSE_HOST` | não | `https://cloud.langfuse.com` | Host Langfuse |

Tracing só ativa se public + secret estão definidos **e** o extra `langfuse` está instalado (`pip install -e ".[langfuse]"`).
