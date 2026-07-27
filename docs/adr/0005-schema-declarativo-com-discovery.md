---
status: accepted
date: 2026-07-27
---

# ADR-0005: Schema declarativo com discovery opcional

## Context and Problem Statement

O LLM precisa de contexto de colunas e significado de negócio. Reflection pura não traz descrições; declarar tudo à mão em código Python é verboso.

## Considered Options

- Só discovery SQLAlchemy (sem descrições de negócio)
- Só schema declarativo obrigatório no YAML
- Híbrido: com `columns` → declarativo; sem `columns` → discovery

## Decision Outcome

Chosen option: **híbrido YAML**, because tabelas críticas (shard/DuckDB) ganham descrições ricas e tabelas simples continuam baratas via reflection.

## Consequences

**Positive:**
- Glossário e `description` entram no prompt quando existem
- Onboarding rápido com discovery para POCs

**Negative:**
- Dois modos para o leitor da config entender
- Discovery depende do `database` de referência estar acessível

**Neutral:**
- `sample_rows` controla amostras em ambos os modos
