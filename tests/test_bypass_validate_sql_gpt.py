import pytest

from txt2sql.guardrail import ReadOnlyViolationError, validate_sql


# ==========================================================
# CASOS QUE DEVEM PASSAR
# ==========================================================

VALID_SQL = [
    "SELECT * FROM users",
    "SELECT id, name FROM users WHERE id = 1",
    "SELECT count(*) FROM users",
    "SELECT now()",
    "SELECT * FROM users LIMIT 10",
    "WITH cte AS (SELECT * FROM users) SELECT * FROM cte",
    "EXPLAIN SELECT * FROM users",
]

@pytest.mark.parametrize("sql", VALID_SQL)
def test_valid_queries(sql):
    assert validate_sql(sql) == sql


# ==========================================================
# COMANDOS DE ESCRITA ÓBVIOS
# ==========================================================

INVALID_SQL = [

    # insert
    "INSERT INTO users VALUES (1)",

    # update
    "UPDATE users SET name='abc'",

    # delete
    "DELETE FROM users",

    # truncate
    "TRUNCATE TABLE users",

    # drop
    "DROP TABLE users",

    # create
    "CREATE TABLE test(id int)",

    # alter
    "ALTER TABLE users ADD COLUMN age int",

    # replace
    "REPLACE INTO users VALUES (1)",

    # merge
    "MERGE INTO users USING backup",

    # grant
    "GRANT ALL ON users TO bob",

    # revoke
    "REVOKE ALL ON users FROM bob",

    # rename
    "RENAME TABLE users TO users_old",

    # comment
    "COMMENT ON TABLE users IS 'abc'",

    # analyze write?
    "VACUUM FULL users",

    # cluster
    "CLUSTER users",

    # refresh MV
    "REFRESH MATERIALIZED VIEW mv",

    # copy to table
    "COPY users FROM '/tmp/file.csv'",

    # load
    "LOAD DATA INFILE '/tmp/a.csv' INTO TABLE users",

]

@pytest.mark.parametrize("sql", INVALID_SQL)
def test_write_commands(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# MÚLTIPLAS INSTRUÇÕES
# ==========================================================

MULTI_STATEMENTS = [

    "SELECT * FROM users; DELETE FROM users",

    "SELECT 1;DROP TABLE users",

    "SELECT * FROM users;UPDATE users SET admin=true",

    "SELECT now();INSERT INTO users VALUES(1)",

]

@pytest.mark.parametrize("sql", MULTI_STATEMENTS)
def test_multiple_statements(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# ESCONDIDOS EM CTE
# ==========================================================

CTE_ATTACKS = [

    """
    WITH x AS (
        DELETE FROM users
        RETURNING *
    )
    SELECT * FROM x
    """,

    """
    WITH x AS (
        UPDATE users SET admin=true
        RETURNING *
    )
    SELECT * FROM x
    """,

]

@pytest.mark.parametrize("sql", CTE_ATTACKS)
def test_cte_attacks(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# RETURNING
# ==========================================================

RETURNING = [

    "DELETE FROM users RETURNING *",

    "UPDATE users SET x=1 RETURNING *",

]

@pytest.mark.parametrize("sql", RETURNING)
def test_returning(sql):
    with pytest.raises(Exception):
        validate_sql(sql)

def test_comment_cannot_hide_second_statement():
    """Comentário entre statements não neutraliza o DML empilhado."""
    with pytest.raises(ReadOnlyViolationError):
        validate_sql("SELECT * FROM users;/*hidden*/DELETE FROM users")


# ==========================================================
# KEYWORDS QUEBRADAS
# ==========================================================

OBFUSCATED = [

    "DR/**/OP TABLE users",

    "UP/**/DATE users SET x=1",

    "IN/**/SERT INTO users VALUES(1)",

    "DE/**/LETE FROM users",

]

@pytest.mark.parametrize("sql", OBFUSCATED)
def test_obfuscated_keywords(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# CASE INSENSITIVE
# ==========================================================

CASE_ATTACKS = [

    "drop table users",

    "DrOp TaBlE users",

    "uPdAtE users set x=1",

    "InSeRt Into users values(1)",

]

@pytest.mark.parametrize("sql", CASE_ATTACKS)
def test_case(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# WHITESPACE
# ==========================================================

WHITESPACE_ATTACKS = [

    "DROP\nTABLE users",

    "DROP\tTABLE users",

    "DROP\r\nTABLE users",

    "DROP\fTABLE users",

]

@pytest.mark.parametrize("sql", WHITESPACE_ATTACKS)
def test_whitespace(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# UNICODE
# ==========================================================

UNICODE_ATTACKS = [

    "DROP\u00A0TABLE users",

    "DROP\u2003TABLE users",

    "DROP\u2009TABLE users",

]

@pytest.mark.parametrize("sql", UNICODE_ATTACKS)
def test_unicode(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# STORED PROCEDURES
# ==========================================================

PROCEDURES = [

    "CALL delete_everything()",

    "EXEC delete_everything",

    "EXECUTE delete_everything()",

]

@pytest.mark.parametrize("sql", PROCEDURES)
def test_procedures(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# COPY PROGRAM (postgres)
# ==========================================================

POSTGRES = [

    "COPY users TO PROGRAM 'rm -rf /'",

    "COPY users FROM PROGRAM 'cat /tmp/x'",

]

@pytest.mark.parametrize("sql", POSTGRES)
def test_postgres(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# MYSQL
# ==========================================================

MYSQL = [

    "LOAD DATA LOCAL INFILE '/tmp/a' INTO TABLE users",

    "REPLACE INTO users VALUES(1)",

]

@pytest.mark.parametrize("sql", MYSQL)
def test_mysql(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# SQL SERVER
# ==========================================================

SQLSERVER = [

    "EXEC xp_cmdshell 'dir'",

    "EXEC sp_configure",

]

@pytest.mark.parametrize("sql", SQLSERVER)
def test_sqlserver(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# ORACLE
# ==========================================================

ORACLE = [

    "BEGIN EXECUTE IMMEDIATE 'DROP TABLE users'; END;",

]

@pytest.mark.parametrize("sql", ORACLE)
def test_oracle(sql):
    with pytest.raises(Exception):
        validate_sql(sql)


# ==========================================================
# ALLOWED TABLES
# ==========================================================

def test_allowed_table_ok():
    sql = "SELECT * FROM users"

    assert (
        validate_sql(
            sql,
            allowed_tables=["users"],
        )
        == sql
    )


def test_allowed_table_violation():
    with pytest.raises(Exception):
        validate_sql(
            "SELECT * FROM admin",
            allowed_tables=["users"],
        )