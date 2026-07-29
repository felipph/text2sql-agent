# Solução de problemas

Guia diagnóstico (identificar o problema). Remediação prescritiva em produção: [runbook](runbook.md).

## Problema → Causa → Solução

**`ValueError: Banco '…': env var '…' não definida`**  
Causa: `connection_env` no YAML sem variável no processo.  
Solução: exporte a env var ou use `override_connections` em `load_config`.

**`ValueError` / erro Azure ao construir o LLM**  
Causa: bloco `llm` incompleto e `AZURE_OPENAI_*` ausentes.  
Solução: preencha YAML ou env vars — ver [variáveis de ambiente](../referencia/variaveis-de-ambiente.md).

**`ReadOnlyViolationError` / Policy Gate `rejected`**  
Causa: SQL com DML/DDL, múltiplos statements, keyword denylist, shard não resolvido ou agg em tabela `force_analytical`.  
Solução: reescreva como um único `SELECT`/`WITH … SELECT`; no dual-path confira `execution_path` e o IntentPlan. O guardrail/Policy Gate são fail-closed.

**Agente erra em tabela shardada / dados de outro CNPJ**  
Causa (dual-path): discriminador ausente ou errado nos filtros do `IntentPlan` → clarify ou binding incorreto.  
Causa (ReAct, `dual_path=False`): faltou `resolve_shard` ou houve tentativa de fan-out.  
Solução: garanta o discriminador na pergunta/`custom_section`; confira o resolver em `examples/shard_resolver_example.py`.

**Clarificação em loop / “não consigo esclarecer”**  
Causa: `Budget.max_clarifications` (default 2) esgotado, ou ambiguidade recorrente.  
Solução: refine a pergunta com o discriminador/métrica explícitos; verifique se o checkpointer + `Command(resume=...)` estão corretos.

**Interrupt de clarificação sem resume**  
Causa: `ask_clarification` usou `interrupt` mas o caller não trata `__interrupt__` / não chama `Command(resume=...)`.  
Solução: injete checkpointer e retome com o mesmo `thread_id` — ver [API](../referencia/api.md#hitl-clarificação). Sem checkpointer o grafo só emite a pergunta e encerra.

**Agregação rejeitada em tabela OLTP-hot**  
Causa: `tables[].duckdb.force_analytical` (ou `trigger: always`) + SQL com agg na origem.  
Solução: esperado — o path deve materializar e agregar no DuckDB; ajuste IntentPlan/rota, não “desligue” o gate sem ADR.

**Agregação lenta ou timeout no OLTP**  
Causa: tabela volumétrica sem `duckdb.enabled` / `force_analytical`, ou ReAct com `trigger` que não casa com a query.  
Solução: habilite DuckDB (`trigger: aggregation` ou `always`) e ajuste `fetch_limit` / `query_timeout`.

**`Catalog Error: Table with name … already exists` no retry DuckDB**  
Causa: falha parcial após `CREATE TABLE` na mesma `DuckDBSession`.  
Solução: no ReAct a sessão é por turno — reinicie o turno/`close()`. No dual-path, sufficiency gate deve refresh; se corrompida, use novo `thread_id` ou limpe o store.

**Testes / smoke não acham o pacote**  
Causa: ambiente sem install editable.  
Solução: `pip install -e ".[dev]"` ou `PYTHONPATH=.`.
