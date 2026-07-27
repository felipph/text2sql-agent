# Design: descrição negocial de tabelas

**Data:** 2026-07-27  
**Escopo:** `TableConfig.description`, `SchemaLoader`, `Txt2SqlPromptBuilder`, exemplos YAML  
**Status:** implementado

## Problema

Colunas e relacionamentos já expõem `description` no YAML e no contexto do LLM.
Tabelas só aparecem com ID/nome físico — o agente carece de semântica de negócio
no nível da tabela (especialmente em discovery, sem `columns`).

## Objetivo

Campo opcional `tables[].description` em qualquer tabela do YAML, injetado:

1. no system prompt (seção própria “Semântica das tabelas”);
2. na saída do `SchemaLoader` (`load_schema` + tool `sql_db_schema`).

## Não-objetivos

- Descrição obrigatória.
- Discovery de `COMMENT ON TABLE` / metadados do banco.
- Alterar guardrail, sharding ou DuckDB.
- Mudança de API pública além do campo novo em `TableConfig`.

## Abordagem

Campo `description: str | None = None` em `TableConfig`, espelhando `ColumnConfig`.

### Config

- YAML: `tables[].description` opcional.
- `load_config` passa `t.get("description")`.
- `is_declarative` permanece baseado só em `columns`.

### SchemaLoader

Após a linha `Tabela: …`, se houver descrição:

```
Tabela: clientes  (física: public.clientes, banco: db_main)
  Descrição: Cadastro de clientes …
```

Válido para declarativo e discovery.

### Prompt

Nova seção imediatamente antes de “Semântica das colunas”, emitida só se existir
ao menos uma tabela com `description`:

```
## N. Semântica das tabelas
- `clientes`: …
- `recebiveis`: …
```

Seções seguintes renumeradas (+1).

### Exemplos / docs / testes

- Preencher `description` em `examples/recebiveis.yaml` e `examples/diario.yaml`.
- Atualizar `docs/guias/configuracao.md`.
- Testes: parse, SchemaLoader, PromptBuilder.
