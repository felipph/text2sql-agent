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
