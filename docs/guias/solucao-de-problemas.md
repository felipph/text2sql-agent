# Solução de problemas

Guia diagnóstico (identificar o problema). Remediação prescritiva em produção: [runbook](runbook.md).

## Problema → Causa → Solução

**`ValueError: Banco '…': env var '…' não definida`**  
Causa: `connection_env` no YAML sem variável no processo.  
Solução: exporte a env var ou use `override_connections` em `load_config`.

**`ValueError` / erro Azure ao construir o LLM**  
Causa: bloco `llm` incompleto e `AZURE_OPENAI_*` ausentes.  
Solução: preencha YAML ou env vars — ver [variáveis de ambiente](../referencia/variaveis-de-ambiente.md).

**`ReadOnlyViolationError`**  
Causa: SQL com DML/DDL, múltiplos statements ou keyword denylist.  
Solução: reescreva como um único `SELECT`/`WITH … SELECT`. O guardrail é fail-closed.

**Agente erra em tabela shardada / dados de outro CNPJ**  
Causa: faltou `resolve_shard` ou houve tentativa de fan-out.  
Solução: garanta discriminator no prompt (`custom_section`); confira o resolver em `examples/shard_resolver_example.py`.

**Agregação lenta ou timeout no OLTP**  
Causa: tabela volumétrica sem `duckdb.enabled` ou `trigger` que não casa com a query.  
Solução: habilite DuckDB com `trigger: aggregation` (ou `always`) e ajuste `fetch_limit`.

**`Catalog Error: Table with name … already exists` no retry DuckDB**  
Causa: falha parcial após `CREATE TABLE` na mesma `DuckDBSession`.  
Solução: a sessão é por turno — reinicie o turno/`close()`; não reutilize sessão corrompida.

**Testes / smoke não acham o pacote**  
Causa: ambiente sem install editable.  
Solução: `pip install -e ".[dev]"` ou `PYTHONPATH=.`.
