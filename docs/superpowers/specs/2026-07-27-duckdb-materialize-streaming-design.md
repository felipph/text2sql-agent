# Design: materialize DuckDB sem carregar tudo em memória

**Data:** 2026-07-27  
**Escopo:** `txt2sql/db/duckdb_layer.py` — função `DuckDBSession.materialize`  
**Status:** aprovado em conversa; aguardando revisão do spec

## Problema

`materialize` hoje faz `result.fetchall()`, materializa todas as linhas em uma `list[tuple]` e só então chama `executemany`. Com `fetch_limit` padrão de 100 000, o pico de memória no processo Python é desnecessário: o destino já é o DuckDB in-memory, e as linhas podem ser gravadas em lotes.

## Objetivo

Gravar registros da origem no DuckDB **diretamente em lotes**, mantendo pico de memória ≈ tamanho de um lote, sem mudar a API pública de `DuckDBSession` nem adicionar dependências.

## Não-objetivos

- Não adicionar pandas/pyarrow.
- Não expor `batch_size` em `DuckDBConfig`/YAML nesta iteração.
- Não alterar triggers, `fetch_limit`, nomes lógicos ou o ciclo de vida efêmero da sessão.
- Não mudar a assinatura pública de `materialize` / `execute` / `close`.

## Abordagem escolhida

**`fetchmany` + `INSERT` por lote**, com constante interna `BATCH_SIZE = 5_000`.

Alternativas descartadas:

| Opção | Motivo do descarte |
| --- | --- |
| pyarrow/pandas streaming | Nova dependência sem ganho necessário para o limite atual |
| `batch_size` configurável | Flexibilidade prematura; constante interna basta |
| Insert linha a linha | Throughput ruim |

## Fluxo

1. Montar o mesmo `SELECT * FROM … [WHERE …] LIMIT fetch_limit` de hoje.
2. Abrir conexão na origem e executar o select.
3. Ler o **primeiro** lote com `fetchmany(BATCH_SIZE)`.
4. Se vazio: criar tabela com colunas `VARCHAR` (comportamento atual de tabela vazia) e marcar como materializada.
5. Se não vazio:
   - Inferir schema com `_infer_schema` a partir do primeiro lote.
   - `CREATE TABLE` no DuckDB.
   - `executemany` do primeiro lote.
   - Loop: `fetchmany` → `executemany` até lote vazio.
6. Acumular apenas um contador de linhas para o log — **não** reter todas as linhas em memória.
7. Adicionar o nome lógico a `_materialized`.

## Mudanças de código

Arquivo único principal: `txt2sql/db/duckdb_layer.py`.

- Constante de módulo `BATCH_SIZE = 5_000`.
- Reescrever o corpo de `materialize` para o fluxo acima.
- Refatorar `_create_table_from_rows` em helpers mais estreitos, por exemplo:
  - criar tabela vazia a partir de schema (ou VARCHAR se sem linhas);
  - inserir um lote via `executemany`.
- Manter `_infer_schema` (agora aplicado só ao primeiro lote, não a todas as linhas).

## Contratos preservados

- Idempotência: se `logical_name` já está em `_materialized`, retorna sem re-buscar.
- Nome da tabela DuckDB = `table_config.id`.
- Tipos inferidos a partir da primeira linha não-nula por coluna (mesma regra; agora limitada ao primeiro lote — aceitável e alinhado ao uso atual).
- Tabela vazia → todas as colunas `VARCHAR`.

## Testes

- Smoke existente (`smoke_test.py` materialize) deve continuar passando.
- Teste unitário preferível: engine SQLAlchemy SQLite in-memory com N linhas (`N > BATCH_SIZE`), `materialize`, depois `COUNT(*)` no DuckDB igual a `min(N, fetch_limit)`.

## Riscos e mitigações

| Risco | Mitigação |
| --- | --- |
| Inferência de tipo errada se o 1º lote só tiver NULL numa coluna | Mesmo risco já existia com “primeira não-nula”; se todas forem NULL no 1º lote, cai em VARCHAR — aceitável |
| Lote grande demais / pequeno demais | Constante 5 000; ajustar depois se profiling pedir |
| Cursor da origem esgota / fecha cedo | Manter `with source_engine.connect()` envolvendo todo o loop de fetch+insert |

## Fora de escopo futuro (não fazer agora)

- `batch_size` em config.
- Attach remoto / `COPY` nativo entre engines.
- Persistência DuckDB em disco.
