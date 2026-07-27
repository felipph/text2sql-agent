---
status: accepted
date: 2026-07-27
---

# ADR-0001: Biblioteca standalone com LangGraph (sem app HTTP embutida)

## Context and Problem Statement

Precisávamos de um agente Text-to-SQL reutilizável em apps diferentes. Embutir FastAPI ou um servidor próprio acoplaria deploy, auth e ciclo de vida à lib.

## Considered Options

- Biblioteca Python pura com grafo LangGraph compilado pelo caller
- Serviço HTTP/FastAPI com endpoints `/query`
- Plugin acoplado a uma aplicação hospedeira específica

## Decision Outcome

Chosen option: **biblioteca standalone + LangGraph**, because o caller controla checkpointer, HTTP e credenciais, e a lib permanece testável sem servidor.

### Pros and Cons of the Options

#### Biblioteca + LangGraph
- Good, because desacopla transporte e persistência de sessão
- Bad, because cada app precisa montar o wiring (`build_agent`, checkpointer)

#### Serviço HTTP
- Good, because onboarding uniforme via REST
- Bad, because impõe stack de deploy e modelo de auth

## Consequences

**Positive:**
- `pip install` + YAML basta para embutir o agente
- Checkpointer externo explícito (sem surpresa de estado)

**Negative:**
- Não há health endpoint nativo — a app hospedeira deve expor

**Neutral:**
- Documentação de “deploy” descreve release do pacote, não de um container da lib
