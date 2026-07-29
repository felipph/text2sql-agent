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
        # CTE com nome allowlisted escondendo tabela fora do escopo no body
        "allow_cte_name_masks_forbidden_table",
        "WITH permitida AS (SELECT * FROM proibida) SELECT * FROM permitida",
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
    # --- 12. Side-effects em forma de SELECT (buracos esperados se aprovarem) ---
    (
        "side_set_config_role",
        "SELECT set_config('role', 'postgres', false)",
        "postgres",
        None,
    ),
    (
        "side_set_config_in_cte",
        "WITH x AS (SELECT set_config('search_path', 'evil', true) AS r) SELECT * FROM x",
        "postgres",
        None,
    ),
    (
        # adminpack: nome distinto de pg_write_file na denylist
        "side_pg_file_write",
        "SELECT pg_file_write('/tmp/x', 'a', true)",
        "postgres",
        None,
    ),
    (
        "side_pg_advisory_lock",
        "SELECT pg_advisory_lock(1)",
        "postgres",
        None,
    ),
    (
        "side_pg_advisory_xact_lock",
        "SELECT pg_advisory_xact_lock(42)",
        "postgres",
        None,
    ),
    (
        "side_pg_sleep",
        "SELECT pg_sleep(999)",
        "postgres",
        None,
    ),
    (
        "side_pg_sleep_for",
        "SELECT pg_sleep_for('5 minutes')",
        "postgres",
        None,
    ),
    (
        "side_current_setting_sensitive",
        "SELECT current_setting('data_directory')",
        "postgres",
        None,
    ),
    (
        "side_pg_stat_file",
        "SELECT * FROM pg_stat_file('/etc/passwd')",
        "postgres",
        None,
    ),
    (
        "side_lo_create",
        "SELECT lo_create(0)",
        "postgres",
        None,
    ),
    (
        "side_lo_put",
        "SELECT lo_put(1234, 0, 'ab')",
        "postgres",
        None,
    ),
    (
        "side_pg_notify",
        "SELECT pg_notify('q', 'payload')",
        "postgres",
        None,
    ),
    (
        "side_pg_create_logical_replication_slot",
        "SELECT * FROM pg_create_logical_replication_slot('x', 'test_decoding')",
        "postgres",
        None,
    ),
    (
        "side_pg_drop_replication_slot",
        "SELECT pg_drop_replication_slot('x')",
        "postgres",
        None,
    ),
    (
        "side_select_assign_tsql",
        "SELECT @x = id FROM t",
        "tsql",
        None,
    ),
    (
        "side_openjson_tvf",
        "SELECT * FROM OPENJSON('[]')",
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
