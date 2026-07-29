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
        "SELECT id INTO novo FROM t",
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
