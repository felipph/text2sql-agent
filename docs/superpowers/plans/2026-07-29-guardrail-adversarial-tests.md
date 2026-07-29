# Guardrail Adversarial Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Suite pytest de regressão + red team inventivo para `validate_sql`, revelando buracos sem alterar `guardrail.py`.

**Architecture:** Dois arquivos — `test_guardrail.py` (contrato estável, deve ficar verde) e `test_guardrail_break.py` (parametrizado por `case_id`; cada payload deve levantar `ReadOnlyViolationError`; se não levantar = FAIL = buraco documentado). Zero mudanças em produção.

**Tech Stack:** Python 3.12+, pytest, sqlglot (via `validate_sql`), dialetos `None` / `postgres` / `tsql`.

**Spec:** `docs/superpowers/specs/2026-07-29-guardrail-adversarial-tests-design.md`

**Note:** Commits só se o usuário pedir. Não usar `xfail`. Não editar `txt2sql/guardrail.py`.

---

## File map

| File | Responsibility |
| --- | --- |
| `tests/test_guardrail.py` | Regressão: aprova SELECT/CTE/UNION; rejeita DML óbvio, vazio, multi-stmt, denylist, allowlist |
| `tests/test_guardrail_break.py` | Red team: ~11 categorias × 3–8 payloads; `pytest.raises(ReadOnlyViolationError)` |

Referência de comportamento atual: `txt2sql/guardrail.py`, `smoke_test.py` (seção Guardrail).

---

### Task 1: Regressão `test_guardrail.py`

**Files:**
- Create: `tests/test_guardrail.py`

- [ ] **Step 1: Criar o arquivo completo**

```python
"""Regressão do guardrail read-only (`validate_sql`).

Contrato estável — deve ficar verde. Controles positivos e rejeições óbvias.
Ataques inventivos ficam em ``test_guardrail_break.py``.
"""

from __future__ import annotations

import pytest

from txt2sql.guardrail import ReadOnlyViolationError, validate_sql


# --- Aprovações ------------------------------------------------------------- #


def test_select_simples_aprovado() -> None:
    sql = "SELECT id FROM clientes"
    assert validate_sql(sql) == sql


def test_with_select_aprovado() -> None:
    sql = "WITH c AS (SELECT id FROM clientes) SELECT * FROM c"
    assert validate_sql(sql) == sql


def test_union_de_selects_aprovado() -> None:
    sql = "SELECT id FROM a UNION ALL SELECT id FROM b"
    assert validate_sql(sql) == sql


def test_allowlist_tabela_no_escopo() -> None:
    sql = "SELECT a FROM permitida"
    assert validate_sql(sql, allowed_tables=["permitida"]) == sql


def test_allowlist_cte_local_nao_exige_escopo() -> None:
    sql = "WITH tmp AS (SELECT 1 AS x) SELECT x FROM tmp"
    assert validate_sql(sql, allowed_tables=["clientes"]) == sql


# --- Rejeições óbvias ------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "\n\t",
    ],
)
def test_rejeita_vazio(sql: str) -> None:
    with pytest.raises(ReadOnlyViolationError, match="vazia"):
        validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "CREATE TABLE t (id INT)",
        "TRUNCATE TABLE t",
        "ALTER TABLE t ADD COLUMN x INT",
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET x = 1",
    ],
)
def test_rejeita_dml_ddl_toplevel(sql: str) -> None:
    with pytest.raises(ReadOnlyViolationError):
        validate_sql(sql)


def test_rejeita_multi_statement() -> None:
    with pytest.raises(ReadOnlyViolationError):
        validate_sql("SELECT * FROM t; DROP TABLE t")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id INTO novo FROM t",
        "EXEC sp_who",
        "EXECUTE xp_cmdshell 'dir'",
        "WITH c AS (DELETE FROM t RETURNING *) SELECT * FROM c",
    ],
)
def test_rejeita_denylist_e_cte_dml_smoke(sql: str) -> None:
    with pytest.raises(ReadOnlyViolationError):
        validate_sql(sql)


def test_allowlist_rejeita_tabela_fora() -> None:
    with pytest.raises(ReadOnlyViolationError, match="fora do escopo"):
        validate_sql("SELECT a FROM proibida", allowed_tables=["permitida"])
```

- [ ] **Step 2: Rodar regressão — deve ficar verde**

```bash
.venv/bin/pytest tests/test_guardrail.py -v
```

Expected: todos PASS. Se algum FAIL, o contrato do smoke divergiu — ajustar o teste para o comportamento real de `validate_sql` (ainda sem mudar produção), exceto se for bug óbvio de digitação no teste.

- [ ] **Step 3: Commit (só se o usuário pedir)**

```bash
git add tests/test_guardrail.py
git commit -m "$(cat <<'EOF'
test(guardrail): regressão pytest de validate_sql

Espelha o smoke: SELECT/CTE/UNION ok; DML, multi-stmt e allowlist rejeitam.
EOF
)"
```

---

### Task 2: Break — esqueleto + categorias 1–4

**Files:**
- Create: `tests/test_guardrail_break.py`

- [ ] **Step 1: Criar arquivo com helper + categorias 1–4**

```python
"""Red team do guardrail: payloads que DEVEM ser rejeitados.

Falha vermelha (DID NOT RAISE) = buraco em ``validate_sql``.
Nesta fase não corrigimos o guardrail e não usamos xfail.
"""

from __future__ import annotations

import pytest

from txt2sql.guardrail import ReadOnlyViolationError, validate_sql

# (case_id, sql, dialect, allowed_tables)
BreakCase = tuple[str, str, str | None, list[str] | None]

CASES: list[BreakCase] = [
    # --- 1. DML/DDL aninhado ---
    (
        "cte_delete_returning",
        "WITH c AS (DELETE FROM t RETURNING *) SELECT * FROM c",
        "postgres",
        None,
    ),
    (
        "cte_update_returning",
        "WITH c AS (UPDATE t SET x = 1 RETURNING *) SELECT * FROM c",
        "postgres",
        None,
    ),
    (
        "cte_insert_returning",
        "WITH c AS (INSERT INTO t(x) VALUES (1) RETURNING *) SELECT * FROM c",
        "postgres",
        None,
    ),
    (
        "subquery_delete_in_from",
        "SELECT * FROM (DELETE FROM t RETURNING *) AS d",
        "postgres",
        None,
    ),
    (
        "cte_merge",
        "WITH m AS (MERGE INTO t USING s ON t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET x = 1) SELECT 1",
        "tsql",
        None,
    ),
    (
        "nested_drop_in_cte",
        "WITH c AS (SELECT 1) SELECT * FROM c; DROP TABLE t",
        None,
        None,
    ),
    # --- 2. Multi-statement / stacking ---
    (
        "stack_select_drop",
        "SELECT 1; DROP TABLE t",
        None,
        None,
    ),
    (
        "stack_select_delete",
        "SELECT id FROM t; DELETE FROM t",
        None,
        None,
    ),
    (
        "stack_with_comment_between",
        "SELECT 1; /* evil */ DELETE FROM t",
        None,
        None,
    ),
    (
        "stack_whitespace_newline",
        "SELECT 1;\n\nUPDATE t SET x = 1",
        None,
        None,
    ),
    (
        "stack_select_into_second",
        "SELECT 1; SELECT * INTO neo FROM t",
        "tsql",
        None,
    ),
    # --- 3. Comment / token smuggling ---
    (
        "smuggle_insert_block_comment",
        "INS/**/ERT INTO t VALUES (1)",
        None,
        None,
    ),
    (
        "smuggle_delete_block_comment",
        "DEL/**/ETE FROM t",
        None,
        None,
    ),
    (
        "smuggle_into_mid_keyword",
        "SELECT id INT/**/O novo FROM t",
        "tsql",
        None,
    ),
    (
        "smuggle_drop_line_comment",
        "SELECT 1; --\nDROP TABLE t",
        None,
        None,
    ),
    (
        "smuggle_update_nested_comments",
        "UP/*x*/DATE/*y*/ t SET x = 1",
        None,
        None,
    ),
    (
        "smuggle_exec_comment",
        "EXE/**/C sp_who",
        "tsql",
        None,
    ),
    # --- 4. Ofuscação ---
    (
        "obfusc_null_byte_in_delete",
        "DEL\x00ETE FROM t",
        None,
        None,
    ),
    (
        "obfusc_newline_in_drop",
        "DRO\nP TABLE t",
        None,
        None,
    ),
    (
        "obfusc_tab_in_insert",
        "INS\tERT INTO t VALUES (1)",
        None,
        None,
    ),
    (
        "obfusc_zero_width_in_update",
        "UPDA\u200bTE t SET x = 1",
        None,
        None,
    ),
    (
        "obfusc_fullwidth_select_into",
        "SELECT id INTO novo FROM t",  # baseline; variante unicode abaixo
        "tsql",
        None,
    ),
    (
        "obfusc_homoglyph_insert",
        "ІNSERT INTO t VALUES (1)",  # І = U+0406 Cyrillic
        None,
        None,
    ),
    (
        "obfusc_mixed_case_xp",
        "ExEc Xp_CmdShell 'whoami'",
        "tsql",
        None,
    ),
]


@pytest.mark.parametrize(
    "case_id, sql, dialect, allowed_tables",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_break_must_reject(
    case_id: str,
    sql: str,
    dialect: str | None,
    allowed_tables: list[str] | None,
) -> None:
    with pytest.raises(ReadOnlyViolationError):
        validate_sql(sql, dialect=dialect, allowed_tables=allowed_tables)
```

- [ ] **Step 2: Rodar categorias 1–4 e anotar buracos**

```bash
.venv/bin/pytest tests/test_guardrail_break.py -v --tb=line 2>&1 | tee /tmp/guardrail_break_1.txt
```

Expected: mix de PASS (bloqueado) e FAIL DID NOT RAISE (buraco). **Não** “corrigir” falhas alterando asserts ou o guardrail. Só garantir que o harness está correto (import, parametrize, raises).

- [ ] **Step 3: Commit parcial (só se o usuário pedir)**

```bash
git add tests/test_guardrail_break.py
git commit -m "$(cat <<'EOF'
test(guardrail): red team categorias 1–4 (nested DML, stack, smuggle, obfusc)
EOF
)"
```

---

### Task 3: Break — categorias 5–8

**Files:**
- Modify: `tests/test_guardrail_break.py` (estender `CASES`)

- [ ] **Step 1: Acrescentar ao final de `CASES` (antes do fechamento `]`)**

```python
    # --- 5. SELECT INTO / materialização ---
    (
        "into_select_into_tsql",
        "SELECT * INTO #tmp FROM t",
        "tsql",
        None,
    ),
    (
        "into_with_comment",
        "SELECT id INT/*x*/O dest FROM t",
        "tsql",
        None,
    ),
    (
        "into_postgres_create_as_select",
        "CREATE TABLE dest AS SELECT * FROM t",
        "postgres",
        None,
    ),
    (
        "into_temp_table_select",
        "SELECT * INTO TEMP dest FROM t",
        "postgres",
        None,
    ),
    # --- 6. T-SQL procedural ---
    (
        "tsql_exec_sp",
        "EXEC sp_configure 'show advanced options', 1",
        "tsql",
        None,
    ),
    (
        "tsql_execute_xp",
        "EXECUTE xp_cmdshell 'dir'",
        "tsql",
        None,
    ),
    (
        "tsql_openrowset",
        "SELECT * FROM OPENROWSET('SQLOLEDB', 'server';uid';pwd', 'SELECT 1')",
        "tsql",
        None,
    ),
    (
        "tsql_openquery",
        "SELECT * FROM OPENQUERY(linked, 'SELECT 1')",
        "tsql",
        None,
    ),
    (
        "tsql_waitfor",
        "WAITFOR DELAY '00:00:10'",
        "tsql",
        None,
    ),
    (
        "tsql_dbcc",
        "DBCC CHECKDB",
        "tsql",
        None,
    ),
    (
        "tsql_bulk_insert",
        "BULK INSERT t FROM 'c:\\data.txt'",
        "tsql",
        None,
    ),
    (
        "tsql_select_plus_exec",
        "SELECT 1; EXEC sp_who",
        "tsql",
        None,
    ),
    # --- 7. Postgres side-effects ---
    (
        "pg_copy_program",
        "COPY t FROM PROGRAM 'id'",
        "postgres",
        None,
    ),
    (
        "pg_copy_to_program",
        "COPY (SELECT 1) TO PROGRAM 'cat >/tmp/x'",
        "postgres",
        None,
    ),
    (
        "pg_read_file",
        "SELECT pg_read_file('/etc/passwd')",
        "postgres",
        None,
    ),
    (
        "pg_lo_import",
        "SELECT lo_import('/etc/passwd')",
        "postgres",
        None,
    ),
    (
        "pg_dblink",
        "SELECT * FROM dblink('dbname=other', 'SELECT 1') AS t(x int)",
        "postgres",
        None,
    ),
    (
        "pg_select_into",
        "SELECT * INTO TEMP evil FROM t",
        "postgres",
        None,
    ),
    (
        "pg_create_extension",
        "CREATE EXTENSION dblink",
        "postgres",
        None,
    ),
    # --- 8. Controle de sessão ---
    (
        "session_set",
        "SET search_path TO evil",
        "postgres",
        None,
    ),
    (
        "session_use",
        "USE other_db",
        "tsql",
        None,
    ),
    (
        "session_begin",
        "BEGIN",
        "postgres",
        None,
    ),
    (
        "session_commit",
        "COMMIT",
        "postgres",
        None,
    ),
    (
        "session_begin_then_select",
        "BEGIN; SELECT 1",
        "postgres",
        None,
    ),
    (
        "session_set_then_select",
        "SET ROLE admin; SELECT 1",
        "postgres",
        None,
    ),
```

- [ ] **Step 2: Rodar suite break completa até aqui**

```bash
.venv/bin/pytest tests/test_guardrail_break.py -v --tb=line
```

Expected: mais PASS/FAIL misturados. Harness intacto.

- [ ] **Step 3: Commit (só se o usuário pedir)**

```bash
git add tests/test_guardrail_break.py
git commit -m "$(cat <<'EOF'
test(guardrail): red team categorias 5–8 (INTO, T-SQL, PG, sessão)
EOF
)"
```

---

### Task 4: Break — categorias 9–11 + fechar suite

**Files:**
- Modify: `tests/test_guardrail_break.py` (estender `CASES`)

- [ ] **Step 1: Acrescentar categorias 9–11 ao final de `CASES`**

```python
    # --- 9. Allowlist bypass ---
    (
        "allow_schema_qualified_out",
        "SELECT * FROM evil.proibida",
        "postgres",
        ["permitida"],
    ),
    (
        "allow_catalog_db_schema",
        "SELECT * FROM cat.sch.proibida",
        "postgres",
        ["permitida"],
    ),
    (
        "allow_alias_hides_real",
        "SELECT * FROM proibida AS permitida",
        None,
        ["permitida"],
    ),
    (
        "allow_cte_same_name_as_out_of_scope",
        # CTE 'proibida' é local; mas se o qualify/extração confundir com tabela real
        "WITH proibida AS (SELECT 1 AS x) SELECT * FROM proibida",
        None,
        ["permitida"],
    ),
    (
        "allow_join_sneak",
        "SELECT p.* FROM permitida p JOIN proibida e ON p.id = e.id",
        None,
        ["permitida"],
    ),
    (
        "allow_subquery_out",
        "SELECT * FROM permitida WHERE id IN (SELECT id FROM proibida)",
        None,
        ["permitida"],
    ),
    # --- 10. Parse ambiguity / Command ---
    (
        "amb_grant",
        "GRANT SELECT ON t TO public",
        None,
        None,
    ),
    (
        "amb_revoke",
        "REVOKE ALL ON t FROM public",
        None,
        None,
    ),
    (
        "amb_shutdown",
        "SHUTDOWN",
        "tsql",
        None,
    ),
    (
        "amb_reconfigure",
        "RECONFIGURE",
        "tsql",
        None,
    ),
    (
        "amb_garbage_should_reject",
        "THIS IS NOT SQL @@@",
        None,
        None,
    ),
    (
        "amb_empty_parens",
        "()",
        None,
        None,
    ),
    # --- 11. UNION / compostos ---
    (
        "union_with_delete_arm",
        "SELECT 1 UNION ALL DELETE FROM t RETURNING 1",
        "postgres",
        None,
    ),
    (
        "union_insert_arm",
        "SELECT x FROM t UNION ALL INSERT INTO t(x) VALUES (1) RETURNING x",
        "postgres",
        None,
    ),
    (
        "except_with_dml",
        "SELECT 1 EXCEPT SELECT * FROM (DELETE FROM t RETURNING 1) d",
        "postgres",
        None,
    ),
```

- [ ] **Step 2: Rodar regressão + break e coletar inventário**

```bash
.venv/bin/pytest tests/test_guardrail.py -v
.venv/bin/pytest tests/test_guardrail_break.py -v --tb=no -q
```

Expected:
- `test_guardrail.py`: 100% PASS
- `test_guardrail_break.py`: alguns FAIL — listar `case_id` que falharam (buracos)

Imprimir resumo dos buracos:

```bash
.venv/bin/pytest tests/test_guardrail_break.py -v --tb=no 2>&1 | rg -n "FAILED|PASSED" | rg "FAILED"
```

- [ ] **Step 3: Commit final (só se o usuário pedir)**

```bash
git add tests/test_guardrail.py tests/test_guardrail_break.py
git commit -m "$(cat <<'EOF'
test(guardrail): suite adversária completa (regressão + red team)

Revela falsos negativos de validate_sql sem alterar o guardrail.
EOF
)"
```

---

### Task 5: Verificação final

- [ ] **Step 1: Confirmar que produção não mudou**

```bash
git diff --name-only HEAD -- txt2sql/
```

Expected: vazio (ou só arquivos não relacionados a esta feature).

- [ ] **Step 2: Relatório curto no PR/chat**

Listar:
1. Quantos casos break PASS vs FAIL
2. `case_id` dos buracos (FAILED)
3. Lembrete: fase seguinte = endurecer `guardrail.py` até o break ficar verde

---

## Spec coverage checklist

| Spec | Task |
| --- | --- |
| `test_guardrail.py` regressão | Task 1 |
| `test_guardrail_break.py` raises | Tasks 2–4 |
| Cat. 1–4 nested/stack/smuggle/obfusc | Task 2 |
| Cat. 5–8 INTO/T-SQL/PG/sessão | Task 3 |
| Cat. 9–11 allowlist/ambiguidade/UNION | Task 4 |
| Sem xfail / sem fix guardrail | Note + Tasks |
| Como rodar / interpretar vermelho | Task 5 |

## Self-review

- Sem TBD/placeholders nos passos.
- Payloads literais inclusos.
- Tipos alinhados: `BreakCase = tuple[str, str, str | None, list[str] | None]`.
- Commits condicionados a pedido do usuário (alinhado às user rules).
