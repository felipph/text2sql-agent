# Expose Hardcoded Knobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expor budgets, limites, messages e prompts via YAML; remover knobs mortos com fail-closed; alinhar prompt SQL ao read-only.

**Architecture:** Novos dataclasses em `config.py` (`BudgetConfig`, `MessagesConfig`, `PromptsConfig`) alimentam `AgentConfig`; `graph.init_state` monta `Budget` a partir da config; Policy/materialize/export/answer leem knobs da config. Defaults PT-BR genéricos com `{discriminator}` / `{url}`.

**Tech Stack:** Python, dataclasses, pytest, YAML

**Spec:** `docs/superpowers/specs/2026-08-03-expose-hardcoded-knobs-design.md`

---

### Task 1: Config dataclasses + load_config breaking

- [x] Parse `agent.budget`, `sample_rows`, `query_max_rows`, `max_intent_retries`, `messages`, `prompts`, `export_detect_keywords`
- [x] Parse `analytics.batch_size`, `materialize_sample_rows`
- [x] Reject `top_k`, `max_pages`, `sample_rows_in_table_info`
- [x] Remove fields from `AgentConfig`
- [x] Tests for reject + parse defaults + custom values

### Task 2: Wire Budget + Policy + batch + retries + mat counter

- [x] `init_state` builds Budget from `agent_config`
- [x] `check_sql_plan(..., max_rows=agent_config.query_max_rows)`
- [x] `materialize` uses `batch_size` from config; increment `total_rows_materialized`; check exhausted
- [x] `MAX_INTENT_RETRIES` from config
- [x] materialize sample LIMIT from `analytics.materialize_sample_rows`

### Task 3: Messages + keywords + prompts + DML fix

- [x] Resolve messages from config with defaults
- [x] `{discriminator}` helper
- [x] `detect_wants_export(keywords=...)`
- [x] `intent_extra` / `answer_rules`
- [x] Fix `_section_general_rules` DML/DDL claim

### Task 4: Docs + suite + commit/push

- [x] Update examples/docs/playground
- [x] Full pytest (ignore known guardrail_break)
- [ ] Commit all related work + push
