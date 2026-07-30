# txt2sql

Biblioteca Text-to-SQL com grafo dual-path (simple | analytical), sharding determinístico e DuckDB intermediário por `thread_id`.

## Language

**IntentPlan**:
Plano estruturado da pergunta do usuário (filtros, métricas, joins) antes de qualquer SQL.
_Avoid_: query plan, user query

**ShardResult**:
Par `(database_id, table_name)` físico retornado pelo resolver de domínio para um discriminador.
_Avoid_: shard location, routing result

**ShardBinding**:
Binding já resolvido de uma tabela lógica a um físico, com o valor do discriminador.
_Avoid_: ShardResult (é o retorno cru do resolver; Binding é o artefato do roteamento)

**ShardRouting**:
Conjunto de `ShardBinding`s para o turno (`none` | `single` | `multi`).
_Avoid_: resolve_shard tool output

**Fan-in**:
Materialização de vários bindings shardados numa única tabela lógica no DuckDB.
_Avoid_: fan-out, multi_shard, UNION across shards

**MaterializeOutcome**:
Resultado de materializar o path analítico: catálogo atualizado, sample de rows e erro opcional.
_Avoid_: ExecutionResult (é o artefato de execução SQL do grafo, não do extract)

**DuckDBCatalog**:
Inventário das tabelas já materializadas na sessão DuckDB do `thread_id`, com provenance (`source_queries`).
_Avoid_: schema, cache metadata

**SufficiencyDecision**:
Decisão determinística (ou pós-LLM) se o catálogo cobre o IntentPlan: `reuse` | `refresh` | `unknown`.
_Avoid_: GateDecision (tipo LLM legado; o domínio usa SufficiencyDecision)

**Analytical planning**:
Orquestração gate → plano de materialização → check de cobertura, com fallbacks LLM como adapters.
_Avoid_: sufficiency gate alone (é só o primeiro passo)

**Dual-path**:
Grafo tipado que separa path *simple* (SQL na origem) de *analytical* (extract → DuckDB → SQL analítico).
_Avoid_: ReAct, tool loop, dual_path=False
