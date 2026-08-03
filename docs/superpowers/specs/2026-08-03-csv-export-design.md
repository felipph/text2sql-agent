# Design: export CSV denormalizado sob demanda

**Data:** 2026-08-03  
**Status:** aprovado (brainstorming)  
**Motivo:** permitir baixar a lista bruta (sem agregação) dos dados que alimentaram a análise, via URL de download.

## Decisões fechadas

| Tema | Escolha |
|------|---------|
| Gatilho | Sob demanda (usuário pede exportar/baixar/CSV) |
| Conteúdo | Um CSV denormalizado (JOIN no DuckDB), sem agregação |
| Serving HTTP | Lib só grava + monta URL; app hospedeira serve `export.dir` |
| Cleanup TTL | Função pública `cleanup_expired_exports`; **app agenda** (sem thread na lib) |
| I/O | Streaming: preferir DuckDB `COPY ... TO`; nunca `fetchall` do dump |
| Separador | Configurável (`export.delimiter`, default `,`) |

## Problema

Após path analytical, o usuário vê só um `sample` agregado. Precisa da lista completa de títulos/linhas que foram sumarizados, em CSV, sem carregar tudo em memória Python, com URL estável e limpeza por TTL.

## Configuração

Bloco opcional em `agent.export` (YAML → `ExportConfig` em `AgentConfig`):

```yaml
agent:
  export:
    enabled: false          # default off
    dir: /var/txt2sql/exports
    base_url: https://app.example/exports   # sem exigir barra final
    ttl_seconds: 86400      # 24h
    delimiter: ","          # ou ";" para Excel PT-BR
    max_rows: 500000
```

Validação:
- Se `enabled=true`: `dir` e `base_url` obrigatórios; `ttl_seconds >= 1`; `delimiter` string não vazia (1 caractere recomendado); `max_rows >= 1`.
- `dir` criado se não existir (ao exportar).

## API pública

```python
# txt2sql/export_csv.py

@dataclass(frozen=True)
class ExportResult:
    path: Path
    url: str
    row_count: int
    truncated: bool
    filename: str

def export_denormalized_csv(
    *,
    session: DuckDBSession,
    select_sql: str,          # SELECT denormalizado, sem agg
    config: ExportConfig,
    thread_id: str,
) -> ExportResult:
    """Grava CSV em streaming (COPY TO) e retorna path + URL."""

def cleanup_expired_exports(dir: Path | str, ttl_seconds: int) -> int:
    """Remove arquivos com mtime mais antigo que ttl. Retorna qtde removida.
    Agendamento fica a cargo do app (cron / APScheduler / etc.).
    """

def build_export_url(base_url: str, filename: str) -> str: ...
```

### Escrita eficiente

1. **Preferido:** DuckDB  
   `COPY ({select_sql_limited}) TO '{abs_path}' (HEADER, DELIMITER '{delim}', FORMAT CSV)`  
   O engine escreve em streaming; a lib não acumula rows.
2. Contagem: `SELECT COUNT(*) FROM ({select_sql})` **ou** row count reportado pelo COPY se disponível; se `count > max_rows`, aplicar `LIMIT max_rows` e `truncated=True`.
3. **Proibido:** `session.execute(select)` trazendo todas as linhas para `list` antes de escrever.
4. Fallback só se `COPY TO` indisponível no ambiente: `fetchmany(batch_size)` + `csv.writer` em arquivo aberto (batch pequeno, ex. 5_000).

Nome do arquivo: `{safe_thread_id}_{uuid4().hex}.csv`.

URL: `{base_url.rstrip('/')}/{filename}`.

## Integração no grafo

```
interpret_intent
  → se wants_export (heurística/prompt) e export.enabled
      → resolve_and_route / sufficiency (reuse se catálogo cobrir tabelas do join)
      → se catálogo insuficiente → materialize (mesmo pipeline analytical)
      → export_csv  (SELECT denormalizado → COPY → state.export_url)
      → answer (cita link; aviso se truncated)
  → senão fluxo normal
```

### Detecção de export

- Prompt de intent: se o usuário pedir exportar/baixar/CSV/planilha da lista completa, marcar intenção de export (campo no IntentPlan **ou** rota derivada de heurística textual + `execution_path`/`intent_route`).
- Preferência de implementação: campo opcional `IntentPlan.wants_export: bool = False` (structured output) + fallback heurístico em keywords (`exportar`, `csv`, `baixar planilha`, `lista completa`) se o LLM omitir.
- Follow-up no mesmo `thread_id` com DuckDB já materializado → `reuse` + export sem rematerializar quando sufficiency cobrir.

### SELECT denormalizado

- Sem `SUM`/`COUNT`/`GROUP BY`/`DISTINCT` agregador desnecessário: projeção de colunas das tabelas envolvidas + JOINs do IntentPlan / `RelationshipConfig`.
- Geração: template determinístico a partir de `joins` + colunas do catálogo/schema quando possível; LLM só como fallback para o SQL de export (validado: read-only, só nomes lógicos DuckDB).
- Escopo = dados **já no DuckDB da sessão** (respeita `max_shards` / partial da materialização atual). Aviso natural se partial.

### State

```python
export_url: str | None
export_result: ExportResult | dict | None  # path, row_count, truncated (trace)
```

`answer_provenance` pode espelhar `export_url` para debug; a mensagem ao usuário inclui o link, **sem** bloco de proveniência SQL.

## Erros (respostas naturais)

| Caso | Comportamento |
|------|----------------|
| `enabled=false` / config incompleta | Não exporta; informa que exportação não está disponível |
| Catálogo vazio / tabelas faltando | Pede análise prévia ou materializa se o intent de export já trouxer entidades suficientes |
| COPY/disco falha | `export_url=None`; mensagem de falha sem inventar URL |
| `truncated` | Link válido + aviso: lista limitada neste turno; sugerir recorte |

## Scheduler (app)

A lib **não** sobe thread. Documentar:

```python
from txt2sql import cleanup_expired_exports
# no cron do app:
cleanup_expired_exports(cfg.export.dir, cfg.export.ttl_seconds)
```

Playground (opcional, fora do núcleo): exemplo de servir `export.dir` + chamada periódica de cleanup — pode ficar para task separada se não couber no MVP da lib.

## Testes

- Unit `export_denormalized_csv`: arquivo criado; conteúdo com delimiter `;`; URL correta; não usa fetchall (mock/spy).
- Unit `cleanup_expired_exports`: remove só mtime antigo.
- Unit `build_export_url` / validação `ExportConfig`.
- Graph: “exporte em CSV” com sessão DuckDB pré-materializada → `export_url` setado; SQL de export sem agg.
- Graph: export desabilitado → sem arquivo.
- Regression: pergunta analítica normal não cria CSV.

## Fora de escopo

- Servidor HTTP embutido na lib
- Zip multi-tabela / múltiplos CSVs
- Thread daemon de cleanup na lib
- Export do `sample` agregado (só denormalizado bruto)
- Alterar `max_shards` automaticamente no export

## Critério de sucesso

Usuário pede “exporte a lista completa em CSV” após (ou junto com) análise com dados no DuckDB → recebe URL downloadável; arquivo no `dir` com separador configurado; memória estável em dumps grandes; app consegue limpar via `cleanup_expired_exports`.
