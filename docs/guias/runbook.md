# Runbook

Procedimentos operacionais para quem integra ou opera um agente baseado em `txt2sql`. A biblioteca não expõe HTTP próprio — health/logs vivem na app hospedeira.

## Health check

Na app consumidora, valide:

1. `load_config(path)` sem exceção.
2. `build_agent(config)` sem exceção (LLM alcançável).
3. Um `invoke` curto com pergunta conhecida (canário).

Localmente, use os smokes:

```bash
.venv/bin/python smoke_test.py
.venv/bin/python smoke_test_graph.py
```

Esperado: exit 0 e mensagem de sucesso no smoke de dados.

## Logs

A lib usa **loguru**. Na app hospedeira, configure sink/nível do loguru ou redirecione stderr.

Padrões úteis:

* `Materializando '…' no DuckDB` — início de materialização
* `Tabela '…' materializada com N linha(s)` — fim OK
* `ReadOnlyViolationError` / mensagens do guardrail — SQL rejeitado
* Warnings `LANGFUSE_* definidos, mas o pacote 'langfuse' não está instalado`

## Incidentes comuns

### Sintoma: erros em massa de conexão / timeout SQL

1. Confirme env vars `connection_env` no processo da app.
2. Teste a connection string fora do agente (cliente SQL).
3. Se só um shard falha, isole `database_id` nos logs e o resolver.
4. Verificação: canário `invoke` no shard afetado.

### Sintoma: respostas vazias / “nenhuma linha” após deploy de YAML

1. Diff do YAML (ids de tabela, `name` físico, schema).
2. Confira se discovery aponta ao `database` certo.
3. Para shards: rode o resolver manualmente com o discriminador do caso.
4. Verificação: tool `sql_db_schema` retorna colunas esperadas.

### Sintoma: Latência alta em agregações

1. Verifique `tables[].duckdb.enabled` e se o `trigger` casa com a SQL.
2. Ajuste `fetch_limit` se estiver puxando volume excessivo.
3. Confirme nos logs que o caminho `materialize_duckdb` foi usado.
4. Verificação: mesma pergunta com tempo aceitável e log de materialização.

### Sintoma: escrita acidental bloqueada (usuários reportam erro de SQL)

1. Esperado se `read_only: true` — não “libere” escrita sem ADR.
2. Se SELECT legítimo foi bloqueado, capture o SQL e abra issue (possível falso positivo do denylist/AST).
3. Verificação: `validate_sql(sql, dialect=…)` em REPL reproduz o erro.

## Rollback

Siga [Deploy → Rollback](deploy.md#rollback): pin anterior da biblioteca e/ou revert do YAML/env na app hospedeira.

## Escalation

<!-- TODO: preencher contatos L1/L2 e critérios de page quando o on-call existir -->

* L1 — time da app consumidora (config, rede, deploy)
* L2 — maintainers de `txt2sql` (comportamento da lib, guardrail, DuckDB)
