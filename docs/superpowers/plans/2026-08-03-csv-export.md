# CSV Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Export CSV denormalizado sob demanda via COPY streaming + cleanup injetável.

**Architecture:** `ExportConfig` + `export_csv.py` (COPY/cleanup) + `build_denormalized_select` + nó `export_csv` no grafo quando `wants_export`.

**Tech Stack:** Python, DuckDB COPY, pytest

**Spec:** `docs/superpowers/specs/2026-08-03-csv-export-design.md`

---

### Task 1: ExportConfig + export_csv module

- [x] Create: `txt2sql/export_csv.py`, `tests/test_export_csv.py`
- [x] Modify: `txt2sql/config.py`, `txt2sql/__init__.py`

### Task 2: build_denormalized_select + IntentPlan.wants_export

- [x] Modify: `txt2sql/intent.py`, `txt2sql/prompts.py`
- [x] Create: select builder in `export_csv.py` or `txt2sql/export_sql.py`

### Task 3: Graph node export_csv + answer link

- [x] Modify: `txt2sql/graph.py`, `tests/test_graph_dual_path.py`
- [x] Docs: api.md snippet; playground config example optional
