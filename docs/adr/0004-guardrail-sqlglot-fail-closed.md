---
status: accepted
date: 2026-07-27
---

# ADR-0004: Guardrail read-only fail-closed com sqlglot

## Context and Problem Statement

O LLM gera SQL contra bancos reais. Qualquer DML/DDL ou statement múltiplo é risco inaceitável.

## Considered Options

- Confiar só em `read_only` do usuário / permissões do DB role
- Validação textual por denylist de keywords
- Parse AST com sqlglot (fail-closed) + denylist complementar + listener `before_cursor_execute`

## Decision Outcome

Chosen option: **AST sqlglot fail-closed + denylist + listener no engine**, because validação só por texto é fácil de burlar e permissões de DB não cobrem erros do modelo a tempo.

## Consequences

**Positive:**
- Apenas um `SELECT`/`WITH … SELECT` por statement
- Defesa em profundidade (lib + DB role)

**Negative:**
- Possíveis falsos positivos em dialetos exóticos
- Dependência de sqlglot e do dialeto configurado

**Neutral:**
- Allowlist opcional de tabelas via qualify
