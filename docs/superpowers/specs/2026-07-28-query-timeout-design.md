# Design: timeout de execução em `sql_db_query`

**Data:** 2026-07-28  
**Escopo:** `config.py` (campos YAML), `db/registry.py` (`execute` + deadline cliente), `agent.py` (`execute_queries` / ToolMessage), testes, export opcional de `QueryTimeoutError`  
**Status:** implementado

## Problema

Hoje só existe `connect_timeout` (abertura de conexão, argumentos por driver). Queries SELECT longas em `sql_db_query` podem travar o turno indefinidamente, e não há um limite uniforme entre Postgres, SQLite, MSSQL, etc.

## Objetivo

Definir um **timeout de execução** para o caminho OLTP de `sql_db_query` que:

1. seja **independente do driver** (deadline no processo Python);
2. seja **configurável** (global + override por banco);
3. devolva erro amigável ao LLM via `ToolMessage`, sem derrubar o grafo;
4. tente **cancelar/invalidar** a conexão best-effort ao estourar.

## Não-objetivos (v1)

- Timeout em DuckDB analítico, materialização (`materialize`) ou discovery de schema.
- `SET statement_timeout` / timeouts nativos por dialeto.
- Mudar `connect_timeout` ou a API de `build_agent` / `load_config` além dos novos campos.
- Garantir cancelamento no servidor em todos os drivers (apenas best-effort no cliente).

## Decisões fechadas

| Tema | Escolha |
|------|---------|
| Escopo | Só `sql_db_query` → `DatabaseRegistry.execute` (OLTP) |
| Config | Global `agent.query_timeout` + override `databases[].query_timeout` |
| Default | `30` segundos |
| Desligar | `0` no escopo efetivo |
| Mecanismo | Thread worker + `join(timeout)` no cliente |
| Cancel | Best-effort: `cursor.cancel()` se existir; senão fechar / `invalidate` |
| Erro | `QueryTimeoutError` → `ToolMessage` amigável; turno continua |
| Contagem | Timeout conta como página em `max_pages` (como qualquer execução) |

## Configuração

### Campos

- `AgentConfig.query_timeout: int = 30`
- `DatabaseConfig.query_timeout: int | None = None` — `None` herda o global

### Resolução efetiva

```
efetivo(db) =
  db.query_timeout if db.query_timeout is not None
  else agent.query_timeout
```

- `efetivo == 0` → sem deadline (comportamento atual).
- Valor negativo em qualquer campo → `ValueError` em `load_config` / `__post_init__`.

### YAML

```yaml
agent:
  query_timeout: 30

databases:
  - id: db_main
    connection_env: MAIN_DB_URL
    query_timeout: 60   # opcional; sobrescreve o global
```

## Arquitetura

```
sql_db_query → check_query → execute_queries
                                 │
                                 ├─ use_duckdb → DuckDBSession (sem timeout v1)
                                 └─ OLTP → registry.execute(database_id, sql)
                                              │
                                              ├─ efetivo == 0 → execute síncrono
                                              └─ efetivo > 0 → worker + join(timeout)
                                                   ├─ ok → rows
                                                   └─ timeout → cancel best-effort
                                                                → QueryTimeoutError
                                                                → ToolMessage
```

### `DatabaseRegistry.execute`

1. Resolve `timeout` efetivo para `database_id`.
2. Abre conexão no thread do caller.
3. Se `timeout == 0`: `conn.execute` + `fetchall` como hoje.
4. Senão: submete execute+fetchall a um worker thread; `join(timeout)`.
5. Sucesso: retorna `list[dict]` como hoje.
6. Timeout:
   - best-effort cancel/invalidate/close da conexão (não deve bloquear o caller por tempo indefinido);
   - levanta `QueryTimeoutError` com `database_id` e segundos do limite.

A conexão usada no caminho com timeout não deve voltar “limpa” ao pool após invalidate — o pool descarta conexões invalidadas.

### Agente (`execute_queries`)

No ramo OLTP, capturar `QueryTimeoutError` além de `SQLAlchemyError` / `ReadOnlyViolationError`:

```
ERRO: query excedeu o timeout de N segundos. Simplifique a consulta ou filtre mais.
```

O grafo volta a `generate_query`; o LLM pode corrigir. `page_count` incrementa.

### Exceção pública

```python
class QueryTimeoutError(Exception):
    """Query SELECT excedeu o query_timeout configurado."""
```

Definida em `db/registry.py`. Exportada em `txt2sql.__init__` (`from txt2sql import QueryTimeoutError`).

## Testes

1. **Config:** herança global, override por banco, `0` desliga, negativo rejeita.
2. **Registry:** timeout curto + worker que dorme além do limite → `QueryTimeoutError`; `query_timeout: 0` completa.
3. **Agente:** timeout vira `ToolMessage` de erro; grafo não aborta; `page_count` sobe.

Testes de registry preferem mock/sleep controlado (não depender de query lenta real no CI).

## Riscos e mitigação

| Risco | Mitigação |
|-------|-----------|
| Query continua no servidor após timeout cliente | Cancel/invalidate best-effort; documentar limite; híbrido dialeto fica fora de v1 |
| Thread órfã após timeout | Worker daemon ou join curto pós-cancel; conexão invalidada |
| Pool com conexão ruim | Sempre `invalidate`/dispose da conexão no timeout |

## Fora de escopo explícito

- DuckDB / materialize / schema discovery timeouts.
- Timeouts nativos por dialeto.
- Alterar semântica de `connect_timeout`.
